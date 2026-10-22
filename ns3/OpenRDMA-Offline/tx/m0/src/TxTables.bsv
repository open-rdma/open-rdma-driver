package TxTables;

import Assert :: *;
import FIFOF :: *;
import Vector :: *;

import CommonTypes :: *;
import TxMvpTypes :: *;

interface SlotTableIfc;
    interface ServerP#(SlotQueryReq, SlotQueryResp) query;
    interface ServerP#(SlotCfgReq, TxCfgResp) cfg;
endinterface

module mkSlotTable(SlotTableIfc);
    // Backing state.
    Vector#(16, Reg#(PayloadSlot)) entries <- replicateM(mkReg(unpack(0)));

    // Data-plane query service: request and ordered response.
    FIFOF#(SlotQueryReq) queryReqQ <- mkSizedFIFOF(2);
    FIFOF#(SlotQueryResp) queryRespQ <- mkSizedFIFOF(2);

    // Control-plane configuration service: request and commit response.
    FIFOF#(SlotCfgReq) cfgReqQ <- mkSizedFIFOF(2);
    FIFOF#(TxCfgResp) cfgRespQ <- mkSizedFIFOF(2);

    rule handleQuery;
        let req = queryReqQ.first;
        TxSlotIndex index = truncate(req.slotId);
        dynamicAssert(req.slotId < 18'd16, "TX MVP slot index out of range");
        dynamicAssert(entries[index].valid == 1, "TX MVP queried invalid slot");
        queryReqQ.deq;
        queryRespQ.enq(SlotQueryResp {
            slotId: req.slotId,
            entry: entries[index]
        });
    endrule

    rule handleConfig;
        let req = cfgReqQ.first;
        cfgReqQ.deq;
        entries[req.index] <= req.entry;
        cfgRespQ.enq(TxCfgResp { success: 1 });
    endrule

    interface query = toServerP(toPipeIn(queryReqQ), toPipeOut(queryRespQ));
    interface cfg = toServerP(toPipeIn(cfgReqQ), toPipeOut(cfgRespQ));
endmodule

interface MrTableIfc;
    interface ServerP#(MrQueryReq, MrQueryResp) query;
    interface ServerP#(MrCfgReq, TxCfgResp) cfg;
endinterface

module mkMrTable(MrTableIfc);
    // Backing state.
    Vector#(16, Reg#(MrEntry)) entries <- replicateM(mkReg(unpack(0)));

    // Data-plane query service: request and ordered response.
    FIFOF#(MrQueryReq) queryReqQ <- mkSizedFIFOF(2);
    FIFOF#(MrQueryResp) queryRespQ <- mkSizedFIFOF(2);

    // Control-plane configuration service: request and commit response.
    FIFOF#(MrCfgReq) cfgReqQ <- mkSizedFIFOF(2);
    FIFOF#(TxCfgResp) cfgRespQ <- mkSizedFIFOF(2);

    rule handleQuery;
        let req = queryReqQ.first;
        TxMrLookupIndex logicalIndex = txLocalLkeyMrIndex(req.localLkey);
        Bit#(4) physicalIndex = truncate(logicalIndex);
        dynamicAssert(logicalIndex < 13'd16, "TX MVP MR index out of range");
        dynamicAssert(entries[physicalIndex].valid == 1, "TX MVP queried invalid MR");
        queryReqQ.deq;
        queryRespQ.enq(MrQueryResp {
            localLkey: req.localLkey,
            entry: entries[physicalIndex]
        });
    endrule

    rule handleConfig;
        let req = cfgReqQ.first;
        Bit#(4) physicalIndex = truncate(req.index);
        dynamicAssert(req.index < 13'd16, "TX MVP MR config index out of range");
        cfgReqQ.deq;
        entries[physicalIndex] <= req.entry;
        cfgRespQ.enq(TxCfgResp { success: 1 });
    endrule

    interface query = toServerP(toPipeIn(queryReqQ), toPipeOut(queryRespQ));
    interface cfg = toServerP(toPipeIn(cfgReqQ), toPipeOut(cfgRespQ));
endmodule

interface QpStateIfc;
    interface ServerP#(QpReserveReq, QpReserveResp) reserve;
    interface ServerP#(QpCfgReq, TxCfgResp) cfg;
endinterface

module mkQpState(QpStateIfc);
    // Backing state: immutable QP configuration plus mutable TX PSN state.
    Vector#(4, Reg#(QpEntry)) entries <- replicateM(mkReg(unpack(0)));
    Vector#(4, Reg#(TxPsn)) nextPsn <- replicateM(mkReg(0));

    // Data-plane reservation service: request and ordered response.
    FIFOF#(QpReserveReq) reserveReqQ <- mkSizedFIFOF(2);
    FIFOF#(QpReserveResp) reserveRespQ <- mkSizedFIFOF(2);

    // Control-plane configuration service: request and commit response.
    FIFOF#(QpCfgReq) cfgReqQ <- mkSizedFIFOF(2);
    FIFOF#(TxCfgResp) cfgRespQ <- mkSizedFIFOF(2);

    rule handleReserve (!cfgReqQ.notEmpty);
        let req = reserveReqQ.first;
        TxQpIndex index = truncate(req.qpn);
        QpEntry entry = entries[index];
        TxPsnCount count = txPacketCount(req.len, entry.pmtu);
        TxPsn firstPsn = nextPsn[index];

        dynamicAssert(req.qpn < 24'd4, "TX MVP QPN out of range");
        dynamicAssert(entry.valid == 1, "TX MVP queried invalid QP");
        dynamicAssert(txPmtuIsValid(entry.pmtu), "TX MVP invalid PMTU");
        reserveReqQ.deq;
        reserveRespQ.enq(QpReserveResp {
            qpn: req.qpn,
            pmtu: entry.pmtu,
            startPsn: firstPsn,
            psnCount: count
        });
        nextPsn[index] <= firstPsn + zeroExtend(count);
    endrule

    rule handleConfig;
        let req = cfgReqQ.first;
        cfgReqQ.deq;
        entries[req.index] <= req.entry;
        nextPsn[req.index] <= req.entry.initialPsn;
        cfgRespQ.enq(TxCfgResp { success: 1 });
    endrule

    interface reserve = toServerP(toPipeIn(reserveReqQ), toPipeOut(reserveRespQ));
    interface cfg = toServerP(toPipeIn(cfgReqQ), toPipeOut(cfgRespQ));
endmodule

interface PgtTableIfc;
    interface ServerP#(PgtQueryReq, PgtQueryResp) query;
    interface ServerP#(PgtCfgReq, TxCfgResp) cfg;
endinterface

module mkPgtTable(PgtTableIfc);
    // Backing state.
    Vector#(128, Reg#(PteEntry)) entries <- replicateM(mkReg(unpack(0)));

    // Data-plane query service: request and ordered response.
    FIFOF#(PgtQueryReq) queryReqQ <- mkSizedFIFOF(2);
    FIFOF#(PgtQueryResp) queryRespQ <- mkSizedFIFOF(2);

    // Control-plane configuration service: request and commit response.
    FIFOF#(PgtCfgReq) cfgReqQ <- mkSizedFIFOF(2);
    FIFOF#(TxCfgResp) cfgRespQ <- mkSizedFIFOF(2);

    rule handleQuery;
        let req = queryReqQ.first;
        TxPteIndex index = truncate(req.pteIndex);
        dynamicAssert(req.pteIndex < 64'd128, "TX MVP PGT index out of range");
        dynamicAssert(entries[index].valid == 1, "TX MVP queried invalid PTE");
        queryReqQ.deq;
        queryRespQ.enq(PgtQueryResp {
            pteIndex: req.pteIndex,
            entry: entries[index]
        });
    endrule

    rule handleConfig;
        let req = cfgReqQ.first;
        cfgReqQ.deq;
        entries[req.index] <= req.entry;
        cfgRespQ.enq(TxCfgResp { success: 1 });
    endrule

    interface query = toServerP(toPipeIn(queryReqQ), toPipeOut(queryRespQ));
    interface cfg = toServerP(toPipeIn(cfgReqQ), toPipeOut(cfgRespQ));
endmodule

endpackage
