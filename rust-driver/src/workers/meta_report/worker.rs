use log::{debug, error};

use crate::{
    rdma_utils::{
        psn::Psn,
        psn_tracker::{LocalAckTracker, RemoteAckTracker},
        qp::QpTable,
        types::{SendWrBase, SendWrRdma},
    },
    ring::traits::DeviceAdaptor,
    types::{RemoteAddr, VirtAddr},
    workers::{
        ack_responder::AckResponse,
        completion::{CompletionTask, Event, MessageMeta, RecvEvent, RecvEventOp},
        qp_timeout::AckTimeoutTask,
        rdma::RdmaWriteTask,
        retransmit::PacketRetransmitTask,
        send::WorkReqOpCode,
        spawner::{SingleThreadPollingWorker, TaskTx},
    },
};

use super::types::{
    AckMetaLocalHw, AckMetaRemoteDriver, HeaderReadMeta, HeaderType, HeaderWriteMeta,
    MetaReportQueueHandler, NakMetaLocalHw, NakMetaRemoteDriver, NakMetaRemoteHw, PacketPos,
    ReportMeta,
};

/// A worker for processing packet meta
pub(crate) struct MetaWorker<Dev: DeviceAdaptor> {
    /// Inner meta report queue
    inner: MetaReportQueueHandler<Dev>,
    handler: MetaHandler,
}

impl<Dev: DeviceAdaptor> MetaWorker<Dev> {
    pub(crate) fn new(inner: MetaReportQueueHandler<Dev>, handler: MetaHandler) -> Self {
        Self { inner, handler }
    }
}

impl<Dev: DeviceAdaptor + Send + 'static> SingleThreadPollingWorker for MetaWorker<Dev> {
    type Task = ReportMeta;

    fn poll(&mut self) -> Option<Self::Task> {
        self.inner.try_recv_meta()
    }

    fn process(&mut self, meta: Self::Task) {
        debug!("Kevin MetaWorker process meta: {meta:?}");
        if self.handler.handle_meta(meta).is_none() {
            error!("invalid meta: {meta:?}");
        }
    }
}

pub(crate) struct MetaHandler {
    pub(super) send_table: QpTable<RemoteAckTracker>,
    pub(super) recv_table: QpTable<LocalAckTracker>,
    pub(super) ack_tx: TaskTx<AckResponse>,
    pub(super) ack_timeout_tx: TaskTx<AckTimeoutTask>,
    pub(super) packet_retransmit_tx: TaskTx<PacketRetransmitTask>,
    pub(super) completion_tx: TaskTx<CompletionTask>,
    pub(super) rdma_write_tx: TaskTx<RdmaWriteTask>,
}

impl MetaHandler {
    pub(crate) fn new(
        ack_tx: TaskTx<AckResponse>,
        ack_timeout_tx: TaskTx<AckTimeoutTask>,
        packet_retransmit_tx: TaskTx<PacketRetransmitTask>,
        completion_tx: TaskTx<CompletionTask>,
        rdma_write_tx: TaskTx<RdmaWriteTask>,
    ) -> Self {
        Self {
            send_table: QpTable::new(),
            recv_table: QpTable::new(),
            ack_tx,
            ack_timeout_tx,
            packet_retransmit_tx,
            completion_tx,
            rdma_write_tx,
        }
    }

    pub(super) fn handle_meta(&mut self, meta: ReportMeta) -> Option<()> {
        // log::info!("MetaHandler handle_meta got meta = {:?}", meta);
        self.update_ack_timer(&meta);
        match meta {
            ReportMeta::HeaderWrite(x) => self.handle_header_write(x),
            ReportMeta::HeaderRead(x) => self.handle_header_read(x),
            ReportMeta::AckLocalHw(x) => self.handle_ack_local_hw(x),
            ReportMeta::AckRemoteDriver(x) => self.handle_ack_remote_driver(x),
            ReportMeta::NakLocalHw(x) => self.handle_nak_local_hw(x),
            ReportMeta::NakRemoteHw(x) => self.handle_nak_remote_hw(x),
            ReportMeta::NakRemoteDriver(x) => self.handle_nak_remote_driver(x),
            ReportMeta::Cnp { .. } => todo!(),
        }
    }

    fn update_ack_timer(&self, meta: &ReportMeta) {
        self.ack_timeout_tx
            .send(AckTimeoutTask::recv_meta(meta.qpn()));
    }

    fn handle_ack_local_hw(&mut self, meta: AckMetaLocalHw) -> Option<()> {
        log::debug!("handle_ack_local_hw: {meta:?}");
        let tracker = self.recv_table.get_qp_mut(meta.qpn)?;
        if let Some(psn) = tracker.ack_bitmap(meta.psn_now, meta.now_bitmap) {
            self.receiver_updates(meta.qpn, psn);
        }

        Some(())
    }

    fn handle_ack_remote_driver(&mut self, meta: AckMetaRemoteDriver) -> Option<()> {
        // log::debug!("handle_ack_remote_driver: {meta:?}");
        let tracker = self.send_table.get_qp_mut(meta.qpn)?;
        if let Some(psn) = tracker.ack_before(meta.psn_now) {
            self.sender_updates(meta.qpn, psn);
        }

        Some(())
    }

    fn handle_nak_local_hw(&mut self, meta: NakMetaLocalHw) -> Option<()> {
        log::info!("handle_nak_local_hw: {meta:?}");

        let tracker = self.recv_table.get_qp_mut(meta.qpn)?;
        if let Some(psn) =
            tracker.nak_bitmap(meta.psn_pre, meta.pre_bitmap, meta.psn_now, meta.now_bitmap)
        {
            self.receiver_updates(meta.qpn, psn);
        }

        Some(())
    }

    fn handle_nak_remote_hw(&mut self, meta: NakMetaRemoteHw) -> Option<()> {
        log::info!("handle_nak_remote_hw: {meta:?}");

        let tracker = self.send_table.get_qp_mut(meta.qpn)?;
        if let Some(psn) = tracker.nak_bitmap(
            meta.msn,
            meta.psn_pre,
            meta.pre_bitmap,
            meta.psn_now,
            meta.now_bitmap,
        ) {
            self.sender_updates(meta.qpn, psn);
        }

        // self.packet_retransmit_tx
        //     .send(PacketRetransmitTask::RetransmitRange {
        //         qpn: meta.qpn,
        //         psn_low: meta.psn_pre,
        //         psn_high: meta.psn_now + 128,
        //     });

        Some(())
    }

    #[allow(clippy::unnecessary_wraps)]
    fn handle_nak_remote_driver(&mut self, meta: NakMetaRemoteDriver) -> Option<()> {
        debug!("nak remote driver: {meta:?}");

        let tracker = self.send_table.get_qp_mut(meta.qpn)?;
        if let Some(psn) = tracker.ack_before(meta.psn_pre) {
            self.sender_updates(meta.qpn, psn);
        }

        // self.packet_retransmit_tx
        //     .send(PacketRetransmitTask::RetransmitRange {
        //         qpn: meta.qpn,
        //         psn_low: meta.psn_pre,
        //         psn_high: meta.psn_now,
        //     });

        Some(())
    }

    pub(crate) fn sender_updates(&self, qpn: u32, base_psn: Psn) {
        // log::info!(
        //     "MetaHandler sender_updates qpn={:?}, base_psn={:?}",
        //     qpn,
        //     base_psn
        // );
        self.completion_tx
            .send(CompletionTask::AckSend { qpn, base_psn });
        self.packet_retransmit_tx
            .send(PacketRetransmitTask::Ack { qpn, psn: base_psn });
        self.rdma_write_tx
            .send(RdmaWriteTask::new_ack(qpn, base_psn));
    }

    pub(crate) fn receiver_updates(&self, qpn: u32, base_psn: Psn) {
        debug!(
            "MetaHandler receiver_updates qpn={:?}, base_psn={:?}",
            qpn, base_psn
        );
        self.completion_tx
            .send(CompletionTask::AckRecv { qpn, base_psn });
        // // FIXME TODO 这对吗？为什么recv也要负责tx重传？
        // self.packet_retransmit_tx
        //     .send(PacketRetransmitTask::Ack { qpn, psn: base_psn });
    }

    pub(super) fn handle_header_read(&mut self, meta: HeaderReadMeta) -> Option<()> {
        debug!("MetaHandler handle_header_read got meta = {:?}", meta);
        if meta.ack_req {
            let end_psn = meta.psn + 1;
            let event = Event::Recv(RecvEvent::new(
                meta.dqpn,
                RecvEventOp::RecvRead,
                MessageMeta::new(meta.msn, end_psn),
                true,
            ));
            self.completion_tx.send(CompletionTask::Register {
                qpn: meta.dqpn,
                event,
            });
            let tracker = self.recv_table.get_qp_mut(meta.dqpn)?;
            if let Some(base_psn) = tracker.ack_one(meta.psn) {
                debug!("send ack 111");
                self.completion_tx.send(CompletionTask::AckRecv {
                    qpn: meta.dqpn,
                    base_psn,
                });
            }
        }

        let flags = if meta.ack_req {
            ibverbs_sys::ibv_send_flags::IBV_SEND_SOLICITED.0
        } else {
            0
        };

        let base = SendWrBase::new(
            0,
            flags,
            VirtAddr::new(meta.raddr),
            meta.total_len,
            meta.rkey,
            0,
            WorkReqOpCode::RdmaReadResp,
        );
        let send_wr = SendWrRdma::new_from_base(base, RemoteAddr::new(meta.laddr), meta.lkey);
        let task = RdmaWriteTask::new_write(meta.dqpn, send_wr);
        self.rdma_write_tx.send(task);

        Some(())
    }

    pub(super) fn handle_header_write(&mut self, meta: HeaderWriteMeta) -> Option<()> {
        let HeaderWriteMeta {
            pos,
            msn,
            psn,
            solicited,
            ack_req,
            is_retry,
            dqpn,
            total_len,
            raddr,
            rkey,
            imm,
            header_type,
        } = meta;
        debug!("Meta Handler got meta = {:?}", meta);
        let tracker = self.recv_table.get_qp_mut(dqpn)?;

        if matches!(pos, PacketPos::Last | PacketPos::Only) {
            let end_psn = psn + 1;
            match header_type {
                HeaderType::Write => {
                    let event = Event::Recv(RecvEvent::new(
                        meta.dqpn,
                        RecvEventOp::Write,
                        MessageMeta::new(msn, end_psn),
                        ack_req,
                    ));
                    debug!("send event to completion_tx queue: event={:?}", event);
                    self.completion_tx
                        .send(CompletionTask::Register { qpn: dqpn, event });
                }
                HeaderType::WriteWithImm => {
                    let event = Event::Recv(RecvEvent::new(
                        meta.dqpn,
                        RecvEventOp::WriteWithImm { imm },
                        MessageMeta::new(msn, end_psn),
                        ack_req,
                    ));
                    debug!("send event to completion_tx queue: event={:?}", event);
                    self.completion_tx
                        .send(CompletionTask::Register { qpn: dqpn, event });
                }
                HeaderType::Send => {
                    let event = Event::Recv(RecvEvent::new(
                        meta.dqpn,
                        RecvEventOp::Recv,
                        MessageMeta::new(msn, end_psn),
                        ack_req,
                    ));
                    debug!("send event to completion_tx queue: event={:?}", event);
                    self.completion_tx
                        .send(CompletionTask::Register { qpn: dqpn, event });
                }
                HeaderType::SendWithImm => {
                    let event = Event::Recv(RecvEvent::new(
                        meta.dqpn,
                        RecvEventOp::RecvWithImm { imm },
                        MessageMeta::new(msn, end_psn),
                        ack_req,
                    ));
                    debug!("send event to completion_tx queue: event={:?}", event);
                    self.completion_tx
                        .send(CompletionTask::Register { qpn: dqpn, event });
                }
                HeaderType::ReadResp => {
                    let event = Event::Recv(RecvEvent::new(
                        meta.dqpn,
                        RecvEventOp::ReadResp,
                        MessageMeta::new(msn, end_psn),
                        ack_req,
                    ));
                    debug!("send event to completion_tx queue: event={:?}", event);
                    self.completion_tx
                        .send(CompletionTask::Register { qpn: dqpn, event });
                }
            }
        }
        if let Some(base_psn) = tracker.ack_one(psn) {
            debug!("send event 222222222");
            self.completion_tx.send(CompletionTask::AckRecv {
                qpn: dqpn,
                base_psn,
            });
        }
        // Timeout of an `AckReq` message, notify retransmission
        if matches!(pos, PacketPos::Last | PacketPos::Only) && is_retry && ack_req {
            self.ack_tx.send(AckResponse::Nak {
                qpn: dqpn,
                base_psn: tracker.base_psn(),
                ack_req_packet_psn: psn - 1,
            });
        }

        Some(())
    }
}

#[cfg(test)]
mod test {
    use crate::workers::spawner::{task_channel, TaskRx};

    use super::*;

    #[allow(clippy::struct_field_names)]
    struct Rxs {
        ack_rx: TaskRx<AckResponse>,
        ack_timeout_rx: TaskRx<AckTimeoutTask>,
        packet_retransmit_rx: TaskRx<PacketRetransmitTask>,
        completion_rx: TaskRx<CompletionTask>,
        rdma_write_rx: TaskRx<RdmaWriteTask>,
    }

    #[allow(clippy::needless_pass_by_value)]
    impl Rxs {
        fn assert_completion(&self, task: CompletionTask) {
            let recv = self.completion_rx.recv().unwrap();
            assert_eq!(task, recv);
        }

        fn assert_ack(&self, task: AckResponse) {
            let recv = self.ack_rx.recv().unwrap();
            assert_eq!(task, recv);
        }

        fn assert_ack_timeout(&self, task: AckTimeoutTask) {
            let recv = self.ack_timeout_rx.recv().unwrap();
            assert_eq!(task, recv);
        }

        fn assert_packet_retransmit(&self, task: PacketRetransmitTask) {
            let recv = self.packet_retransmit_rx.recv().unwrap();
            assert_eq!(task, recv);
        }

        fn assert_rdma_write(&self, task: RdmaWriteTask) {
            let recv = self.rdma_write_rx.recv().unwrap();
            assert_eq!(task, recv);
        }
    }

    fn init_handler() -> (MetaHandler, Rxs) {
        let (ack_tx, ack_rx) = task_channel();
        let (ack_timeout_tx, ack_timeout_rx) = task_channel();
        let (packet_retransmit_tx, packet_retransmit_rx) = task_channel();
        let (completion_tx, completion_rx) = task_channel();
        let (rdma_write_tx, rdma_write_rx) = task_channel();
        let handler = MetaHandler::new(
            ack_tx,
            ack_timeout_tx,
            packet_retransmit_tx,
            completion_tx,
            rdma_write_tx,
        );
        let rxs = Rxs {
            ack_rx,
            ack_timeout_rx,
            packet_retransmit_rx,
            completion_rx,
            rdma_write_rx,
        };
        (handler, rxs)
    }

    #[test]
    fn test_handle_ack_local_hw() {
        let (mut handler, rxs) = init_handler();
        let qpn = 123;
        let meta = AckMetaLocalHw {
            qpn,
            psn_now: Psn(0),
            now_bitmap: u128::MAX,
        };
        handler.handle_meta(ReportMeta::AckLocalHw(meta)).unwrap();
        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
        rxs.assert_completion(CompletionTask::AckRecv {
            qpn,
            base_psn: Psn(128),
        });
        rxs.assert_packet_retransmit(PacketRetransmitTask::Ack { qpn, psn: Psn(128) });
    }

    #[test]
    fn test_handle_ack_remote_driver() {
        let (mut handler, rxs) = init_handler();
        let qpn = 456;

        // Initialize tracker for the QP
        handler.send_table.get_qp_mut(qpn).unwrap();

        let meta = AckMetaRemoteDriver {
            qpn,
            psn_now: Psn(200),
        };
        handler
            .handle_meta(ReportMeta::AckRemoteDriver(meta))
            .unwrap();
        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
        rxs.assert_completion(CompletionTask::AckSend {
            qpn,
            base_psn: Psn(200),
        });
        rxs.assert_packet_retransmit(PacketRetransmitTask::Ack { qpn, psn: Psn(200) });
    }

    #[test]
    fn test_handle_nak_local_hw() {
        let (mut handler, rxs) = init_handler();
        let qpn = 789;

        let meta = NakMetaLocalHw {
            qpn,
            msn: 10,
            psn_now: Psn(128),
            now_bitmap: 1,
            psn_pre: Psn(0),
            pre_bitmap: u128::MAX - 2,
        };
        handler.handle_meta(ReportMeta::NakLocalHw(meta)).unwrap();
        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
        rxs.assert_completion(CompletionTask::AckRecv {
            qpn,
            base_psn: Psn(1),
        });
        rxs.assert_packet_retransmit(PacketRetransmitTask::Ack { qpn, psn: Psn(1) });
    }

    #[test]
    fn test_handle_nak_remote_hw() {
        let (mut handler, rxs) = init_handler();
        let qpn = 101;
        let meta = NakMetaRemoteHw {
            qpn,
            msn: 15,
            psn_now: Psn(128),
            now_bitmap: 1,
            psn_pre: Psn(0),
            pre_bitmap: u128::MAX - 2,
        };

        handler.handle_meta(ReportMeta::NakRemoteHw(meta)).unwrap();
        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
        rxs.assert_completion(CompletionTask::AckSend {
            qpn,
            base_psn: Psn(1),
        });
        rxs.assert_packet_retransmit(PacketRetransmitTask::Ack { qpn, psn: Psn(1) });
        rxs.assert_packet_retransmit(PacketRetransmitTask::RetransmitRange {
            qpn,
            psn_low: Psn(0),
            psn_high: Psn(256),
        });
    }

    #[test]
    fn test_handle_nak_remote_driver() {
        let (mut handler, rxs) = init_handler();
        let qpn = 202;

        let meta = NakMetaRemoteDriver {
            qpn,
            psn_now: Psn(500),
            psn_pre: Psn(450),
        };

        let result = handler.handle_meta(ReportMeta::NakRemoteDriver(meta));
        assert!(result.is_some());

        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
        rxs.assert_completion(CompletionTask::AckSend {
            qpn,
            base_psn: Psn(450),
        });
        rxs.assert_packet_retransmit(PacketRetransmitTask::Ack { qpn, psn: Psn(450) });
        rxs.assert_packet_retransmit(PacketRetransmitTask::RetransmitRange {
            qpn,
            psn_low: Psn(450),
            psn_high: Psn(500),
        });
    }

    #[test]
    fn test_handle_header_read() {
        let (mut handler, rxs) = init_handler();
        let qpn = 303;

        let meta = HeaderReadMeta {
            msn: 20,
            psn: Psn(600),
            dqpn: qpn,
            raddr: 0x1000,
            rkey: 0x2000,
            total_len: 1024,
            laddr: 0x3000,
            lkey: 0x4000,
            ack_req: true,
        };

        handler.handle_meta(ReportMeta::HeaderRead(meta)).unwrap();
        rxs.assert_completion(CompletionTask::Register {
            qpn,
            event: Event::Recv(RecvEvent::new(
                qpn,
                RecvEventOp::RecvRead,
                MessageMeta::new(20, Psn(601)),
                true,
            )),
        });
        let base = SendWrBase::new(
            0,
            ibverbs_sys::ibv_send_flags::IBV_SEND_SOLICITED.0,
            VirtAddr::new(0x1000),
            1024,
            0x2000,
            0,
            WorkReqOpCode::RdmaReadResp,
        );
        let send_wr = SendWrRdma::new_from_base(base, RemoteAddr::new(meta.laddr), meta.lkey);
        let task = RdmaWriteTask::new_write(meta.dqpn, send_wr);
        rxs.assert_rdma_write(task);
        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
    }

    #[test]
    fn test_handle_header_write_last_packet() {
        let (mut handler, rxs) = init_handler();
        let qpn = 505;

        let meta = HeaderWriteMeta {
            pos: PacketPos::Only,
            msn: 30,
            psn: Psn(0),
            solicited: false,
            ack_req: true,
            is_retry: false,
            dqpn: qpn,
            total_len: 4096,
            raddr: 0x9000,
            rkey: 0xA000,
            imm: 0,
            header_type: HeaderType::Write,
        };

        handler.handle_meta(ReportMeta::HeaderWrite(meta)).unwrap();
        let event = Event::Recv(RecvEvent::new(
            meta.dqpn,
            RecvEventOp::Write,
            MessageMeta::new(30, Psn(1)),
            true,
        ));
        rxs.assert_completion(CompletionTask::Register { qpn, event });
        rxs.assert_completion(CompletionTask::AckRecv {
            qpn,
            base_psn: Psn(1),
        });
        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
    }

    #[test]
    fn test_handle_header_write_middle_packet() {
        let (mut handler, rxs) = init_handler();
        let qpn = 505;

        let meta = HeaderWriteMeta {
            pos: PacketPos::Middle,
            msn: 30,
            psn: Psn(1),
            solicited: false,
            ack_req: true,
            is_retry: false,
            dqpn: qpn,
            total_len: 4096,
            raddr: 0x9000,
            rkey: 0xA000,
            imm: 0,
            header_type: HeaderType::Write,
        };

        handler.handle_meta(ReportMeta::HeaderWrite(meta)).unwrap();

        // Should not send completion task for middle packet
        assert!(rxs.completion_rx.try_recv().is_none());

        rxs.assert_ack_timeout(AckTimeoutTask::RecvMeta { qpn });
    }

    #[test]
    fn test_handle_meta_invalid_qpn() {
        let (mut handler, _rxs) = init_handler();

        let meta = ReportMeta::AckLocalHw(AckMetaLocalHw {
            qpn: 999_999, // Invalid QPN
            psn_now: Psn(100),
            now_bitmap: 0xFF,
        });

        let result = handler.handle_meta(meta);
        assert!(result.is_none());
    }
}
