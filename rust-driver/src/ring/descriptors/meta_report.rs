use bilge::prelude::*;

use crate::impl_desc_serde;
use crate::ring::traits::{DescDeserialize, DescSerialize, FromRingBytes};
use crate::workers::meta_report::{HeaderType, PacketPos};

use super::RingBufDescCommonHead;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
enum RdmaOpCode {
    SendFirst = 0x00,
    SendMiddle = 0x01,
    SendLast = 0x02,
    SendLastWithImmediate = 0x03,
    SendOnly = 0x04,
    SendOnlyWithImmediate = 0x05,
    RdmaWriteFirst = 0x06,
    RdmaWriteMiddle = 0x07,
    RdmaWriteLast = 0x08,
    RdmaWriteLastWithImmediate = 0x09,
    RdmaWriteOnly = 0x0a,
    RdmaWriteOnlyWithImmediate = 0x0b,
    RdmaReadRequest = 0x0c,
    RdmaReadResponseFirst = 0x0d,
    RdmaReadResponseMiddle = 0x0e,
    RdmaReadResponseLast = 0x0f,
    RdmaReadResponseOnly = 0x10,
    Acknowledge = 0x11,
    AtomicAcknowledge = 0x12,
    CompareSwap = 0x13,
    FetchAdd = 0x14,
    Resync = 0x15,
    SendLastWithInvalidate = 0x16,
    SendOnlyWithInvalidate = 0x17,
}

impl RdmaOpCode {
    fn from_u8(value: u8) -> Option<Self> {
        let variant = match value {
            0x00 => Self::SendFirst,
            0x01 => Self::SendMiddle,
            0x02 => Self::SendLast,
            0x03 => Self::SendLastWithImmediate,
            0x04 => Self::SendOnly,
            0x05 => Self::SendOnlyWithImmediate,
            0x06 => Self::RdmaWriteFirst,
            0x07 => Self::RdmaWriteMiddle,
            0x08 => Self::RdmaWriteLast,
            0x09 => Self::RdmaWriteLastWithImmediate,
            0x0a => Self::RdmaWriteOnly,
            0x0b => Self::RdmaWriteOnlyWithImmediate,
            0x0c => Self::RdmaReadRequest,
            0x0d => Self::RdmaReadResponseFirst,
            0x0e => Self::RdmaReadResponseMiddle,
            0x0f => Self::RdmaReadResponseLast,
            0x10 => Self::RdmaReadResponseOnly,
            0x11 => Self::Acknowledge,
            0x12 => Self::AtomicAcknowledge,
            0x13 => Self::CompareSwap,
            0x14 => Self::FetchAdd,
            0x15 => Self::Resync,
            0x16 => Self::SendLastWithInvalidate,
            0x17 => Self::SendOnlyWithInvalidate,
            _ => return None,
        };
        Some(variant)
    }

    fn is_packet(self) -> bool {
        matches!(
            self,
            RdmaOpCode::SendFirst
                | RdmaOpCode::SendMiddle
                | RdmaOpCode::SendLast
                | RdmaOpCode::SendLastWithImmediate
                | RdmaOpCode::SendOnly
                | RdmaOpCode::SendOnlyWithImmediate
                | RdmaOpCode::RdmaWriteFirst
                | RdmaOpCode::RdmaWriteMiddle
                | RdmaOpCode::RdmaWriteLast
                | RdmaOpCode::RdmaWriteLastWithImmediate
                | RdmaOpCode::RdmaWriteOnly
                | RdmaOpCode::RdmaWriteOnlyWithImmediate
                | RdmaOpCode::RdmaReadRequest
                | RdmaOpCode::RdmaReadResponseFirst
                | RdmaOpCode::RdmaReadResponseMiddle
                | RdmaOpCode::RdmaReadResponseLast
                | RdmaOpCode::RdmaReadResponseOnly
        )
    }

    fn packet_pos(self) -> Option<PacketPos> {
        match self {
            RdmaOpCode::SendFirst
            | RdmaOpCode::RdmaWriteFirst
            | RdmaOpCode::RdmaReadResponseFirst => Some(PacketPos::First),
            RdmaOpCode::SendMiddle
            | RdmaOpCode::RdmaWriteMiddle
            | RdmaOpCode::RdmaReadResponseMiddle => Some(PacketPos::Middle),
            RdmaOpCode::SendLast
            | RdmaOpCode::SendLastWithImmediate
            | RdmaOpCode::RdmaWriteLast
            | RdmaOpCode::RdmaWriteLastWithImmediate
            | RdmaOpCode::RdmaReadResponseLast
            | RdmaOpCode::SendLastWithInvalidate => Some(PacketPos::Last),
            RdmaOpCode::SendOnly
            | RdmaOpCode::SendOnlyWithImmediate
            | RdmaOpCode::RdmaWriteOnly
            | RdmaOpCode::RdmaWriteOnlyWithImmediate
            | RdmaOpCode::RdmaReadResponseOnly
            | RdmaOpCode::SendOnlyWithInvalidate => Some(PacketPos::Only),
            RdmaOpCode::RdmaReadRequest
            | RdmaOpCode::Acknowledge
            | RdmaOpCode::AtomicAcknowledge
            | RdmaOpCode::CompareSwap
            | RdmaOpCode::FetchAdd
            | RdmaOpCode::Resync => None,
        }
    }

    fn is_ack(self) -> bool {
        matches!(
            self,
            RdmaOpCode::Acknowledge | RdmaOpCode::AtomicAcknowledge
        )
    }
}

/// Meta report queue descriptor types that can be submitted
#[derive(Debug, Clone, Copy)]
pub(crate) enum MetaReportQueueDescFirst {
    /// Basic packet header info
    PacketInfo(MetaReportQueuePacketBasicInfoDesc),
    /// Ack info
    Ack(MetaReportQueueAckDesc),
}

impl MetaReportQueueDescFirst {
    pub(crate) fn has_next(&self) -> bool {
        match *self {
            MetaReportQueueDescFirst::PacketInfo(d) => d.common_header().has_next(),
            MetaReportQueueDescFirst::Ack(d) => d.common_header().has_next(),
        }
    }
}

/// Meta report queue descriptor types that can be submitted
#[derive(Debug, Clone, Copy)]
pub(crate) enum MetaReportQueueDescNext {
    /// Extended info for READ
    ReadInfo(MetaReportQueueReadReqExtendInfoDesc),
    /// Extra Ack info, used for NAK
    AckExtra(MetaReportQueueAckExtraDesc),
}

impl DescDeserialize for MetaReportQueueDescFirst {
    fn deserialize(d: [u8; 32]) -> Self {
        let head = RingBufDescCommonHead::from(u16::from_le_bytes([d[30], d[31]]));
        let rdma_opcode = RdmaOpCode::from_u8(head.op_code())
            .unwrap_or_else(|| unreachable!("invalid opcode, desc: {:?}", d));
        match rdma_opcode {
            op if rdma_opcode.is_packet() => {
                Self::PacketInfo(MetaReportQueuePacketBasicInfoDesc::deserialize(d))
            }
            op if rdma_opcode.is_ack() => Self::Ack(MetaReportQueueAckDesc::deserialize(d)),
            _ => unreachable!("opcode unsupported"),
        }
    }
}

impl DescDeserialize for MetaReportQueueDescNext {
    fn deserialize(d: [u8; 32]) -> Self {
        let head = RingBufDescCommonHead::from(u16::from_le_bytes([d[30], d[31]]));
        let rdma_opcode =
            RdmaOpCode::from_u8(head.op_code()).unwrap_or_else(|| unreachable!("invalid opcode"));
        match rdma_opcode {
            op if rdma_opcode.is_packet() => {
                Self::ReadInfo(MetaReportQueueReadReqExtendInfoDesc::deserialize(d))
            }
            op if rdma_opcode.is_ack() => {
                Self::AckExtra(MetaReportQueueAckExtraDesc::deserialize(d))
            }
            _ => unreachable!("opcode unsupported"),
        }
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueuePacketBasicInfoDescChunk0 {
    pub imm_data: u32,
    pub rkey: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueuePacketBasicInfoDescChunk1 {
    pub raddr: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueuePacketBasicInfoDescChunk2 {
    pub total_len: u32,
    reserved1: u8,
    pub dqpn: u24,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueuePacketBasicInfoDescChunk3 {
    reserved0: u4,
    pub is_retry: bool,
    pub ack_req: bool,
    pub solicited: bool,
    pub ecn_marked: bool,
    pub psn: u24,
    pub msn: u16,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct MetaReportQueuePacketBasicInfoDesc {
    c0: MetaReportQueuePacketBasicInfoDescChunk0,
    c1: MetaReportQueuePacketBasicInfoDescChunk1,
    c2: MetaReportQueuePacketBasicInfoDescChunk2,
    c3: MetaReportQueuePacketBasicInfoDescChunk3,
}

impl MetaReportQueuePacketBasicInfoDesc {
    pub(crate) fn packet_pos(&self) -> PacketPos {
        RdmaOpCode::from_u8(self.c3.common_header().op_code())
            .and_then(RdmaOpCode::packet_pos)
            .unwrap_or_else(|| {
                unreachable!("packet position info should always exists for this descriptor")
            })
    }

    #[allow(clippy::wildcard_enum_match_arm)]
    pub(crate) fn header_type(&self) -> HeaderType {
        let opcode = RdmaOpCode::from_u8(self.c3.common_header().op_code()).unwrap_or_else(|| {
            unreachable!("packet position info should always exists for this descriptor")
        });
        match opcode {
            RdmaOpCode::RdmaWriteFirst
            | RdmaOpCode::RdmaWriteMiddle
            | RdmaOpCode::RdmaWriteLast
            | RdmaOpCode::RdmaWriteOnly => HeaderType::Write,
            RdmaOpCode::RdmaWriteLastWithImmediate | RdmaOpCode::RdmaWriteOnlyWithImmediate => {
                HeaderType::WriteWithImm
            }
            RdmaOpCode::SendFirst
            | RdmaOpCode::SendMiddle
            | RdmaOpCode::SendLast
            | RdmaOpCode::SendOnly => HeaderType::Send,
            RdmaOpCode::SendLastWithImmediate | RdmaOpCode::SendOnlyWithImmediate => {
                HeaderType::SendWithImm
            }
            RdmaOpCode::RdmaReadResponseFirst
            | RdmaOpCode::RdmaReadResponseMiddle
            | RdmaOpCode::RdmaReadResponseLast
            | RdmaOpCode::RdmaReadResponseOnly => HeaderType::ReadResp,
            _ => unreachable!("unsupported header type"),
        }
    }

    pub(crate) fn msn(&self) -> u16 {
        self.c3.msn()
    }

    pub(crate) fn set_msn(&mut self, val: u16) {
        self.c3.set_msn(val);
    }

    pub(crate) fn psn(&self) -> u32 {
        self.c3.psn().into()
    }

    pub(crate) fn set_psn(&mut self, val: u32) {
        self.c3.set_psn(u24::masked_new(val));
    }

    pub(crate) fn ecn_marked(&self) -> bool {
        self.c3.ecn_marked()
    }

    pub(crate) fn set_ecn_marked(&mut self, val: bool) {
        self.c3.set_ecn_marked(val);
    }

    pub(crate) fn solicited(&self) -> bool {
        self.c3.solicited()
    }

    pub(crate) fn set_solicited(&mut self, val: bool) {
        self.c3.set_solicited(val);
    }

    pub(crate) fn ack_req(&self) -> bool {
        self.c3.ack_req()
    }

    pub(crate) fn set_ack_req(&mut self, val: bool) {
        self.c3.set_ack_req(val);
    }

    pub(crate) fn is_retry(&self) -> bool {
        self.c3.is_retry()
    }

    pub(crate) fn set_is_retry(&mut self, val: bool) {
        self.c3.set_is_retry(val);
    }

    pub(crate) fn dqpn(&self) -> u32 {
        self.c2.dqpn().into()
    }

    pub(crate) fn set_dqpn(&mut self, val: u32) {
        self.c2.set_dqpn(u24::masked_new(val));
    }

    pub(crate) fn total_len(&self) -> u32 {
        self.c2.total_len()
    }

    pub(crate) fn set_total_len(&mut self, val: u32) {
        self.c2.set_total_len(val);
    }

    pub(crate) fn raddr(&self) -> u64 {
        self.c1.raddr()
    }

    pub(crate) fn set_raddr(&mut self, val: u64) {
        self.c1.set_raddr(val);
    }

    pub(crate) fn rkey(&self) -> u32 {
        self.c0.rkey()
    }

    pub(crate) fn set_rkey(&mut self, val: u32) {
        self.c0.set_rkey(val);
    }

    pub(crate) fn imm_data(&self) -> u32 {
        self.c0.imm_data()
    }

    pub(crate) fn set_imm_data(&mut self, val: u32) {
        self.c0.set_imm_data(val);
    }

    fn common_header(&self) -> RingBufDescCommonHead {
        self.c3.common_header()
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueReadReqExtendInfoDescChunk0 {
    reserved2: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueReadReqExtendInfoDescChunk1 {
    reserved1: u32,
    pub lkey: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueReadReqExtendInfoDescChunk2 {
    pub laddr: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueReadReqExtendInfoDescChunk3 {
    pub total_len: u32,
    reserved0: u16,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct MetaReportQueueReadReqExtendInfoDesc {
    c0: MetaReportQueueReadReqExtendInfoDescChunk0,
    c1: MetaReportQueueReadReqExtendInfoDescChunk1,
    c2: MetaReportQueueReadReqExtendInfoDescChunk2,
    c3: MetaReportQueueReadReqExtendInfoDescChunk3,
}

impl MetaReportQueueReadReqExtendInfoDesc {
    pub(crate) fn total_len(&self) -> u32 {
        self.c3.total_len()
    }

    pub(crate) fn set_total_len(&mut self, val: u32) {
        self.c3.set_total_len(val);
    }

    pub(crate) fn laddr(&self) -> u64 {
        self.c2.laddr()
    }

    pub(crate) fn set_laddr(&mut self, val: u64) {
        self.c2.set_laddr(val);
    }

    pub(crate) fn lkey(&self) -> u32 {
        self.c1.lkey()
    }

    pub(crate) fn set_lkey(&mut self, val: u32) {
        self.c1.set_lkey(val);
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckDescChunk0 {
    pub now_bitmap_low: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckDescChunk1 {
    pub now_bitmap_high: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckDescChunk2 {
    pub msn: u16,
    pub qpn: u24,
    pub psn_now: u24,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckDescChunk3 {
    reserved2: u8,
    pub psn_before_slide: u24,
    reserved1: u8,
    pub is_packet_lost: bool,
    pub is_window_slided: bool,
    pub is_send_by_driver: bool,
    pub is_send_by_local_hw: bool,
    reserved0: u4,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct MetaReportQueueAckDesc {
    c0: MetaReportQueueAckDescChunk0,
    c1: MetaReportQueueAckDescChunk1,
    c2: MetaReportQueueAckDescChunk2,
    c3: MetaReportQueueAckDescChunk3,
}

impl MetaReportQueueAckDesc {
    pub(crate) fn is_send_by_local_hw(&self) -> bool {
        self.c3.is_send_by_local_hw()
    }

    pub(crate) fn set_is_send_by_local_hw(&mut self, val: bool) {
        self.c3.set_is_send_by_local_hw(val);
    }

    pub(crate) fn is_send_by_driver(&self) -> bool {
        self.c3.is_send_by_driver()
    }

    pub(crate) fn set_is_send_by_driver(&mut self, val: bool) {
        self.c3.set_is_send_by_driver(val);
    }

    pub(crate) fn is_window_slided(&self) -> bool {
        self.c3.is_window_slided()
    }

    pub(crate) fn set_is_window_slided(&mut self, val: bool) {
        self.c3.set_is_window_slided(val);
    }

    pub(crate) fn is_packet_lost(&self) -> bool {
        self.c3.is_packet_lost()
    }

    pub(crate) fn set_is_packet_lost(&mut self, val: bool) {
        self.c3.set_is_packet_lost(val);
    }

    pub(crate) fn psn_before_slide(&self) -> u32 {
        self.c3.psn_before_slide().into()
    }

    pub(crate) fn set_psn_before_slide(&mut self, val: u32) {
        self.c3.set_psn_before_slide(u24::masked_new(val));
    }

    pub(crate) fn psn_now(&self) -> u32 {
        self.c2.psn_now().into()
    }

    pub(crate) fn set_psn_now(&mut self, val: u32) {
        self.c2.set_psn_now(u24::masked_new(val));
    }

    pub(crate) fn qpn(&self) -> u32 {
        self.c2.qpn().into()
    }

    pub(crate) fn set_qpn(&mut self, val: u32) {
        self.c2.set_qpn(u24::masked_new(val));
    }

    pub(crate) fn msn(&self) -> u16 {
        self.c2.msn()
    }

    pub(crate) fn set_msn(&mut self, val: u16) {
        self.c2.set_msn(val);
    }

    pub(crate) fn now_bitmap(&self) -> u128 {
        u128::from(self.c1.now_bitmap_high()) << 64 | u128::from(self.c0.now_bitmap_low())
    }

    pub(crate) fn set_now_bitmap(&mut self, val: u128) {
        self.c0.set_now_bitmap_low(val as u64);
        self.c1.set_now_bitmap_high((val >> 64) as u64);
    }

    fn common_header(&self) -> RingBufDescCommonHead {
        self.c3.common_header()
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckExtraDescChunk0 {
    pub pre_bitmap_low: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckExtraDescChunk1 {
    pub pre_bitmap_high: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckExtraDescChunk2 {
    reserved2: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct MetaReportQueueAckExtraDescChunk3 {
    reserved1: u32,
    reserved0: u16,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct MetaReportQueueAckExtraDesc {
    c0: MetaReportQueueAckExtraDescChunk0,
    c1: MetaReportQueueAckExtraDescChunk1,
    c2: MetaReportQueueAckExtraDescChunk2,
    c3: MetaReportQueueAckExtraDescChunk3,
}

impl MetaReportQueueAckExtraDesc {
    pub(crate) fn pre_bitmap(&self) -> u128 {
        u128::from(self.c1.pre_bitmap_high()) << 64 | u128::from(self.c0.pre_bitmap_low())
    }

    pub(crate) fn set_pre_bitmap(&mut self, val: u128) {
        self.c0.set_pre_bitmap_low(val as u64);
        self.c1.set_pre_bitmap_high((val >> 64) as u64);
    }
}

impl_desc_serde!(
    MetaReportQueuePacketBasicInfoDesc,
    MetaReportQueueReadReqExtendInfoDesc,
    MetaReportQueueAckDesc,
    MetaReportQueueAckExtraDesc
);

pub(crate) enum MetaReportQueueDesc {
    /// Packet info for write operations
    WritePacketInfo(MetaReportQueuePacketBasicInfoDesc),
    /// Packet info for read operations
    ReadPacketInfo(
        (
            MetaReportQueuePacketBasicInfoDesc,
            MetaReportQueueReadReqExtendInfoDesc,
        ),
    ),
    /// Packet info for congestion event
    CnpPacketInfo(MetaReportQueuePacketBasicInfoDesc),
    /// Ack
    Ack(MetaReportQueueAckDesc),
    /// Nak
    Nak((MetaReportQueueAckDesc, MetaReportQueueAckExtraDesc)),
}

impl FromRingBytes for MetaReportQueueDesc {
    type Bytes = [u8; 32];
    const MAX_DESC_COUNT: usize = 2;

    fn from_bytes(bytes: &[Self::Bytes]) -> Self {
        match bytes.len() {
            1 | 2 => {}
            _ => unreachable!(),
        }

        let mut iter = bytes.into_iter().map(|a| *a); // TODO 使用 iter 防止拷贝
        let first: Option<MetaReportQueueDescFirst> = iter.next().map(DescDeserialize::deserialize);
        let next: Option<MetaReportQueueDescNext> = iter.next().map(DescDeserialize::deserialize);

        match (first, next) {
            (Some(MetaReportQueueDescFirst::PacketInfo(d)), None) if d.ecn_marked() => {
                MetaReportQueueDesc::CnpPacketInfo(d)
            }
            (Some(MetaReportQueueDescFirst::PacketInfo(d)), None) => {
                MetaReportQueueDesc::WritePacketInfo(d)
            }
            (Some(MetaReportQueueDescFirst::Ack(d)), None) => MetaReportQueueDesc::Ack(d),
            (
                Some(MetaReportQueueDescFirst::PacketInfo(f)),
                Some(MetaReportQueueDescNext::ReadInfo(n)),
            ) => MetaReportQueueDesc::ReadPacketInfo((f, n)),
            (
                Some(MetaReportQueueDescFirst::Ack(f)),
                Some(MetaReportQueueDescNext::AckExtra(n)),
            ) => MetaReportQueueDesc::Nak((f, n)),
            _ => {
                unreachable!("invalid descriptor format")
            }
        }
    }

    fn is_valid(bytes: &Self::Bytes) -> bool {
        // Valid bit is bit 7 of byte 31
        bytes[31] >> 7 == 1
    }

    fn desc_count(first: &Self::Bytes) -> usize {
        if (first[31] >> 6) & 1 == 1 { 2 } else { 1 }
    }
}
