use bilge::prelude::*;

use crate::impl_desc_serde;
use crate::ring::traits::{DescDeserialize, DescSerialize, FromRingBytes};
use crate::types::{PhysAddr, VirtAddr};

use super::RingBufDescCommonHead;
use crate::ring::traits::ToRingBytes;

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
#[repr(u8)]
pub(crate) enum CmdQueueDescOperators {
    UpdateMrTable = 0x00,
    UpdatePgt = 0x01,
    ManageQp = 0x02,
    SetNetworkParam = 0x03,
    SetRawPacketReceiveMeta = 0x04,
}

#[bitsize(16)]
#[derive(Clone, Copy, DebugBits, FromBits)]
pub(crate) struct RingbufDescCmdQueueCommonHead {
    pub user_data: u8,
    pub is_success: bool,
    reserved1: u7,
}

impl RingbufDescCmdQueueCommonHead {
    fn new_with_user_data(user_data: u8) -> Self {
        let mut this: Self = 0u16.into();
        this.set_user_data(user_data);
        this
    }
}

#[bitsize(32)]
#[derive(Clone, Copy, DebugBits, FromBits)]
pub(crate) struct CmdQueueReqDescHeaderChunk {
    pub cmd_queue_common_header: RingbufDescCmdQueueCommonHead,
    pub common_header: RingBufDescCommonHead,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescUpdateMrTableChunk0 {
    reserved2: u7,
    pub pgt_offset: u17,
    pub acc_flags: u8,
    pub reserved1: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescUpdateMrTableChunk1 {
    pub mr_key: u32,
    pub mr_length: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescUpdateMrTableChunk2 {
    pub mr_base_va: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescUpdateMrTableChunk3 {
    reserved0: u32,
    headers: CmdQueueReqDescHeaderChunk,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct CmdQueueReqDescUpdateMrTable {
    c0: CmdQueueReqDescUpdateMrTableChunk0,
    c1: CmdQueueReqDescUpdateMrTableChunk1,
    c2: CmdQueueReqDescUpdateMrTableChunk2,
    c3: CmdQueueReqDescUpdateMrTableChunk3,
}

impl CmdQueueReqDescUpdateMrTable {
    pub(crate) fn new(
        user_data: u8,
        mr_base_va: VirtAddr,
        mr_length: u32,
        mr_key: u32,
        pd_handler: u32,
        acc_flags: u8,
        pgt_offset: u32,
    ) -> Self {
        let common_header =
            RingBufDescCommonHead::new_cmd_desc(CmdQueueDescOperators::UpdateMrTable);
        let cmd_queue_common_header = RingbufDescCmdQueueCommonHead::new_with_user_data(user_data);
        let header = CmdQueueReqDescHeaderChunk::new(cmd_queue_common_header, common_header);
        let c3 = CmdQueueReqDescUpdateMrTableChunk3::new(0, header);
        let c2 = CmdQueueReqDescUpdateMrTableChunk2::new(mr_base_va.as_u64());
        let c1 = CmdQueueReqDescUpdateMrTableChunk1::new(mr_key, mr_length);
        let c0 = CmdQueueReqDescUpdateMrTableChunk0::new(
            u7::from_u8(0),
            u17::from_u32(pgt_offset),
            acc_flags,
            pd_handler,
        );

        Self { c0, c1, c2, c3 }
    }

    pub(crate) fn headers(&self) -> CmdQueueReqDescHeaderChunk {
        self.c3.headers()
    }
    pub(crate) fn set_headers(&mut self, headers: CmdQueueReqDescHeaderChunk) {
        self.c3.set_headers(headers);
    }
    pub(crate) fn mr_base_va(&self) -> u64 {
        self.c2.mr_base_va()
    }
    pub(crate) fn set_mr_base_va(&mut self, val: u64) {
        self.c2.set_mr_base_va(val);
    }
    pub(crate) fn mr_length(&self) -> u32 {
        self.c1.mr_length()
    }
    pub(crate) fn set_mr_length(&mut self, val: u32) {
        self.c1.set_mr_length(val);
    }
    pub(crate) fn mr_key(&self) -> u32 {
        self.c1.mr_key()
    }
    pub(crate) fn set_mr_key(&mut self, val: u32) {
        self.c1.set_mr_key(val);
    }
    pub(crate) fn acc_flags(&self) -> u8 {
        self.c0.acc_flags()
    }
    pub(crate) fn set_acc_flags(&mut self, val: u8) {
        self.c0.set_acc_flags(val);
    }
    pub(crate) fn pgt_offset(&self) -> u32 {
        self.c0.pgt_offset().into()
    }
    pub(crate) fn set_pgt_offset(&mut self, val: u32) {
        self.c0.set_pgt_offset(u17::masked_new(val));
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
pub(crate) struct CmdQueueReqDescUpdatePGTChunk0 {
    reserved0: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
pub(crate) struct CmdQueueReqDescUpdatePGTChunk1 {
    zero_based_entry_count: u32,
    start_index: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
pub(crate) struct CmdQueueReqDescUpdatePGTChunk2 {
    dma_addr: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
pub(crate) struct CmdQueueReqDescUpdatePGTChunk3 {
    reserved0: u32,
    headers: CmdQueueReqDescHeaderChunk,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct CmdQueueReqDescUpdatePGT {
    c0: CmdQueueReqDescUpdatePGTChunk0,
    c1: CmdQueueReqDescUpdatePGTChunk1,
    c2: CmdQueueReqDescUpdatePGTChunk2,
    c3: CmdQueueReqDescUpdatePGTChunk3,
}

impl CmdQueueReqDescUpdatePGT {
    pub(crate) fn new(
        user_data: u8,
        dma_addr: PhysAddr,
        start_index: u32,
        zero_based_entry_count: u32,
    ) -> Self {
        let common_header = RingBufDescCommonHead::new_cmd_desc(CmdQueueDescOperators::UpdatePgt);
        let cmd_queue_common_header = RingbufDescCmdQueueCommonHead::new_with_user_data(user_data);
        let headers = CmdQueueReqDescHeaderChunk::new(cmd_queue_common_header, common_header);
        let c3 = CmdQueueReqDescUpdatePGTChunk3::new(0, headers);
        let c2 = CmdQueueReqDescUpdatePGTChunk2::new(dma_addr.as_u64());
        let c1 = CmdQueueReqDescUpdatePGTChunk1::new(zero_based_entry_count, start_index);
        let c0 = CmdQueueReqDescUpdatePGTChunk0::new(0);

        Self { c0, c1, c2, c3 }
    }

    pub(crate) fn headers(&self) -> CmdQueueReqDescHeaderChunk {
        self.c3.headers()
    }
    pub(crate) fn set_headers(&mut self, headers: CmdQueueReqDescHeaderChunk) {
        self.c3.set_headers(headers);
    }
    pub(crate) fn dma_addr(&self) -> u64 {
        self.c2.dma_addr()
    }
    pub(crate) fn set_dma_addr(&mut self, val: u64) {
        self.c2.set_dma_addr(val);
    }
    pub(crate) fn start_index(&self) -> u32 {
        self.c1.start_index()
    }
    pub(crate) fn set_start_index(&mut self, val: u32) {
        self.c1.set_start_index(val);
    }
    pub(crate) fn zero_based_entry_count(&self) -> u32 {
        self.c1.zero_based_entry_count()
    }
    pub(crate) fn set_zero_based_entry_count(&mut self, val: u32) {
        self.c1.set_zero_based_entry_count(val);
    }
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct CmdQueueRespDescOnlyCommonHeader {
    rest: [u8; 28],
    header: CmdQueueReqDescHeaderChunk,
}

impl CmdQueueRespDescOnlyCommonHeader {
    /// Creates a new `CmdQueueReqDescUpdateMrTable` response
    pub(crate) fn new_cmd_queue_resp_desc_update_mr_table(user_data: u8) -> Self {
        let common_header =
            RingBufDescCommonHead::new_cmd_desc(CmdQueueDescOperators::UpdateMrTable);
        let cmd_queue_common_header = RingbufDescCmdQueueCommonHead::new_with_user_data(user_data);
        let header = CmdQueueReqDescHeaderChunk::new(cmd_queue_common_header, common_header);
        Self {
            header,
            rest: [0; 28],
        }
    }

    /// Creates a new `CmdQueueReqDescUpdatePGT` response
    pub(crate) fn new_cmd_queue_resp_desc_update_pgt(user_data: u8) -> Self {
        let common_header = RingBufDescCommonHead::new_cmd_desc(CmdQueueDescOperators::UpdatePgt);
        let cmd_queue_common_header = RingbufDescCmdQueueCommonHead::new_with_user_data(user_data);
        let header = CmdQueueReqDescHeaderChunk::new(cmd_queue_common_header, common_header);
        Self {
            header,
            rest: [0; 28],
        }
    }

    pub(crate) fn headers(&self) -> CmdQueueReqDescHeaderChunk {
        self.header
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescQpManagementChunk0 {
    pub peer_mac_addr: u48,
    pub local_udp_port: u16,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescQpManagementChunk1 {
    reserved4: u16,
    reserved3: u5,
    pub pmtu: u3,
    reserved2: u4,
    pub qp_type: u4,
    pub rq_access_flags: u8,
    pub peer_qpn: u24,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescQpManagementChunk2 {
    reserved1: u32,
    pub qpn: u24,
    reserved0: u6,
    pub is_error: bool,
    pub is_valid: bool,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescQpManagementChunk3 {
    pub ip_addr: u32,
    pub cmd_queue_common_header: RingbufDescCmdQueueCommonHead,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct CmdQueueReqDescQpManagement {
    c0: CmdQueueReqDescQpManagementChunk0,
    c1: CmdQueueReqDescQpManagementChunk1,
    c2: CmdQueueReqDescQpManagementChunk2,
    c3: CmdQueueReqDescQpManagementChunk3,
}

impl CmdQueueReqDescQpManagement {
    #[allow(clippy::too_many_arguments)] // FIXME: use builder
    pub(crate) fn new(
        user_data: u8,
        ip_addr: u32,
        qpn: u32,
        is_error: bool,
        is_valid: bool,
        peer_qpn: u32,
        rq_access_flags: u8,
        qp_type: u8,
        pmtu: u8,
        local_udp_port: u16,
        peer_mac_addr: u64,
    ) -> Self {
        let common_header = RingBufDescCommonHead::new_cmd_desc(CmdQueueDescOperators::ManageQp);
        let cmd_queue_common_header = RingbufDescCmdQueueCommonHead::new_with_user_data(user_data);
        let c3 =
            CmdQueueReqDescQpManagementChunk3::new(ip_addr, cmd_queue_common_header, common_header);
        let c2 = CmdQueueReqDescQpManagementChunk2::new(
            0,
            u24::masked_new(qpn),
            u6::from_u8(0),
            is_error,
            is_valid,
        );
        let c1 = CmdQueueReqDescQpManagementChunk1::new(
            0,
            u5::from_u8(0),
            u3::masked_new(pmtu),
            u4::from_u8(0),
            u4::masked_new(qp_type),
            rq_access_flags,
            u24::masked_new(peer_qpn),
        );
        let c0 =
            CmdQueueReqDescQpManagementChunk0::new(u48::masked_new(peer_mac_addr), local_udp_port);

        Self { c0, c1, c2, c3 }
    }

    pub(crate) fn cmd_queue_common_header(&self) -> RingbufDescCmdQueueCommonHead {
        self.c3.cmd_queue_common_header()
    }

    pub(crate) fn set_cmd_queue_common_header(&mut self, val: RingbufDescCmdQueueCommonHead) {
        self.c3.set_cmd_queue_common_header(val);
    }

    pub(crate) fn ip_addr(&self) -> u32 {
        self.c3.ip_addr()
    }

    pub(crate) fn set_ip_addr(&mut self, val: u32) {
        self.c3.set_ip_addr(val);
    }

    pub(crate) fn qpn(&self) -> u32 {
        self.c2.qpn().into()
    }

    pub(crate) fn set_qpn(&mut self, val: u32) {
        self.c2.set_qpn(u24::masked_new(val));
    }

    pub(crate) fn is_error(&self) -> bool {
        self.c2.is_error()
    }

    pub(crate) fn set_is_error(&mut self, val: bool) {
        self.c2.set_is_error(val);
    }

    pub(crate) fn is_valid(&self) -> bool {
        self.c2.is_valid()
    }

    pub(crate) fn set_is_valid(&mut self, val: bool) {
        self.c2.set_is_valid(val);
    }

    pub(crate) fn peer_qpn(&self) -> u32 {
        self.c1.peer_qpn().into()
    }

    pub(crate) fn set_peer_qpn(&mut self, val: u32) {
        self.c1.set_peer_qpn(u24::masked_new(val));
    }

    pub(crate) fn rq_access_flags(&self) -> u8 {
        self.c1.rq_access_flags()
    }

    pub(crate) fn set_rq_access_flags(&mut self, val: u8) {
        self.c1.set_rq_access_flags(val);
    }

    pub(crate) fn qp_type(&self) -> u8 {
        self.c1.qp_type().into()
    }

    pub(crate) fn set_qp_type(&mut self, val: u8) {
        self.c1.set_qp_type(u4::masked_new(val));
    }

    pub(crate) fn pmtu(&self) -> u8 {
        self.c1.pmtu().into()
    }

    pub(crate) fn set_pmtu(&mut self, val: u8) {
        self.c1.set_pmtu(u3::masked_new(val));
    }

    pub(crate) fn local_udp_port(&self) -> u16 {
        self.c0.local_udp_port()
    }

    pub(crate) fn set_local_udp_port(&mut self, val: u16) {
        self.c0.set_local_udp_port(val);
    }

    pub(crate) fn peer_mac_addr(&self) -> u64 {
        self.c0.peer_mac_addr().into()
    }

    pub(crate) fn set_peer_mac_addr(&mut self, val: u64) {
        self.c0.set_peer_mac_addr(u48::masked_new(val));
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetNetworkParamChunk0 {
    reserved2: u16,
    pub mac_addr: u48,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetNetworkParamChunk1 {
    reserved1: u32,
    pub ip_addr: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetNetworkParamChunk2 {
    pub netmask: u32,
    pub gateway: u32,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetNetworkParamChunk3 {
    reserved0: u32,
    pub cmd_queue_common_header: RingbufDescCmdQueueCommonHead,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct CmdQueueReqDescSetNetworkParam {
    c0: CmdQueueReqDescSetNetworkParamChunk0,
    c1: CmdQueueReqDescSetNetworkParamChunk1,
    c2: CmdQueueReqDescSetNetworkParamChunk2,
    c3: CmdQueueReqDescSetNetworkParamChunk3,
}

impl CmdQueueReqDescSetNetworkParam {
    pub(crate) fn new(
        user_data: u8,
        gateway: u32,
        netmask: u32,
        ip_addr: u32,
        mac_addr: u64,
    ) -> Self {
        let common_header =
            RingBufDescCommonHead::new_cmd_desc(CmdQueueDescOperators::SetNetworkParam);
        let cmd_queue_common_header = RingbufDescCmdQueueCommonHead::new_with_user_data(user_data);
        let c3 =
            CmdQueueReqDescSetNetworkParamChunk3::new(0, cmd_queue_common_header, common_header);
        let c2 = CmdQueueReqDescSetNetworkParamChunk2::new(netmask, gateway);
        let c1 = CmdQueueReqDescSetNetworkParamChunk1::new(0, ip_addr);
        let c0 = CmdQueueReqDescSetNetworkParamChunk0::new(0, u48::masked_new(mac_addr));

        Self { c0, c1, c2, c3 }
    }

    pub(crate) fn cmd_queue_common_header(&self) -> RingbufDescCmdQueueCommonHead {
        self.c3.cmd_queue_common_header()
    }

    pub(crate) fn set_cmd_queue_common_header(&mut self, val: RingbufDescCmdQueueCommonHead) {
        self.c3.set_cmd_queue_common_header(val);
    }

    pub(crate) fn gateway(&self) -> u32 {
        self.c2.gateway()
    }

    pub(crate) fn set_gateway(&mut self, val: u32) {
        self.c2.set_gateway(val);
    }

    pub(crate) fn netmask(&self) -> u32 {
        self.c2.netmask()
    }

    pub(crate) fn set_netmask(&mut self, val: u32) {
        self.c2.set_netmask(val);
    }

    pub(crate) fn ip_addr(&self) -> u32 {
        self.c1.ip_addr()
    }

    pub(crate) fn set_ip_addr(&mut self, val: u32) {
        self.c1.set_ip_addr(val);
    }

    pub(crate) fn mac_addr(&self) -> u64 {
        self.c0.mac_addr().into()
    }

    pub(crate) fn set_mac_addr(&mut self, val: u64) {
        self.c0.set_mac_addr(u48::masked_new(val));
    }
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetRawPacketReceiveMetaChunk0 {
    reserved2: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetRawPacketReceiveMetaChunk1 {
    reserved1: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetRawPacketReceiveMetaChunk2 {
    pub write_base_addr: u64,
}

#[bitsize(64)]
#[derive(Clone, Copy, DebugBits, FromBits)]
struct CmdQueueReqDescSetRawPacketReceiveMetaChunk3 {
    reserved0: u32,
    pub cmd_queue_common_header: RingbufDescCmdQueueCommonHead,
    pub common_header: RingBufDescCommonHead,
}

#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub(crate) struct CmdQueueReqDescSetRawPacketReceiveMeta {
    c0: CmdQueueReqDescSetRawPacketReceiveMetaChunk0,
    c1: CmdQueueReqDescSetRawPacketReceiveMetaChunk1,
    c2: CmdQueueReqDescSetRawPacketReceiveMetaChunk2,
    c3: CmdQueueReqDescSetRawPacketReceiveMetaChunk3,
}

impl CmdQueueReqDescSetRawPacketReceiveMeta {
    pub(crate) fn new(user_data: u8, write_base_addr: PhysAddr) -> Self {
        let common_header =
            RingBufDescCommonHead::new_cmd_desc(CmdQueueDescOperators::SetRawPacketReceiveMeta);
        let cmd_queue_common_header = RingbufDescCmdQueueCommonHead::new_with_user_data(user_data);
        let c3 = CmdQueueReqDescSetRawPacketReceiveMetaChunk3::new(
            0,
            cmd_queue_common_header,
            common_header,
        );
        let c2 = CmdQueueReqDescSetRawPacketReceiveMetaChunk2::new(write_base_addr.as_u64());
        let c1 = CmdQueueReqDescSetRawPacketReceiveMetaChunk1::new(0);
        let c0 = CmdQueueReqDescSetRawPacketReceiveMetaChunk0::new(0);

        Self { c0, c1, c2, c3 }
    }

    pub(crate) fn cmd_queue_common_header(&self) -> RingbufDescCmdQueueCommonHead {
        self.c3.cmd_queue_common_header()
    }

    pub(crate) fn set_cmd_queue_common_header(&mut self, val: RingbufDescCmdQueueCommonHead) {
        self.c3.set_cmd_queue_common_header(val);
    }

    pub(crate) fn write_base_addr(&self) -> u64 {
        self.c2.write_base_addr()
    }

    pub(crate) fn set_write_base_addr(&mut self, val: u64) {
        self.c2.set_write_base_addr(val);
    }
}

impl_desc_serde!(
    CmdQueueReqDescUpdateMrTable,
    CmdQueueReqDescUpdatePGT,
    CmdQueueReqDescQpManagement,
    CmdQueueReqDescSetNetworkParam,
    CmdQueueReqDescSetRawPacketReceiveMeta
);

/// Command queue descriptor types that can be submitted
#[derive(Debug, Clone, Copy)]
pub(crate) enum CmdQueueDesc {
    /// Update first stage table command
    UpdateMrTable(CmdQueueReqDescUpdateMrTable),
    /// Update second stage table command
    UpdatePGT(CmdQueueReqDescUpdatePGT),
    /// Manage Queue Pair operations
    ManageQP(CmdQueueReqDescQpManagement),
    /// Set network parameters
    SetNetworkParam(CmdQueueReqDescSetNetworkParam),
    /// Set metadata for raw packet receive operations
    SetRawPacketReceiveMeta(CmdQueueReqDescSetRawPacketReceiveMeta),
}

impl ToRingBytes for CmdQueueDesc {
    type Bytes = [u8; 32];
    const MAX_DESC_COUNT: usize = 1;

    fn desc_count(&self) -> usize {
        1
    }

    fn encode_to_slice(&self, out: &mut [[u8; 32]]) {
        assert!(out.len() >= self.desc_count());
        out[0] = match self {
            CmdQueueDesc::UpdateMrTable(desc) => desc.serialize(),
            CmdQueueDesc::UpdatePGT(desc) => desc.serialize(),
            CmdQueueDesc::ManageQP(desc) => desc.serialize(),
            CmdQueueDesc::SetNetworkParam(desc) => desc.serialize(),
            CmdQueueDesc::SetRawPacketReceiveMeta(desc) => desc.serialize(),
        };
    }
}

/// Command queue response descriptor type
#[derive(Debug, Clone, Copy)]
pub(crate) struct CmdRespQueueDesc([u8; 32]);

impl FromRingBytes for CmdRespQueueDesc {
    type Bytes = [u8; 32];
    const MAX_DESC_COUNT: usize = 1;

    fn from_bytes(bytes: &[Self::Bytes]) -> Self {
        match bytes.len() {
            1 => CmdRespQueueDesc(bytes[0]),
            _ => unreachable!(),
        }
    }

    fn is_valid(bytes: &Self::Bytes) -> bool {
        // Valid bit is bit 7 of byte 31
        bytes[31] >> 7 == 1
    }

    fn desc_count(_first: &Self::Bytes) -> usize {
        1
    }
}
