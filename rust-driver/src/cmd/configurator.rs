use std::{io, net::Ipv4Addr};

use parking_lot::Mutex;

use crate::{
    constants::CARD_MAC_ADDRESS,
    mem::DmaBuf,
    net::config::NetworkConfig,
    ring::{
        buffer::{desc_ring::DmaBuffer, ConsumerRingDefault, ProducerRingDefault},
        descriptors::cmd::CmdQueueDesc,
        spec::{cmd_req_ring, cmd_resp_ring, CmdReqSpec, CmdRespSpec},
        traits::DeviceAdaptor,
    },
};

use super::{
    types::{
        CmdQueueReqDescQpManagement, CmdQueueReqDescSetNetworkParam,
        CmdQueueReqDescSetRawPacketReceiveMeta, CmdQueueReqDescUpdateMrTable,
        CmdQueueReqDescUpdatePGT,
    },
    MttUpdate, PgtUpdate, RecvBufferMeta, UpdateQp,
};

/// Controller of the command queue
pub(crate) struct CommandConfigurator<Dev: DeviceAdaptor> {
    /// Command queue pair
    cmd_qp: Mutex<CmdQp<Dev>>,
}

impl<Dev: DeviceAdaptor> CommandConfigurator<Dev> {
    /// Creates a new command controller instance
    ///
    /// # Returns
    /// A new `CommandConfigurator` with an initialized command queue
    pub(crate) fn init(dev: &Dev, req_buf: DmaBuf, resp_buf: DmaBuf) -> io::Result<Self> {
        let req_csr_ring = cmd_req_ring(dev.clone());
        let resp_csr_ring = cmd_resp_ring(dev.clone());
        let tx_ring =
            ProducerRingDefault::new(DmaBuffer::new_for_spec::<CmdReqSpec>(req_buf), req_csr_ring)
                .unwrap();
        let rx_ring = ConsumerRingDefault::new(
            DmaBuffer::new_for_spec::<CmdRespSpec>(resp_buf),
            resp_csr_ring,
        )
        .unwrap();

        Ok(Self {
            cmd_qp: Mutex::new(CmdQp::new(tx_ring, rx_ring)),
        })
    }
}

impl<Dev: DeviceAdaptor> CommandConfigurator<Dev> {
    pub(crate) fn update_mtt(&self, update: MttUpdate) {
        let update_mr_table = CmdQueueReqDescUpdateMrTable::new(
            0,
            update.mr_base_va,
            update.mr_length,
            update.mr_key,
            update.pd_handler,
            update.acc_flags,
            update.base_pgt_offset,
        );
        let mut qp = self.cmd_qp.lock();
        let mut qp_update = qp.update();
        qp_update.push(CmdQueueDesc::UpdateMrTable(update_mr_table));
        // qp_update.flush(&self.req_csr_ring);
        qp_update.wait();
    }

    pub(crate) fn update_pgt(&self, update: PgtUpdate) {
        let desc = CmdQueueReqDescUpdatePGT::new(
            0,
            update.dma_addr,
            update.pgt_offset,
            update.zero_based_entry_count,
        );
        let mut qp = self.cmd_qp.lock();
        let mut qp_update = qp.update();
        qp_update.push(CmdQueueDesc::UpdatePGT(desc));
        // qp_update.flush(&self.req_csr_ring);
        qp_update.wait();
    }

    pub(crate) fn update_qp(&self, entry: UpdateQp) {
        let desc = CmdQueueReqDescQpManagement::new(
            0,
            entry.ip_addr,
            entry.qpn,
            false,
            true,
            entry.peer_qpn,
            entry.rq_access_flags,
            entry.qp_type,
            entry.pmtu,
            entry.local_udp_port,
            entry.peer_mac_addr,
        );

        let mut qp = self.cmd_qp.lock();
        let mut update = qp.update();
        update.push(CmdQueueDesc::ManageQP(desc));
        // update.flush(&self.req_csr_ring);
        update.wait();
    }

    pub(crate) fn set_network(&self, param: NetworkConfig) {
        let desc = CmdQueueReqDescSetNetworkParam::new(
            0,
            param.gateway.map_or(0, Ipv4Addr::to_bits),
            param.ip.mask().to_bits(),
            param.ip.ip().to_bits(),
            CARD_MAC_ADDRESS,
        );
        let mut qp = self.cmd_qp.lock();
        let mut update = qp.update();
        update.push(CmdQueueDesc::SetNetworkParam(desc));
        // update.flush(&self.req_csr_ring);
        update.wait();
    }

    pub(crate) fn set_raw_packet_recv_buffer(&self, meta: RecvBufferMeta) {
        let desc = CmdQueueReqDescSetRawPacketReceiveMeta::new(0, meta.phys_addr);
        let mut qp = self.cmd_qp.lock();
        let mut update = qp.update();
        update.push(CmdQueueDesc::SetRawPacketReceiveMeta(desc));
        // update.flush(&self.req_csr_ring);
        update.wait();
    }
}

/// Command queue pair
struct CmdQp<Dev: DeviceAdaptor> {
    /// The command request queue
    req_queue: ProducerRingDefault<Dev, CmdReqSpec>,
    /// The command response queue
    resp_queue: ConsumerRingDefault<Dev, CmdRespSpec>,
}

impl<Dev: DeviceAdaptor> CmdQp<Dev> {
    /// Creates a new command queue pair
    fn new(
        req_queue: ProducerRingDefault<Dev, CmdReqSpec>,
        resp_queue: ConsumerRingDefault<Dev, CmdRespSpec>,
    ) -> Self {
        Self {
            req_queue,
            resp_queue,
        }
    }

    /// Creates a queue pair update handle to process commands
    fn update(&mut self) -> QpUpdate<'_, Dev> {
        QpUpdate {
            num: 0,
            req_queue: &mut self.req_queue,
            resp_queue: &mut self.resp_queue,
        }
    }
}

/// An updates handle
struct QpUpdate<'a, Dev: DeviceAdaptor> {
    /// Number of updates
    num: usize,
    /// The command request queue
    req_queue: &'a mut ProducerRingDefault<Dev, CmdReqSpec>,
    /// The command response queue
    resp_queue: &'a mut ConsumerRingDefault<Dev, CmdRespSpec>,
}

impl<Dev: DeviceAdaptor> QpUpdate<'_, Dev> {
    /// Pushes a new command queue descriptor to the queue.
    fn push(&mut self, desc: CmdQueueDesc) {
        self.num = self.num.wrapping_add(1);
        //FIXME: handle failed condition
        let result = self.req_queue.try_push_atomic(&[desc]).unwrap();
        assert!(result, "failed to push command descriptor");
    }

    // /// Flushes the command queue by writing the head pointer to the CSR ring.
    // fn flush(&mut self, req_csr_ring: &CmdReqRingCsr<Dev>) {
    //     let _ = req_csr_ring.write_head(self.req_queue.head());
    // }

    /// Waits for responses to all pushed commands.
    fn wait(mut self) {
        while self.num != 0 {
            // TODO : 不应该自旋阻塞
            if let Some(_resp) = self.resp_queue.try_pop().unwrap() {
                self.num = self.num.wrapping_sub(1);
            }
        }
    }
}
