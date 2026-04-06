use std::collections::VecDeque;

use log::debug;

use crate::{
    rdma_utils::{fragmenter::WrPacketFragmenter, psn::Psn, qp::QpTable, types::SendWrRdma},
    workers::{
        send::{QpParams, SendHandle, WorkReqOpCode},
        spawner::SingleThreadTaskWorker,
    },
};

#[derive(Debug, PartialEq, Eq)]
#[allow(variant_size_differences)]
pub(crate) enum PacketRetransmitTask {
    NewWr {
        qpn: u32,
        wr: SendQueueElem,
    },
    RetransmitRange {
        qpn: u32,
        // Inclusive
        psn_low: Psn,
        // Exclusive
        psn_high: Psn,
    },
    RetransmitAll {
        qpn: u32,
    },
    Ack {
        qpn: u32,
        psn: Psn,
    },
}

impl PacketRetransmitTask {
    fn qpn(&self) -> u32 {
        match *self {
            PacketRetransmitTask::RetransmitRange { qpn, .. }
            | PacketRetransmitTask::NewWr { qpn, .. }
            | PacketRetransmitTask::RetransmitAll { qpn }
            | PacketRetransmitTask::Ack { qpn, .. } => qpn,
        }
    }
}

pub(crate) struct PacketRetransmitWorker {
    wr_sender: SendHandle,
    table: QpTable<IbvSendQueue>,
}

impl SingleThreadTaskWorker for PacketRetransmitWorker {
    type Task = PacketRetransmitTask;

    fn process(&mut self, task: Self::Task) {
        let qpn = task.qpn();
        let Some(sq) = self.table.get_qp_mut(qpn) else {
            return;
        };
        match task {
            PacketRetransmitTask::NewWr { wr, .. } => {
                sq.push(wr);
            }
            PacketRetransmitTask::RetransmitRange {
                psn_low, psn_high, ..
            } => {
                debug!("retransmit range, qpn: {qpn}, low: {psn_low}, high: {psn_high}");

                let sqes = sq.range(psn_low, psn_high);
                let packets = sqes
                    .into_iter()
                    .flat_map(|sqe| WrPacketFragmenter::new(sqe.wr(), sqe.qp_param(), sqe.psn()))
                    .skip_while(|x| x.psn < psn_low)
                    .take_while(|x| x.psn < psn_high);
                for mut packet in packets {
                    packet.set_is_retry();
                    log::info!("send RetransmitRange");

                    self.wr_sender.send(packet);
                }
            }
            PacketRetransmitTask::RetransmitAll { qpn } => {
                debug!("retransmit all, qpn: {qpn}");

                let packets = sq
                    .inner
                    .iter()
                    .flat_map(|sqe| WrPacketFragmenter::new(sqe.wr(), sqe.qp_param(), sqe.psn()))
                    .skip_while(|x| x.psn < sq.base_psn);
                for mut packet in packets {
                    packet.set_is_retry();
                    log::info!("send RetransmitAll");
                    self.wr_sender.send(packet);
                }
            }

            PacketRetransmitTask::Ack { psn, .. } => {
                sq.pop_until(psn);
            }
        }
    }

    fn maintainance(&mut self) {}
}

impl PacketRetransmitWorker {
    pub(crate) fn new(wr_sender: SendHandle) -> Self {
        Self {
            wr_sender,
            table: QpTable::new(),
        }
    }
}

#[derive(Default)]
pub(crate) struct IbvSendQueue {
    inner: VecDeque<SendQueueElem>,
    base_psn: Psn,
}

impl IbvSendQueue {
    pub(crate) fn push(&mut self, elem: SendQueueElem) {
        self.inner.push_back(elem);
    }

    pub(crate) fn pop_until(&mut self, psn: Psn) {
        let a = self.inner.partition_point(|x| x.psn < psn);
        let _drop = self.inner.drain(..a.saturating_sub(1));
        self.base_psn = psn;
    }

    /// Find range [`psn_low`, `psn_high`)
    pub(crate) fn range(&self, psn_low: Psn, psn_high: Psn) -> Vec<SendQueueElem> {
        let a = self.inner.partition_point(|x| x.psn < psn_low);
        let b = self.inner.partition_point(|x| x.psn < psn_high);
        if (a..b).is_empty() {
            return Vec::new();
        }
        self.inner.range(a..b).copied().collect()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct SendQueueElem {
    psn: Psn,
    wr: SendWrRdma,
    qp_param: QpParams,
}

impl SendQueueElem {
    pub(crate) fn new(wr: SendWrRdma, psn: Psn, qp_param: QpParams) -> Self {
        Self { psn, wr, qp_param }
    }

    pub(crate) fn psn(&self) -> Psn {
        self.psn
    }

    pub(crate) fn wr(&self) -> SendWrRdma {
        self.wr
    }

    pub(crate) fn qp_param(&self) -> QpParams {
        self.qp_param
    }

    pub(crate) fn opcode(&self) -> WorkReqOpCode {
        self.wr.opcode()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{rdma_utils::types::SendWrBase, workers::send::QpParams};
    use std::sync::{Arc, Mutex};

    // Mock SendHandle for testing
    #[derive(Clone)]
    struct MockSendHandle {
        sent_packets: Arc<Mutex<Vec<String>>>,
    }

    impl MockSendHandle {
        fn new() -> Self {
            Self {
                sent_packets: Arc::new(Mutex::new(Vec::new())),
            }
        }

        fn get_sent_packets(&self) -> Vec<String> {
            self.sent_packets.lock().unwrap().clone()
        }
    }

    // Mock packet type for testing
    struct MockPacket {
        psn: Psn,
        is_retry: bool,
    }

    impl MockPacket {
        fn set_is_retry(&mut self) {
            self.is_retry = true;
        }
    }

    fn create_test_send_wr() -> SendWrRdma {
        use crate::types::{RemoteAddr, VirtAddr};

        let base = SendWrBase {
            wr_id: 1,
            send_flags: 0,
            laddr: VirtAddr::new(0x1000),
            length: 1024,
            lkey: 0x123,
            imm_data: 0,
            opcode: WorkReqOpCode::RdmaWrite,
        };
        SendWrRdma {
            base,
            raddr: RemoteAddr::new(0x2000),
            rkey: 0x456,
        }
    }

    fn create_test_qp_params() -> QpParams {
        QpParams {
            sqpn: 1,
            pmtu: 1,
            dqpn: 2,
            msn: 0,
            qp_type: ibverbs_sys::ibv_qp_type::IBV_QPT_RC as u8,
            mac_addr: 0xAABB_CCDD_EE0A,
            dqp_ip: 13,
        }
    }

    #[test]
    fn test_packet_retransmit_task_qpn() {
        let wr = create_test_send_wr();
        let qp_param = create_test_qp_params();
        let elem = SendQueueElem::new(wr, Psn(100), qp_param);

        let task1 = PacketRetransmitTask::NewWr { qpn: 42, wr: elem };
        assert_eq!(task1.qpn(), 42);

        let task2 = PacketRetransmitTask::RetransmitRange {
            qpn: 123,
            psn_low: Psn(10),
            psn_high: Psn(20),
        };
        assert_eq!(task2.qpn(), 123);

        let task3 = PacketRetransmitTask::RetransmitAll { qpn: 456 };
        assert_eq!(task3.qpn(), 456);

        let task4 = PacketRetransmitTask::Ack {
            qpn: 789,
            psn: Psn(50),
        };
        assert_eq!(task4.qpn(), 789);
    }

    #[test]
    fn test_ibv_send_queue_pop_until() {
        let mut queue = IbvSendQueue::default();
        let wr = create_test_send_wr();
        let qp_param = create_test_qp_params();

        // Add elements with PSNs 100, 200, 300
        queue.push(SendQueueElem::new(wr, Psn(100), qp_param));
        queue.push(SendQueueElem::new(wr, Psn(200), qp_param));
        queue.push(SendQueueElem::new(wr, Psn(300), qp_param));

        // Pop until PSN 250 (should remove elements with PSN < 250)
        queue.pop_until(Psn(250));

        assert_eq!(queue.base_psn, Psn(250));
        // Should keep at least one element (the last one before the PSN)
        assert!(!queue.inner.is_empty());
    }

    #[test]
    fn test_ibv_send_queue_range() {
        let mut queue = IbvSendQueue::default();
        let wr = create_test_send_wr();
        let qp_param = create_test_qp_params();

        queue.push(SendQueueElem::new(wr, Psn(100), qp_param));
        queue.push(SendQueueElem::new(wr, Psn(200), qp_param));
        queue.push(SendQueueElem::new(wr, Psn(300), qp_param));
        queue.push(SendQueueElem::new(wr, Psn(400), qp_param));
        queue.push(SendQueueElem::new(wr, Psn(500), qp_param));

        let range = queue.range(Psn(150), Psn(350));
        assert_eq!(range[0], SendQueueElem::new(wr, Psn(200), qp_param));
        assert_eq!(range[1], SendQueueElem::new(wr, Psn(300), qp_param));

        let range = queue.range(Psn(100), Psn(350));
        assert_eq!(range[0], SendQueueElem::new(wr, Psn(100), qp_param));
        assert_eq!(range[1], SendQueueElem::new(wr, Psn(200), qp_param));
        assert_eq!(range[2], SendQueueElem::new(wr, Psn(300), qp_param));

        let range = queue.range(Psn(100), Psn(400));
        assert_eq!(range[0], SendQueueElem::new(wr, Psn(100), qp_param));
        assert_eq!(range[1], SendQueueElem::new(wr, Psn(200), qp_param));
        assert_eq!(range[2], SendQueueElem::new(wr, Psn(300), qp_param));
    }

    #[test]
    fn test_ibv_send_queue_range_no_overlap() {
        let mut queue = IbvSendQueue::default();
        let wr = create_test_send_wr();
        let qp_param = create_test_qp_params();

        queue.push(SendQueueElem::new(wr, Psn(100), qp_param));
        queue.push(SendQueueElem::new(wr, Psn(200), qp_param));

        let range = queue.range(Psn(300), Psn(400));
        assert!(range.is_empty());

        let range = queue.range(Psn(0), Psn(50));
        assert!(range.is_empty());
    }

    #[test]
    fn test_ibv_send_queue_multiple_operations() {
        let mut queue = IbvSendQueue::default();
        let wr = create_test_send_wr();
        let qp_param = create_test_qp_params();

        for i in 0..5 {
            let elem = SendQueueElem::new(wr, Psn(i * 100), qp_param);
            queue.push(elem);
        }

        assert_eq!(queue.inner.len(), 5);

        let range = queue.range(Psn(150), Psn(350));
        assert!(!range.is_empty());

        queue.pop_until(Psn(250));
        assert_eq!(queue.base_psn, Psn(250));
    }
}
