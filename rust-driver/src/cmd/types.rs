use crate::{
    mem::experimental::page_allocator::ContiguousPages,
    types::{PhysAddr, VirtAddr},
};

// Re-export descriptor types for use in other modules
pub(crate) use crate::ring::descriptors::cmd::{
    CmdQueueReqDescQpManagement, CmdQueueReqDescSetNetworkParam,
    CmdQueueReqDescSetRawPacketReceiveMeta, CmdQueueReqDescUpdateMrTable, CmdQueueReqDescUpdatePGT,
};

/// Command queue for submitting commands to the device
// pub(crate) struct CmdQueue<Dev: DeviceAdaptor> {
//     /// Inner ring buffer
//     inner: ProducerRingDefault<Dev, CmdReqSpec>,
// }

// impl<Dev: DeviceAdaptor> CmdQueue<Dev> {
//     /// Creates a new `CmdQueue`
//     pub(crate) fn new(ring: ProducerRingDefault<Dev, CmdReqSpec>) -> Self {
//         Self { inner: ring }
//     }

//     /// Produces command descriptors to the queue
//     pub(crate) fn push(&mut self, desc: CmdQueueDesc) -> bool {
//         self.inner.try_push(desc).unwrap()
//     }

//     /// Returns the head pointer
//     pub(crate) fn head(&self) -> u32 {
//         self.inner.head()
//     }
// }

// /// Queue for receiving command responses from the device
// pub(crate) struct CmdRespQueue<Dev: DeviceAdaptor> {
//     /// Inner ring buffer
//     inner: ComsumerRingDefault<Dev, CmdRespSpec>,
// }

// impl<Dev: DeviceAdaptor> CmdRespQueue<Dev> {
//     /// Creates a new `CmdRespQueue`
//     pub(crate) fn new(ring: ComsumerRingDefault<Dev, CmdRespSpec>) -> Self {
//         Self { inner: ring }
//     }

//     /// Tries to poll next valid entry from the queue
//     pub(crate) fn try_pop(&mut self) -> Option<CmdRespQueueDesc> {
//         self.inner.try_pop().unwrap()
//     }

//     /// Return tail pointer
//     pub(crate) fn tail(&self) -> u32 {
//         self.inner.tail()
//     }
// }

#[allow(clippy::missing_docs_in_private_items)]
/// Memory Translation Table entry
#[derive(Debug, Clone, Copy)]
pub(crate) struct MttUpdate {
    pub(crate) mr_base_va: VirtAddr,
    pub(crate) mr_length: u32,
    pub(crate) mr_key: u32,
    pub(crate) pd_handler: u32,
    pub(crate) acc_flags: u8,
    pub(crate) base_pgt_offset: u32,
}

impl MttUpdate {
    pub(crate) fn new(
        mr_base_va: VirtAddr,
        mr_length: u32,
        mr_key: u32,
        pd_handler: u32,
        acc_flags: u8,
        base_pgt_offset: u32,
    ) -> Self {
        Self {
            mr_base_va,
            mr_length,
            mr_key,
            pd_handler,
            acc_flags,
            base_pgt_offset,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct PgtUpdate {
    pub(crate) dma_addr: PhysAddr,
    pub(crate) pgt_offset: u32,
    pub(crate) zero_based_entry_count: u32,
}

impl PgtUpdate {
    pub(crate) fn new(dma_addr: PhysAddr, pgt_offset: u32, zero_based_entry_count: u32) -> Self {
        Self {
            dma_addr,
            pgt_offset,
            zero_based_entry_count,
        }
    }
}

/// Queue Pair entry
#[allow(clippy::missing_docs_in_private_items)]
#[derive(Debug, Default, Clone, Copy)]
pub(crate) struct UpdateQp {
    pub(crate) ip_addr: u32,
    pub(crate) qpn: u32,
    pub(crate) peer_qpn: u32,
    pub(crate) rq_access_flags: u8,
    pub(crate) qp_type: u8,
    pub(crate) pmtu: u8,
    pub(crate) local_udp_port: u16,
    pub(crate) peer_mac_addr: u64,
}

/// Receive buffer
pub(crate) struct RecvBuffer {
    /// One page
    inner: ContiguousPages<1>,
}

/// Metadata about a receive buffer
#[derive(Debug, Clone, Copy)]
pub(crate) struct RecvBufferMeta {
    /// Physical address of the receive buffer
    pub(crate) phys_addr: PhysAddr,
}

impl RecvBufferMeta {
    /// Creates a new `RecvBufferMeta`
    pub(crate) fn new(phys_addr: PhysAddr) -> Self {
        Self { phys_addr }
    }
}

impl RecvBuffer {
    /// Creates a new receive buffer from contiguous pages
    pub(crate) fn new(inner: ContiguousPages<1>) -> Self {
        Self { inner }
    }

    /// Gets start address about this receive buffer
    pub(crate) fn addr(&self) -> u64 {
        self.inner.addr()
    }
}
