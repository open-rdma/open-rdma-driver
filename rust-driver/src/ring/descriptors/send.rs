use bilge::prelude::*;

use crate::{
    impl_desc_serde,
    ring::traits::ToRingBytes,
    types::{RemoteAddr, VirtAddr},
    workers::send::WorkReqOpCode,
};

use crate::ring::traits::{DescDeserialize, DescSerialize};

use super::RingBufDescCommonHead;

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg0Chunk0 {
    reserved1: u3,
    pub flags: u5,
    pub dqpn: u24,
    reserved0: u4,
    pub qp_type: u4,
    pub psn: u24,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg0Chunk1 {
    pub raddr: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg0Chunk2 {
    pub dqp_ip: u32,
    pub rkey: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg0Chunk3 {
    pub total_len: u32,
    pub msn: u16,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct SendQueueReqDescSeg0 {
    c0: SendQueueReqDescSeg0Chunk0,
    c1: SendQueueReqDescSeg0Chunk1,
    c2: SendQueueReqDescSeg0Chunk2,
    c3: SendQueueReqDescSeg0Chunk3,
}

impl SendQueueReqDescSeg0 {
    pub(crate) fn new(
        op_code: WorkReqOpCode,
        msn: u16,
        psn: u32,
        qp_type: u8,
        dqpn: u32,
        flags: u8,
        dqp_ip: u32,
        raddr: RemoteAddr,
        rkey: u32,
        total_len: u32,
    ) -> Self {
        Self::new_inner(
            op_code, msn, psn, qp_type, dqpn, flags, dqp_ip, raddr, rkey, total_len,
        )
    }

    pub(crate) fn new_inner(
        op_code: WorkReqOpCode,
        msn: u16,
        psn: u32,
        qp_type: u8,
        dqpn: u32,
        flags: u8,
        dqp_ip: u32,
        raddr: RemoteAddr,
        rkey: u32,
        total_len: u32,
    ) -> Self {
        let mut common_header = RingBufDescCommonHead::new_send_desc(op_code);
        common_header.set_has_next(true);
        let c3 = SendQueueReqDescSeg0Chunk3::new(total_len, msn, common_header);
        let c2 = SendQueueReqDescSeg0Chunk2::new(dqp_ip, rkey);
        let c1 = SendQueueReqDescSeg0Chunk1::new(raddr.as_u64());
        let c0 = SendQueueReqDescSeg0Chunk0::new(
            u3::from_u8(0),
            u5::masked_new(flags),
            u24::masked_new(dqpn),
            u4::from_u8(0),
            u4::masked_new(qp_type),
            u24::masked_new(psn),
        );

        Self { c0, c1, c2, c3 }
    }

    pub(crate) fn msn(&self) -> u16 {
        self.c3.msn()
    }

    pub(crate) fn set_msn(&mut self, val: u16) {
        self.c3.set_msn(val);
    }

    pub(crate) fn psn(&self) -> u32 {
        self.c0.psn().into()
    }

    pub(crate) fn set_psn(&mut self, val: u32) {
        self.c0.set_psn(u24::masked_new(val));
    }

    pub(crate) fn qp_type(&self) -> u8 {
        self.c0.qp_type().into()
    }

    pub(crate) fn set_qp_type(&mut self, val: u8) {
        self.c0.set_qp_type(u4::masked_new(val));
    }

    pub(crate) fn dqpn(&self) -> u32 {
        self.c0.dqpn().into()
    }

    pub(crate) fn set_dqpn(&mut self, val: u32) {
        self.c0.set_dqpn(u24::masked_new(val));
    }

    pub(crate) fn flags(&self) -> u8 {
        self.c0.flags().into()
    }

    pub(crate) fn set_flags(&mut self, val: u8) {
        self.c0.set_flags(u5::masked_new(val));
    }

    pub(crate) fn dqp_ip(&self) -> u32 {
        self.c2.dqp_ip()
    }

    pub(crate) fn set_dqp_ip(&mut self, val: u32) {
        self.c2.set_dqp_ip(val);
    }

    pub(crate) fn raddr(&self) -> u64 {
        self.c1.raddr()
    }

    pub(crate) fn set_raddr(&mut self, val: u64) {
        self.c1.set_raddr(val);
    }

    pub(crate) fn rkey(&self) -> u32 {
        self.c2.rkey()
    }

    pub(crate) fn set_rkey(&mut self, val: u32) {
        self.c2.set_rkey(val);
    }

    pub(crate) fn total_len(&self) -> u32 {
        self.c3.total_len()
    }

    pub(crate) fn set_total_len(&mut self, val: u32) {
        self.c3.set_total_len(val);
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg1Chunk0 {
    pub laddr: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg1Chunk1 {
    pub len: u32,
    pub lkey: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg1Chunk2 {
    pub sqpn_high_16bits: u16,
    pub mac_addr: u48,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct SendQueueReqDescSeg1Chunk3 {
    pub imm: u32,
    pub sqpn_low_8bits: u8,
    reserved0: u1,
    pub enable_ecn: bool,
    pub is_retry: bool,
    pub is_last: bool,
    pub is_first: bool,
    pub pmtu: u3,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct SendQueueReqDescSeg1 {
    c0: SendQueueReqDescSeg1Chunk0,
    c1: SendQueueReqDescSeg1Chunk1,
    c2: SendQueueReqDescSeg1Chunk2,
    c3: SendQueueReqDescSeg1Chunk3,
}

impl SendQueueReqDescSeg1 {
    pub(crate) fn new(
        op_code: WorkReqOpCode,
        pmtu: u8,
        is_first: bool,
        is_last: bool,
        is_retry: bool,
        enable_ecn: bool,
        sqpn: u32,
        imm: u32,
        mac_addr: u64,
        lkey: u32,
        len: u32,
        laddr: VirtAddr,
    ) -> Self {
        Self::new_inner(
            op_code, pmtu, is_first, is_last, is_retry, enable_ecn, sqpn, imm, mac_addr, lkey, len,
            laddr,
        )
    }

    #[allow(clippy::as_conversions, clippy::cast_possible_truncation)] // truncation is expected
                                                                       // behavior
    pub(crate) fn new_inner(
        op_code: WorkReqOpCode,
        pmtu: u8,
        is_first: bool,
        is_last: bool,
        is_retry: bool,
        enable_ecn: bool,
        sqpn: u32,
        imm: u32,
        mac_addr: u64,
        lkey: u32,
        len: u32,
        laddr: VirtAddr,
    ) -> Self {
        let common_header = RingBufDescCommonHead::new_send_desc(op_code);
        let c3 = SendQueueReqDescSeg1Chunk3::new(
            imm,
            sqpn as u8,
            u1::from_u8(0),
            enable_ecn,
            is_retry,
            is_last,
            is_first,
            u3::masked_new(pmtu),
            common_header,
        );
        let c2 = SendQueueReqDescSeg1Chunk2::new((sqpn >> 8) as u16, u48::masked_new(mac_addr));
        let c1 = SendQueueReqDescSeg1Chunk1::new(len, lkey);
        let c0 = SendQueueReqDescSeg1Chunk0::new(laddr.as_u64());

        Self { c0, c1, c2, c3 }
    }

    pub(crate) fn pmtu(&self) -> u8 {
        self.c3.pmtu().into()
    }

    pub(crate) fn set_pmtu(&mut self, val: u8) {
        self.c3.set_pmtu(u3::masked_new(val));
    }

    pub(crate) fn is_first(&self) -> bool {
        self.c3.is_first()
    }

    pub(crate) fn set_is_first(&mut self, val: bool) {
        self.c3.set_is_first(val);
    }

    pub(crate) fn is_last(&self) -> bool {
        self.c3.is_last()
    }

    pub(crate) fn set_is_last(&mut self, val: bool) {
        self.c3.set_is_last(val);
    }

    pub(crate) fn is_retry(&self) -> bool {
        self.c3.is_retry()
    }

    pub(crate) fn set_is_retry(&mut self, val: bool) {
        self.c3.set_is_retry(val);
    }

    pub(crate) fn enable_ecn(&self) -> bool {
        self.c3.enable_ecn()
    }

    pub(crate) fn set_enable_ecn(&mut self, val: bool) {
        self.c3.set_enable_ecn(val);
    }

    pub(crate) fn sqpn_low_8bits(&self) -> u8 {
        self.c3.sqpn_low_8bits()
    }

    pub(crate) fn set_sqpn_low_8bits(&mut self, val: u8) {
        self.c3.set_sqpn_low_8bits(val);
    }

    pub(crate) fn imm(&self) -> u32 {
        self.c3.imm()
    }

    pub(crate) fn set_imm(&mut self, val: u32) {
        self.c3.set_imm(val);
    }

    pub(crate) fn mac_addr(&self) -> u64 {
        self.c2.mac_addr().into()
    }

    pub(crate) fn set_mac_addr(&mut self, val: u64) {
        self.c2.set_mac_addr(u48::masked_new(val));
    }

    pub(crate) fn sqpn_high_16bits(&self) -> u16 {
        self.c2.sqpn_high_16bits()
    }

    pub(crate) fn set_sqpn_high_16bits(&mut self, val: u16) {
        self.c2.set_sqpn_high_16bits(val);
    }

    pub(crate) fn lkey(&self) -> u32 {
        self.c1.lkey()
    }

    pub(crate) fn set_lkey(&mut self, val: u32) {
        self.c1.set_lkey(val);
    }

    pub(crate) fn len(&self) -> u32 {
        self.c1.len()
    }

    pub(crate) fn set_len(&mut self, val: u32) {
        self.c1.set_len(val);
    }

    pub(crate) fn laddr(&self) -> u64 {
        self.c0.laddr()
    }

    pub(crate) fn set_laddr(&mut self, val: u64) {
        self.c0.set_laddr(val);
    }
}

impl_desc_serde!(SendQueueReqDescSeg0, SendQueueReqDescSeg1);

/// Send queue descriptor types that can be submitted
#[derive(Debug, Clone, Copy)]
pub(crate) enum SendQueueDesc {
    /// First segment
    Seg0(SendQueueReqDescSeg0),
    /// Second segment
    Seg1(SendQueueReqDescSeg1),
}

impl ToRingBytes for SendQueueDesc {
    type Bytes = [u8; 32];
    const MAX_DESC_COUNT: usize = 1;

    fn desc_count(&self) -> usize {
        1
    }

    fn encode_to_slice(&self, out: &mut [[u8; 32]]) {
        assert!(out.len() >= self.desc_count());
        out[0] = match self {
            Self::Seg0(s) => s.serialize(),
            Self::Seg1(s) => s.serialize(),
        };
    }
}
