package EvPathManager;

import Assert :: *;
import FIFOF :: *;
import Vector :: *;

import CommonTypes :: *;
import MrcTxTypes :: *;
import TxTypes :: *;

typedef struct {
    Bit#(1)   valid;
    Bit#(1)   adminEnable;
    Bit#(32)  entropy;
    Bit#(2)   channel;
    Bit#(8)   generation;
    MrcEvState state;
} MrcEvEntry deriving(Bits, Eq, FShow);

typedef struct {
    Maybe#(MrcResolvedPath) selected;
    Bit#(1)                usable;
} MrcPathResolution deriving(Bits, Eq, FShow);

interface EvPathManagerIfc;
    interface PipeIn#(MrcEvEntryConfig) entryConfigIn;
    interface PipeIn#(MrcEvPolicyConfig) policyConfigIn;
    interface PipeIn#(MrcPortStatusUpdate) portStatusIn;
    interface PipeIn#(MrcPathFeedback) feedbackIn;
    interface PipeOut#(MrcProbeRequest) probeOut;

    // `availableChannels` includes transient buffer availability; `allowedChannels`
    // excludes only administratively or operationally unavailable channels.
    // Returning both answers from one scan avoids duplicating the eight-entry
    // profile search for admission and NoPath reporting.
    method MrcPathResolution resolve(
        TxEvProfileId profileId,
        Bit#(4) availableChannels,
        Bit#(4) allowedChannels);
    method Action commitSelection(MrcResolvedPath path);
    method Action setAdminPortMask(Bit#(4) mask);
    method Bit#(1) updatePending;
endinterface

function Bool timeReached(Bit#(16) now, Bit#(16) deadline);
    Bit#(16) elapsed = now - deadline;
    return elapsed[15] == 0;
endfunction

module mkEvPathManager(EvPathManagerIfc);
    Vector#(32, Reg#(MrcEvEntry)) entries <- replicateM(mkReg(unpack(0)));
    // EV_SKIP and EV_ASSUMED_BAD are mutually exclusive, so one deadline holds
    // either the skip expiry or the next probe time according to entry.state.
    Vector#(32, Reg#(Bit#(16))) stateDeadline <- replicateM(mkReg(0));
    Vector#(32, Reg#(Bit#(16))) nextProbeId <- replicateM(mkReg(0));
    Vector#(32, Reg#(Bool)) probeOutstanding <- replicateM(mkReg(False));
    Vector#(4, Reg#(Bit#(3))) profileCursor <- replicateM(mkReg(0));

    FIFOF#(MrcEvEntryConfig) entryConfigQ <- mkSizedFIFOF(2);
    FIFOF#(MrcEvPolicyConfig) policyConfigQ <- mkSizedFIFOF(2);
    FIFOF#(MrcPortStatusUpdate) portStatusQ <- mkSizedFIFOF(2);
    FIFOF#(MrcPathFeedback) feedbackQ <- mkSizedFIFOF(4);
    FIFOF#(MrcProbeRequest) probeQ <- mkSizedFIFOF(4);

    Reg#(Bit#(4)) adminPortMask <- mkReg(4'hF);
    Reg#(Bit#(4)) localReachable <- mkReg(4'hF);
    Reg#(Bit#(4)) peerReachable <- mkReg(4'hF);
    Reg#(Bit#(16)) skipHoldCycles <- mkReg(16'd1024);
    Reg#(Bit#(16)) probeIntervalCycles <- mkReg(16'd4096);
    Reg#(Bit#(16)) cycleCounter <- mkReg(0);
    Reg#(Bit#(5)) scanCursor <- mkReg(0);

    rule tick;
        cycleCounter <= cycleCounter + 1;
    endrule

    rule applyPolicy (policyConfigQ.notEmpty);
        let cfg = policyConfigQ.first;
        policyConfigQ.deq;
        dynamicAssert(cfg.skipHoldCycles != 0 && cfg.skipHoldCycles[15] == 0,
            "M3 EV skip interval must be in [1, 32767]");
        dynamicAssert(cfg.probeIntervalCycles != 0 &&
                cfg.probeIntervalCycles[15] == 0,
            "M3 EV probe interval must be in [1, 32767]");
        skipHoldCycles <= cfg.skipHoldCycles;
        probeIntervalCycles <= cfg.probeIntervalCycles;
    endrule

    rule applyPortStatus (portStatusQ.notEmpty);
        let status = portStatusQ.first;
        portStatusQ.deq;
        localReachable <= status.localReachable;
        peerReachable <= status.peerReachable;
    endrule

    (* descending_urgency = "applyEntryConfig, applyFeedback, scanEntry" *)
    rule applyEntryConfig (entryConfigQ.notEmpty);
        let cfg = entryConfigQ.first;
        entryConfigQ.deq;
        Bit#(5) flatIndex = { cfg.profileId, cfg.entryIndex };
        for (Integer i = 0; i < 32; i = i + 1) begin
            if (flatIndex == fromInteger(i)) begin
                let old = entries[i];
                entries[i] <= MrcEvEntry {
                    valid: cfg.valid,
                    adminEnable: cfg.adminEnable,
                    entropy: cfg.entropy,
                    channel: cfg.channel,
                    generation: old.generation + 1,
                    state: cfg.valid == 1 && cfg.adminEnable == 1
                        ? EV_GOOD : EV_DENIED
                };
                stateDeadline[i] <= cycleCounter;
                probeOutstanding[i] <= False;
            end
        end
    endrule

    rule applyFeedback (feedbackQ.notEmpty);
        let feedback = feedbackQ.first;
        feedbackQ.deq;
        Bit#(5) flatIndex = { feedback.profileId, feedback.entryIndex };
        for (Integer i = 0; i < 32; i = i + 1) begin
            if (flatIndex == fromInteger(i)) begin
                let entry = entries[i];
                Bool current = entry.valid == 1 &&
                    entry.generation == feedback.generation;
                Bool matchingProbe = probeOutstanding[i] &&
                    nextProbeId[i] - 1 == feedback.probeId;
                if (current && entry.adminEnable == 1 &&
                    entry.state != EV_DENIED) begin
                    case (feedback.kind)
                        EV_FB_ECN, EV_FB_SKIP_ONCE, EV_FB_TRIMMED: begin
                            entry.state = EV_SKIP;
                            entries[i] <= entry;
                            stateDeadline[i] <= cycleCounter + skipHoldCycles;
                        end
                        EV_FB_ALWAYS_SKIP, EV_FB_TIMEOUT: begin
                            entry.state = EV_ASSUMED_BAD;
                            entries[i] <= entry;
                            stateDeadline[i] <= cycleCounter;
                            probeOutstanding[i] <= False;
                        end
                        EV_FB_PROBE_GOOD: begin
                            if (matchingProbe) begin
                                entry.state = EV_GOOD;
                                entries[i] <= entry;
                                probeOutstanding[i] <= False;
                            end
                        end
                        EV_FB_PROBE_SKIP: begin
                            if (matchingProbe) begin
                                entry.state = EV_SKIP;
                                entries[i] <= entry;
                                stateDeadline[i] <= cycleCounter + skipHoldCycles;
                                probeOutstanding[i] <= False;
                            end
                        end
                        default: noAction;
                    endcase
                end
            end
        end
    endrule

    rule scanEntry;
        Bit#(5) index = scanCursor;
        scanCursor <= scanCursor + 1;
        for (Integer i = 0; i < 32; i = i + 1) begin
            if (index == fromInteger(i)) begin
                let entry = entries[i];
                if (entry.valid == 1 && entry.adminEnable == 1 &&
                        entry.state == EV_SKIP &&
                        timeReached(cycleCounter, stateDeadline[i])) begin
                    entry.state = EV_GOOD;
                    entries[i] <= entry;
                end else if (entry.valid == 1 && entry.adminEnable == 1 &&
                        entry.state == EV_ASSUMED_BAD && probeQ.notFull &&
                        timeReached(cycleCounter, stateDeadline[i]) &&
                        (adminPortMask & localReachable & peerReachable)
                            [entry.channel] == 1) begin
                    Bit#(16) probeId = nextProbeId[i];
                    probeQ.enq(MrcProbeRequest {
                        profileId: truncate(index >> 3),
                        entryIndex: truncate(index),
                        generation: entry.generation,
                        entropy: entry.entropy,
                        channel: entry.channel,
                        probeId: probeId
                    });
                    nextProbeId[i] <= probeId + 1;
                    stateDeadline[i] <= cycleCounter + probeIntervalCycles;
                    probeOutstanding[i] <= True;
                end
            end
        end
    endrule

    function MrcPathResolution findPath(
        TxEvProfileId profileId,
        Bit#(4) availableChannels,
        Bit#(4) allowedChannels);
        Bit#(4) pathPortMask = adminPortMask & localReachable & peerReachable;
        Bit#(4) effectiveAvailable = availableChannels & pathPortMask;
        Bit#(4) effectiveAllowed = allowedChannels & pathPortMask;
        Bit#(3) cursor = 0;
        case (profileId)
            0: cursor = profileCursor[0];
            1: cursor = profileCursor[1];
            2: cursor = profileCursor[2];
            default: cursor = profileCursor[3];
        endcase
        Maybe#(MrcResolvedPath) selected = tagged Invalid;
        Bool usable = False;
        for (Integer offset = 0; offset < 8; offset = offset + 1) begin
            Bit#(3) entryIndex = cursor + fromInteger(offset);
            Bit#(5) flatIndex = { profileId, entryIndex };
            let entry = entries[flatIndex];
            Bool good = entry.valid == 1 && entry.adminEnable == 1 &&
                entry.state == EV_GOOD;
            if (good && effectiveAllowed[entry.channel] == 1)
                usable = True;
            if (!isValid(selected) && good &&
                    effectiveAvailable[entry.channel] == 1) begin
                selected = tagged Valid MrcResolvedPath {
                    profileId: profileId,
                    entryIndex: entryIndex,
                    generation: entry.generation,
                    entropy: entry.entropy,
                    channel: entry.channel
                };
            end
        end
        return MrcPathResolution {
            selected: selected,
            usable: pack(usable)
        };
    endfunction

    method MrcPathResolution resolve(
        TxEvProfileId profileId,
        Bit#(4) availableChannels,
        Bit#(4) allowedChannels);
        return findPath(profileId, availableChannels, allowedChannels);
    endmethod

    method Action commitSelection(MrcResolvedPath path);
        Bit#(3) next = path.entryIndex + 1;
        case (path.profileId)
            0: profileCursor[0] <= next;
            1: profileCursor[1] <= next;
            2: profileCursor[2] <= next;
            default: profileCursor[3] <= next;
        endcase
    endmethod

    method Action setAdminPortMask(Bit#(4) mask);
        adminPortMask <= mask;
    endmethod

    method Bit#(1) updatePending = pack(
        entryConfigQ.notEmpty || policyConfigQ.notEmpty ||
        portStatusQ.notEmpty || feedbackQ.notEmpty);

    interface entryConfigIn = toPipeIn(entryConfigQ);
    interface policyConfigIn = toPipeIn(policyConfigQ);
    interface portStatusIn = toPipeIn(portStatusQ);
    interface feedbackIn = toPipeIn(feedbackQ);
    interface probeOut = toPipeOut(probeQ);
endmodule

endpackage
