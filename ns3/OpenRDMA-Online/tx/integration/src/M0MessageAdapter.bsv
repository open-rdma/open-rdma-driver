// M0MessageAdapter: M0 (TxMvp) request stream -> (DmaChunkReq, MessageContext).
//
// TxMvp emits one TxDmaReadRequest per physical DMA burst, strictly
// message-serial. Each request maps 1:1 to a DmaChunkReq (same burst bounds,
// <=1024 B, physical-4KiB-safe), and the message's FIRST request also carries
// everything the downstream Phase-A chain needs to build a MessageContext:
// start-of-message remote address, PSN window from the QP reservation, and
// (since the messageLength extension) the full message byte length.
//
// The MessageContext is the M0->M2 seam: m0 reserves PSNs and the Phase-A
// packetizer consumes them. m0's grant is header-aware by construction
// (txPacketCount numerically mirrors packetCountForMessage), so the adapter
// cross-checks the carried psnCount against the packetizer's own formula and
// fail-stops on any future drift.
//
// destQpn: TxMvp has no destination-QP concept (MVP is a self-loop WRITE);
// the adapter forwards qpn so the encapsulator emits a consistent BTH.

package M0MessageAdapter;

import Assert :: *;
import FIFOF :: *;

// `TxDmaReadRequest` is owned by M0; the explicit package qualification keeps
// this seam obvious even though Phase-A no longer carries a legacy duplicate.
import CommonTypes :: *;
import DmaTypes :: *;
import TxMvpTypes :: *;
import TxTypes :: *;

interface M0MessageAdapterIfc;
    interface PipeIn#(TxMvpTypes::TxDmaReadRequest) requestIn;
    interface PipeOut#(DmaChunkReq) chunkOut;
    interface PipeOut#(MessageContext) contextOut;
endinterface

module mkM0MessageAdapter(M0MessageAdapterIfc);
    FIFOF#(TxMvpTypes::TxDmaReadRequest) requestQ <- mkSizedFIFOF(2);
    FIFOF#(DmaChunkReq) chunkQ <- mkSizedFIFOF(2);
    FIFOF#(MessageContext) contextQ <- mkSizedFIFOF(2);

    function DmaChunkReq chunkOf(TxMvpTypes::TxDmaReadRequest req);
        return DmaChunkReq {
            physAddr: req.physicalAddr,
            len: zeroExtend(req.len),
            wqeTag: req.requestId,
            chunkSeq: req.dmaSeq,
            isFirst: unpack(req.isFirst),
            isLast: unpack(req.isLast)
        };
    endfunction

    // Message start: emit the chunk and its MessageContext in the same cycle
    // so the downstream synchronizer always sees the pair in FIFO order.
    rule emitMessageStart (requestQ.notEmpty && requestQ.first.isFirst == 1 &&
                           chunkQ.notFull && contextQ.notFull);
        let req = requestQ.first;

        dynamicAssert(req.messageLength != 0, "M0CHAIN_ZERO_MESSAGE_LENGTH");
        dynamicAssert(req.pmtu <= 4, "M0CHAIN_INVALID_PMTU");
        dynamicAssert(zeroExtend(req.psnCount) ==
                packetCountForMessage(req.messageLength, req.pmtu),
            "M0CHAIN_PSN_COUNT_MISMATCH");

        chunkQ.enq(chunkOf(req));
        contextQ.enq(MessageContext {
            requestId: req.requestId,
            qpn: req.qpn,
            destQpn: req.qpn, // MVP self-loop; no destination QP in M0
            slotId: req.slotId,
            debugSeq: req.debugSeq,
            pmtu: req.pmtu,
            startPsn: req.startPsn,
            psnCount: zeroExtend(req.psnCount),
            remoteAddr: req.remoteAddr,
            remoteKey: req.remoteKey,
            len: req.messageLength,
            wireProfile: WIRE_LEGACY_PRIVATE,
            evProfileId: 0,
            mrcMsn: 0,
            mrcMsnValid: 0
        });
        requestQ.deq;
    endrule

    // Continuation bursts: chunk only.
    rule emitContinuation (requestQ.notEmpty && requestQ.first.isFirst == 0 &&
                           chunkQ.notFull);
        let req = requestQ.first;
        chunkQ.enq(chunkOf(req));
        requestQ.deq;
    endrule

    interface requestIn = toPipeIn(requestQ);
    interface chunkOut = toPipeOut(chunkQ);
    interface contextOut = toPipeOut(contextQ);
endmodule

endpackage
