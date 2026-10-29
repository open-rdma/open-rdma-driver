"""RX BSV wire types mirrored with exact declaration order and widths."""

from __future__ import annotations

from collections import OrderedDict

from cocotb_bsv import Struct
from cocotb_bsv.packing import Bits


def bits(name: str, width: int) -> type[Bits]:
    return type(name, (Bits,), {"_width": width})


Bit1 = bits("Bit1", 1)
Bit2 = bits("Bit2", 2)
Bit4 = bits("Bit4", 4)
Bit5 = bits("Bit5", 5)
Bit6 = bits("Bit6", 6)
Bit7 = bits("Bit7", 7)
Bit8 = bits("Bit8", 8)
Bit13 = bits("Bit13", 13)
Bit16 = bits("Bit16", 16)
Bit17 = bits("Bit17", 17)
Bit18 = bits("Bit18", 18)
Bit24 = bits("Bit24", 24)
Bit25 = bits("Bit25", 25)
Bit32 = bits("Bit32", 32)
Bit64 = bits("Bit64", 64)
Bit128 = bits("Bit128", 128)
Bit256 = bits("Bit256", 256)
Bit1024 = bits("Bit1024", 1024)


class RxWireBeat(Struct):
    _members_def = OrderedDict(data=Bit256, be=Bit32, sof=Bit1, eof=Bit1)


class RxPayloadTag(Struct):
    _members_def = OrderedDict(lane=Bit2, slot=Bit4, generation=Bit8)


class RxBufferedFrame(Struct):
    _members_def = OrderedDict(
        tag=RxPayloadTag,
        header0=Bit256,
        header1=Bit256,
        header2=Bit256,
        bytes=Bit13,
        beats=Bit8,
    )


class RxPayloadReadRequest(Struct):
    _members_def = OrderedDict(tag=RxPayloadTag, wordIndex=Bit8)


class RxPayloadReadResponse(Struct):
    _members_def = OrderedDict(
        tag=RxPayloadTag,
        wordIndex=Bit8,
        low=Bit256,
        high=Bit256,
        valid=Bit1,
    )


class RxBufferRelease(Struct):
    _members_def = OrderedDict(tag=RxPayloadTag)


class RxParsedPacket(Struct):
    _members_def = OrderedDict(
        payloadTag=RxPayloadTag,
        lane=Bit2,
        qpn=Bit24,
        psn=Bit24,
        opcode=Bit8,
        ecn=Bit2,
        payloadStart=Bit7,
        payloadBytes=Bit13,
        remoteAddr=Bit64,
        rkey=Bit32,
        messageBytes=Bit25,
        first=Bit1,
        last=Bit1,
    )


class RxQpConfig(Struct):
    _members_def = OrderedDict(
        valid=Bit1,
        qpn=Bit24,
        initialPsn=Bit24,
        pmtu=bits("Bit3", 3),
        rkey=Bit32,
        virtualBase=Bit64,
        physicalBase=Bit64,
        regionBytes=Bit64,
        remoteWrite=Bit1,
    )


class RxNetworkConfig(Struct):
    _members_def = OrderedDict(
        localMac=bits("Bit48", 48),
        localIp=Bit32,
        peerMac=bits("Bit48b", 48),
        peerIp=Bit32,
    )


class RxError(Struct):
    _members_def = OrderedDict(qpn=Bit24, psn=Bit24, code=Bit5)


class RxFeedback(Struct):
    _members_def = OrderedDict(
        qpn=Bit24,
        kind=Bit2,
        ackPsn=Bit24,
        sackBase=Bit24,
        sackBitmap=Bit64,
        oooCount=Bit7,
        triggerPsn=Bit24,
    )


class RxEcnEvent(Struct):
    _members_def = OrderedDict(lane=Bit2, qpn=Bit24, psn=Bit24, ecn=Bit2)


class RxTxFeedback(Struct):
    _members_def = OrderedDict(qpn=Bit24, psn=Bit24, aeth=Bit32)


class RxAdmittedPacket(Struct):
    _members_def = OrderedDict(
        globalSeq=Bit64,
        payloadTag=RxPayloadTag,
        lane=Bit2,
        qp=Bit2,
        qpn=Bit24,
        psn=Bit24,
        dmaAddr=Bit64,
        payloadStart=Bit7,
        payloadBytes=Bit13,
        messageBytes=Bit25,
        first=Bit1,
        last=Bit1,
        ackValid=Bit1,
        ackPsnAfterMerge=Bit24,
        messageComplete=Bit1,
        messageLastPsn=Bit24,
    )


class RxDmaWriteRequest(Struct):
    _members_def = OrderedDict(
        globalSeq=Bit64,
        epoch=Bit16,
        lane=Bit2,
        qpn=Bit24,
        psn=Bit24,
        addr=Bit64,
        bytes=Bit13,
    )


class RxDmaPayloadBeat(Struct):
    _members_def = OrderedDict(
        globalSeq=Bit64, epoch=Bit16, data=Bit256, be=Bit32, sop=Bit1, eop=Bit1
    )


class RxDmaCompletion(Struct):
    _members_def = OrderedDict(globalSeq=Bit64, epoch=Bit16, lane=Bit2, success=Bit1)


class RxDmaRetire(Struct):
    _members_def = OrderedDict(
        globalSeq=Bit64,
        qp=Bit2,
        qpn=Bit24,
        psn=Bit24,
        success=Bit1,
        ackValid=Bit1,
        ackPsnAfterMerge=Bit24,
        messageComplete=Bit1,
        messageLastPsn=Bit24,
        messageBytes=Bit25,
    )


class RxCompletion(Struct):
    _members_def = OrderedDict(qpn=Bit24, lastPsn=Bit24, bytes=Bit25, success=Bit1)


class RxOrderedFlagIngress(Struct):
    _members_def = OrderedDict(
        qpn=Bit24, barrierPsn=Bit24, flagAddr=Bit64, rkey=Bit32, value=Bit32
    )


class RxFlagDmaRequest(Struct):
    _members_def = OrderedDict(
        orderedSeq=Bit64,
        epoch=Bit16,
        qpn=Bit24,
        barrierPsn=Bit24,
        addr=Bit64,
        value=Bit32,
    )


class RxFlagDmaResponse(Struct):
    _members_def = OrderedDict(orderedSeq=Bit64, epoch=Bit16, success=Bit1)


class RxOrderedCredit(Struct):
    _members_def = OrderedDict(qpn=Bit24, orderedGrantLimit=Bit64)


class RxReceiveProgress(Struct):
    _members_def = OrderedDict(ackPsn=Bit24, receiveBitmap=Bit16, oooCount=Bit7)


class MessageContext(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        qpn=Bit24,
        destQpn=Bit24,
        slotId=Bit18,
        debugSeq=Bit4,
        pmtu=bits("TxPmtuBit3", 3),
        startPsn=Bit24,
        psnCount=Bit17,
        remoteAddr=Bit64,
        remoteKey=Bit32,
        len=Bit25,
    )


class MessagePayloadBeat(Struct):
    _members_def = OrderedDict(
        requestId=Bit16,
        messageOffset=Bit25,
        data=Bit1024,
        keep=Bit128,
        messageSop=Bit1,
        messageEop=Bit1,
    )


class MacIpConfig(Struct):
    _members_def = OrderedDict(
        srcMac=bits("TxSrcMacBit48", 48),
        dstMac=bits("TxDstMacBit48", 48),
        srcIp=Bit32,
        dstIp=Bit32,
    )


class MrcTxCfgReq(Struct):
    _members_def = OrderedDict(cfg=MacIpConfig, channelEnable=Bit4, maxHighBurst=Bit4)


class CfgResp(Struct):
    _members_def = OrderedDict(success=Bit1)


class EthPacket(Struct):
    _members_def = OrderedDict(
        data=Bit256,
        be=Bit32,
        sof=Bit1,
        eof=Bit1,
        requestId=Bit16,
        qpn=Bit24,
        destQpn=Bit24,
        psn=Bit24,
        isFirst=Bit1,
        isLast=Bit1,
        trafficClass=Bit2,
    )


def tag(lane: int, slot: int, generation: int = 0) -> RxPayloadTag:
    return RxPayloadTag(lane=lane, slot=slot, generation=generation)


def assert_struct_equal(expected: Struct, actual: Struct, context: str) -> None:
    for name in expected._members_def:
        lhs = int(getattr(expected, name))
        rhs = int(getattr(actual, name))
        assert rhs == lhs, f"{context}.{name}: expected={lhs:#x} actual={rhs:#x}"
