package TxMvp;

import Assert :: *;
import Connectable :: *;
import FIFOF :: *;

import CommonTypes :: *;
import TxTables :: *;
import TxMvpTypes :: *;

interface SubmissionFrontendIfc;
    interface PipeIn#(DecodedGdrpSubmission) submissionIn;
    interface ClientP#(SlotQueryReq, SlotQueryResp) slotQuery;
    interface PipeOut#(TxWorkRequest) workRequestOut;
endinterface

module mkSubmissionFrontend(SubmissionFrontendIfc);
    // Main pipeline: decoded submission -> Slot-joined work request.
    // pendingQ keeps each submission aligned with the ordered Slot response.
    FIFOF#(DecodedGdrpSubmission) submissionQ <- mkSizedFIFOF(2);
    FIFOF#(DecodedGdrpSubmission) pendingQ <- mkSizedFIFOF(2);
    FIFOF#(TxWorkRequest) workQ <- mkSizedFIFOF(2);

    // Slot Table side service: keep the request/response pair together.
    FIFOF#(SlotQueryReq) slotReqQ <- mkSizedFIFOF(2);
    FIFOF#(SlotQueryResp) slotRespQ <- mkSizedFIFOF(2);

    rule issueSlotQuery;
        let req = submissionQ.first;
        Bit#(8) flags = req.token[7:0];
        TxSlotId slotId = req.token[57:40];
        Bit#(2) opcode = req.token[59:58];

        dynamicAssert(req.qpn < 24'd4, "TX MVP QPN out of range");
        dynamicAssert(flags == 0, "TX MVP token flags must be zero");
        dynamicAssert(opcode == 0, "TX MVP only supports WRITE_RELAXED");
        dynamicAssert(slotId < 18'd16, "TX MVP slot index out of range");
        submissionQ.deq;
        pendingQ.enq(req);
        slotReqQ.enq(SlotQueryReq { slotId: slotId });
    endrule

    rule completeSlotQuery;
        let req = pendingQ.first;
        let resp = slotRespQ.first;
        Bit#(32) lenField = req.token[39:8];
        TxSlotId slotId = req.token[57:40];
        Bit#(4) debugSeq = req.token[63:60];
        TxLength resolvedLen = lenField == 0
            ? 25'h1000000
            : truncate(lenField);
        PayloadSlot slot = resp.entry;

        dynamicAssert(resp.slotId == slotId, "TX MVP slot response reordered");
        dynamicAssert(slot.valid == 1, "TX MVP queried invalid slot");
        dynamicAssert(slot.allowWriteRelaxed == 1, "TX MVP slot lacks WRITE_RELAXED capability");
        dynamicAssert(lenField <= 32'h01000000, "TX MVP length exceeds 16 MiB");
        dynamicAssert(resolvedLen <= slot.maxLen, "TX MVP length exceeds slot limit");
        dynamicAssert(!txAddressOverflows(slot.localOffset, resolvedLen), "TX MVP local offset overflows");
        dynamicAssert(!txAddressOverflows(slot.remoteBase, resolvedLen), "TX MVP remote address overflows");
        pendingQ.deq;
        slotRespQ.deq;
        workQ.enq(TxWorkRequest {
            qpn: req.qpn,
            slotId: slotId,
            debugSeq: debugSeq,
            localLkey: slot.localLkey,
            localOffset: slot.localOffset,
            remoteAddr: slot.remoteBase,
            len: resolvedLen,
            remoteKey: slot.remoteKey
        });
    endrule

    interface submissionIn = toPipeIn(submissionQ);
    interface slotQuery = toClientP(toPipeOut(slotReqQ), toPipeIn(slotRespQ));
    interface workRequestOut = toPipeOut(workQ);
endmodule

interface ProcessSubmissionIfc;
    interface PipeIn#(TxWorkRequest) workRequestIn;
    interface ClientP#(MrQueryReq, MrQueryResp) mrQuery;
    interface ClientP#(QpReserveReq, QpReserveResp) qpReserve;
    interface PipeOut#(ParsedTxRequest) parsedOut;
endinterface

module mkProcessSubmission(ProcessSubmissionIfc);
    // Main pipeline: work input -> MR-admitted context -> QP-admitted output.
    // The pending FIFOs preserve the ordered Work Item at each query join.
    Reg#(TxRequestId) nextRequestId <- mkReg(0);
    FIFOF#(TxWorkRequest) workQ <- mkSizedFIFOF(2);
    FIFOF#(TxWorkRequest) pendingMrQ <- mkSizedFIFOF(2);
    FIFOF#(MrAdmittedRequest) pendingQpQ <- mkSizedFIFOF(2);
    FIFOF#(ParsedTxRequest) parsedQ <- mkSizedFIFOF(2);

    // MR Table side service: request and ordered response.
    FIFOF#(MrQueryReq) mrReqQ <- mkSizedFIFOF(2);
    FIFOF#(MrQueryResp) mrRespQ <- mkSizedFIFOF(2);

    // QP/PSN side service: reservation request and ordered response.
    FIFOF#(QpReserveReq) qpReqQ <- mkSizedFIFOF(2);
    FIFOF#(QpReserveResp) qpRespQ <- mkSizedFIFOF(2);

    rule issueMrQuery;
        let work = workQ.first;
        workQ.deq;
        pendingMrQ.enq(work);
        mrReqQ.enq(MrQueryReq { localLkey: work.localLkey });
    endrule

    rule completeMrQuery;
        let work = pendingMrQ.first;
        let resp = mrRespQ.first;
        MrEntry mr = resp.entry;
        Bit#(65) endOffset = zeroExtend(work.localOffset) + zeroExtend(work.len);
        Bit#(65) localVaWide = zeroExtend(mr.baseVa) + zeroExtend(work.localOffset);

        dynamicAssert(resp.localLkey == work.localLkey, "TX MVP MR response reordered");
        dynamicAssert(txLocalLkeyMrIndex(work.localLkey) < 13'd16, "TX MVP MR index out of range");
        dynamicAssert(mr.valid == 1, "TX MVP queried invalid MR");
        dynamicAssert(mr.keyPart == txLocalLkeyKeyPart(work.localLkey), "TX MVP lkey mismatch");
        dynamicAssert(mr.allowLocalRead == 1, "TX MVP MR lacks local-read permission");
        dynamicAssert(endOffset[64] == 0 && endOffset <= zeroExtend(mr.length), "TX MVP MR range violation");
        dynamicAssert(localVaWide[64] == 0, "TX MVP local address overflows");
        dynamicAssert(txPageShiftIsValid(mr.pageShift), "TX MVP unsupported page size");
        pendingMrQ.deq;
        mrRespQ.deq;
        pendingQpQ.enq(MrAdmittedRequest {
            work: work,
            localVa: truncate(localVaWide),
            mrBaseVa: mr.baseVa,
            pgtOffset: mr.pgtOffset,
            pageShift: mr.pageShift
        });
        qpReqQ.enq(QpReserveReq { qpn: work.qpn, len: work.len });
    endrule

    rule completeQpReserve;
        let admitted = pendingQpQ.first;
        let resp = qpRespQ.first;
        let work = admitted.work;
        dynamicAssert(resp.qpn == work.qpn, "TX MVP QP response reordered");
        pendingQpQ.deq;
        qpRespQ.deq;
        parsedQ.enq(ParsedTxRequest {
            requestId: nextRequestId,
            qpn: work.qpn,
            slotId: work.slotId,
            debugSeq: work.debugSeq,
            pmtu: resp.pmtu,
            startPsn: resp.startPsn,
            psnCount: resp.psnCount,
            localVa: admitted.localVa,
            mrBaseVa: admitted.mrBaseVa,
            pgtOffset: admitted.pgtOffset,
            pageShift: admitted.pageShift,
            remoteAddr: work.remoteAddr,
            len: work.len,
            remoteKey: work.remoteKey
        });
        nextRequestId <= nextRequestId + 1;
    endrule

    interface workRequestIn = toPipeIn(workQ);
    interface mrQuery = toClientP(toPipeOut(mrReqQ), toPipeIn(mrRespQ));
    interface qpReserve = toClientP(toPipeOut(qpReqQ), toPipeIn(qpRespQ));
    interface parsedOut = toPipeOut(parsedQ);
endmodule

interface AddressTranslatorIfc;
    interface PipeIn#(ParsedTxRequest) parsedIn;
    interface ClientP#(PgtQueryReq, PgtQueryResp) pgtQuery;
    interface PipeOut#(TranslatedPageFragment) pageFragmentOut;
endinterface

module mkAddressTranslator(AddressTranslatorIfc);
    // Main pipeline: parsed request -> pending page context -> physical page fragment.
    // translatedBytes tracks the current message while its pages are emitted in order.
    Reg#(TxLength) translatedBytes <- mkReg(0);
    FIFOF#(ParsedTxRequest) parsedQ <- mkSizedFIFOF(2);
    FIFOF#(PendingPgtFragment) pendingQ <- mkSizedFIFOF(1);
    FIFOF#(TranslatedPageFragment) pageQ <- mkSizedFIFOF(2);

    // PGT side service: one page-translation request and its ordered response.
    FIFOF#(PgtQueryReq) pgtReqQ <- mkSizedFIFOF(1);
    FIFOF#(PgtQueryResp) pgtRespQ <- mkSizedFIFOF(1);

    rule issuePgtQuery;
        let parsed = parsedQ.first;
        Bit#(64) pageBytes = txPageBytes(parsed.pageShift);
        Bit#(64) pageMask = pageBytes - 1;
        Bit#(64) completed = zeroExtend(translatedBytes);
        Bit#(64) currentVa = parsed.localVa + completed;
        Bit#(64) currentRemoteAddr = parsed.remoteAddr + completed;
        Bit#(64) remaining = zeroExtend(parsed.len) - completed;
        Bit#(64) pageOffset = currentVa & pageMask;
        Bit#(64) toPageEnd = pageBytes - pageOffset;
        Bit#(64) fragmentWide = remaining < toPageEnd ? remaining : toPageEnd;
        TxLength fragmentLen = truncate(fragmentWide);
        Bit#(1) lastFragment = pack(fragmentWide == remaining);
        Bit#(64) virtualPageIndex =
            (currentVa >> parsed.pageShift) -
            (parsed.mrBaseVa >> parsed.pageShift);
        Bit#(64) pteIndex = zeroExtend(parsed.pgtOffset) + virtualPageIndex;

        dynamicAssert(pageBytes != 0, "TX MVP unsupported page size");
        dynamicAssert(fragmentWide != 0, "TX MVP zero-length page fragment");
        pendingQ.enq(PendingPgtFragment {
            parsed: parsed,
            pageOffset: pageOffset,
            currentRemoteAddr: currentRemoteAddr,
            fragmentLen: fragmentLen,
            isFirst: pack(translatedBytes == 0),
            isLast: lastFragment,
            pteIndex: pteIndex
        });
        pgtReqQ.enq(PgtQueryReq { pteIndex: pteIndex });
    endrule

    rule completePgtQuery;
        let pending = pendingQ.first;
        let resp = pgtRespQ.first;
        let parsed = pending.parsed;
        Bit#(64) pageBytes = txPageBytes(parsed.pageShift);
        Bit#(64) pageMask = pageBytes - 1;
        Bit#(65) physicalWide =
            zeroExtend(resp.entry.physicalPageBase) + zeroExtend(pending.pageOffset);
        TxLength nextTranslatedBytes = pending.isLast == 1
            ? 0
            : translatedBytes + pending.fragmentLen;

        dynamicAssert(resp.pteIndex == pending.pteIndex, "TX MVP PGT response reordered");
        dynamicAssert(resp.entry.valid == 1, "TX MVP queried invalid PTE");
        dynamicAssert((resp.entry.physicalPageBase & pageMask) == 0, "TX MVP PTE base misaligned");
        dynamicAssert(physicalWide[64] == 0, "TX MVP physical address overflows");
        pendingQ.deq;
        pgtRespQ.deq;
        pageQ.enq(TranslatedPageFragment {
            requestId: parsed.requestId,
            qpn: parsed.qpn,
            slotId: parsed.slotId,
            debugSeq: parsed.debugSeq,
            pmtu: parsed.pmtu,
            startPsn: parsed.startPsn,
            psnCount: parsed.psnCount,
            messageLength: parsed.len,
            physicalAddr: truncate(physicalWide),
            remoteAddr: pending.currentRemoteAddr,
            len: pending.fragmentLen,
            remoteKey: parsed.remoteKey,
            isFirst: pending.isFirst,
            isLast: pending.isLast
        });
        translatedBytes <= nextTranslatedBytes;
        if (pending.isLast == 1) begin
            parsedQ.deq;
        end
    endrule

    interface parsedIn = toPipeIn(parsedQ);
    interface pgtQuery = toClientP(toPipeOut(pgtReqQ), toPipeIn(pgtRespQ));
    interface pageFragmentOut = toPipeOut(pageQ);
endmodule

interface PhysicalDmaBurstSplitterIfc;
    interface PipeIn#(TranslatedPageFragment) pageFragmentIn;
    interface PipeOut#(TxDmaReadRequest) dmaReadReqOut;
endinterface

module mkPhysicalDmaBurstSplitter(PhysicalDmaBurstSplitterIfc);
    // Main pipeline: physical page fragment -> boundary-safe DMA bursts.
    // The progress registers describe the head page/message held in pageQ.
    Reg#(TxLength) pageConsumed <- mkReg(0);
    Reg#(TxDmaSeq) nextDmaSeq <- mkReg(0);
    FIFOF#(TranslatedPageFragment) pageQ <- mkSizedFIFOF(2);
    FIFOF#(TxDmaReadRequest) dmaQ <- mkSizedFIFOF(2);

    rule emitBurst;
        let page = pageQ.first;
        Bit#(64) consumed = zeroExtend(pageConsumed);
        Bit#(64) currentPhysicalAddr = page.physicalAddr + consumed;
        Bit#(64) currentRemoteAddr = page.remoteAddr + consumed;
        Bit#(64) remaining = zeroExtend(page.len) - consumed;
        Bit#(64) toPhysical4KiBBoundary = 64'd4096 - zeroExtend(currentPhysicalAddr[11:0]);
        Bit#(64) burstWide = remaining < 64'd1024 ? remaining : 64'd1024;
        Bit#(64) boundedBurstWide = burstWide < toPhysical4KiBBoundary
            ? burstWide
            : toPhysical4KiBBoundary;
        TxLength burstLen = truncate(boundedBurstWide);
        Bit#(1) pageFinished = pack(boundedBurstWide == remaining);
        Bit#(1) messageFinished = page.isLast & pageFinished;
        TxLength nextPageConsumed = pageFinished == 1
            ? 0
            : pageConsumed + burstLen;
        TxDmaSeq followingDmaSeq = messageFinished == 1
            ? 0
            : nextDmaSeq + 1;

        dynamicAssert(remaining != 0, "TX MVP zero-length DMA remainder");
        dynamicAssert(boundedBurstWide != 0, "TX MVP zero-length DMA burst");
        dynamicAssert(boundedBurstWide <= 64'd1024, "TX MVP DMA burst exceeds 1024 bytes");
        dmaQ.enq(TxDmaReadRequest {
            requestId: page.requestId,
            qpn: page.qpn,
            slotId: page.slotId,
            debugSeq: page.debugSeq,
            pmtu: page.pmtu,
            startPsn: page.startPsn,
            psnCount: page.psnCount,
            messageLength: page.messageLength,
            dmaSeq: nextDmaSeq,
            physicalAddr: currentPhysicalAddr,
            remoteAddr: currentRemoteAddr,
            len: burstLen,
            remoteKey: page.remoteKey,
            isFirst: page.isFirst & pack(pageConsumed == 0),
            isLast: messageFinished
        });
        pageConsumed <= nextPageConsumed;
        nextDmaSeq <= followingDmaSeq;
        if (pageFinished == 1) begin
            pageQ.deq;
        end
    endrule

    interface pageFragmentIn = toPipeIn(pageQ);
    interface dmaReadReqOut = toPipeOut(dmaQ);
endmodule

interface TxMvpIfc;
    interface ServerP#(SlotCfgReq, TxCfgResp) slotConfig;
    interface ServerP#(QpCfgReq, TxCfgResp) qpConfig;
    interface ServerP#(MrCfgReq, TxCfgResp) mrConfig;
    interface ServerP#(PgtCfgReq, TxCfgResp) pgtConfig;

    interface PipeIn#(DecodedGdrpSubmission) submissionIn;
    interface PipeOut#(TxDmaReadRequest) dmaReadReqOut;
endinterface

(* synthesize *)
module mkTxMvp(TxMvpIfc);
    // Configuration sideband: the four untagged services share one outstanding
    // transaction so their independent response ports cannot be confused.
    Reg#(Bit#(1)) configPending <- mkReg(0);

    SlotTableIfc slotTable <- mkSlotTable;
    QpStateIfc qpState <- mkQpState;
    MrTableIfc mrTable <- mkMrTable;
    PgtTableIfc pgtTable <- mkPgtTable;
    SubmissionFrontendIfc frontend <- mkSubmissionFrontend;
    ProcessSubmissionIfc processor <- mkProcessSubmission;
    AddressTranslatorIfc translator <- mkAddressTranslator;
    PhysicalDmaBurstSplitterIfc burstSplitter <- mkPhysicalDmaBurstSplitter;

    mkConnection(frontend.slotQuery, slotTable.query);
    mkConnection(frontend.workRequestOut, processor.workRequestIn);
    mkConnection(processor.mrQuery, mrTable.query);
    mkConnection(processor.qpReserve, qpState.reserve);
    mkConnection(processor.parsedOut, translator.parsedIn);
    mkConnection(translator.pgtQuery, pgtTable.query);
    mkConnection(translator.pageFragmentOut, burstSplitter.pageFragmentIn);

    interface ServerP slotConfig;
        interface PipeIn request;
            method Action enq(SlotCfgReq req)
                if (configPending == 0);
                slotTable.cfg.request.enq(req);
                configPending <= 1;
            endmethod
            method Bit#(1) notFull;
                return pack(configPending == 0) &
                       slotTable.cfg.request.notFull;
            endmethod
        endinterface
        interface PipeOut response;
            method TxCfgResp first if (configPending == 1);
                return slotTable.cfg.response.first;
            endmethod
            method Action deq if (configPending == 1);
                slotTable.cfg.response.deq;
                configPending <= 0;
            endmethod
            method Bit#(1) notEmpty;
                return pack(configPending == 1) &
                       slotTable.cfg.response.notEmpty;
            endmethod
        endinterface
    endinterface

    interface ServerP qpConfig;
        interface PipeIn request;
            method Action enq(QpCfgReq req)
                if (configPending == 0);
                qpState.cfg.request.enq(req);
                configPending <= 1;
            endmethod
            method Bit#(1) notFull;
                return pack(configPending == 0) &
                       qpState.cfg.request.notFull;
            endmethod
        endinterface
        interface PipeOut response;
            method TxCfgResp first if (configPending == 1);
                return qpState.cfg.response.first;
            endmethod
            method Action deq if (configPending == 1);
                qpState.cfg.response.deq;
                configPending <= 0;
            endmethod
            method Bit#(1) notEmpty;
                return pack(configPending == 1) &
                       qpState.cfg.response.notEmpty;
            endmethod
        endinterface
    endinterface

    interface ServerP mrConfig;
        interface PipeIn request;
            method Action enq(MrCfgReq req)
                if (configPending == 0);
                mrTable.cfg.request.enq(req);
                configPending <= 1;
            endmethod
            method Bit#(1) notFull;
                return pack(configPending == 0) &
                       mrTable.cfg.request.notFull;
            endmethod
        endinterface
        interface PipeOut response;
            method TxCfgResp first if (configPending == 1);
                return mrTable.cfg.response.first;
            endmethod
            method Action deq if (configPending == 1);
                mrTable.cfg.response.deq;
                configPending <= 0;
            endmethod
            method Bit#(1) notEmpty;
                return pack(configPending == 1) &
                       mrTable.cfg.response.notEmpty;
            endmethod
        endinterface
    endinterface

    interface ServerP pgtConfig;
        interface PipeIn request;
            method Action enq(PgtCfgReq req)
                if (configPending == 0);
                pgtTable.cfg.request.enq(req);
                configPending <= 1;
            endmethod
            method Bit#(1) notFull;
                return pack(configPending == 0) &
                       pgtTable.cfg.request.notFull;
            endmethod
        endinterface
        interface PipeOut response;
            method TxCfgResp first if (configPending == 1);
                return pgtTable.cfg.response.first;
            endmethod
            method Action deq if (configPending == 1);
                pgtTable.cfg.response.deq;
                configPending <= 0;
            endmethod
            method Bit#(1) notEmpty;
                return pack(configPending == 1) &
                       pgtTable.cfg.response.notEmpty;
            endmethod
        endinterface
    endinterface

    interface submissionIn = frontend.submissionIn;
    interface dmaReadReqOut = burstSplitter.dmaReadReqOut;
endmodule

endpackage
