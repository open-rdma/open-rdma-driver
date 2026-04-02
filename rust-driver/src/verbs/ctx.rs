use std::{iter, net::Ipv4Addr, time::Duration};

use log::{debug, error};

use crate::{
    cmd::{CommandConfigurator, MttUpdate, PgtUpdate, RecvBufferMeta, UpdateQp},
    config::DeviceConfig,
    constants::CARD_MAC_ADDRESS,
    mem::{get_num_page, AddressResolver, DmaBuf, DmaBufAllocator, UmemHandler, PAGE_SIZE},
    net::{
        config::NetworkConfig,
        reader::NetConfigReader,
        recv_chan::{
            IpTxTable, PendingSendQueueTable, PostRecvTxTable, RecvWorkers, RecvWrQueueTable,
            SharedPendingSendQueue, SharedRecvWrQueue, PENDING_SEND_QUEUE_CAPACITY,
            RECV_WORKER_PORT,
        },
        simple_nic::SimpleNicController,
    },
    rdma_utils::{
        mtt::{Mtt, PgtEntry},
        pagemaps::check_addr_is_anon_hugepage,
        pd::PdTable,
        qp::{QpManager, QpTableShared},
        types::{
            ibv_qp_attr::{IbvQpAttr, IbvQpInitAttr},
            QpAttr, RecvWr, RecvWrQpn, SendWr, SendWrRdma,
        },
    },
    ring::buffer::DefaultDescRingBufAllocator,
    ring::csr::mode::Mode,
    ring::traits::DeviceAdaptor,
    types::{RemoteAddr, VirtAddr},
    workers::{
        ack_responder::AckResponder,
        completion::{
            Completion, CompletionQueueTable, CompletionTask, CompletionWorker, CqManager, Event,
            PostRecvEvent,
        },
        meta_report,
        qp_timeout::QpAckTimeoutWorker,
        rdma::{RdmaWriteTask, RdmaWriteWorker},
        retransmit::PacketRetransmitWorker,
        send::{self},
        spawner::{task_channel, AbortSignal, SingleThreadTaskWorker, TaskTx},
    },
    RdmaError,
};

use crate::error::Result;

use super::dev::HwDevice;

pub(crate) trait VerbsOps {
    fn reg_mr(&mut self, addr: u64, length: usize, pd_handle: u32, access: u8) -> Result<u32>;
    fn dereg_mr(&mut self, mr_key: u32) -> Result<()>;
    fn create_qp(&mut self, attr: IbvQpInitAttr) -> Result<u32>;
    fn update_qp(&mut self, qpn: u32, attr: IbvQpAttr) -> Result<()>;
    fn destroy_qp(&mut self, qpn: u32) -> Result<()>;
    fn create_cq(&mut self) -> Result<u32>;
    fn destroy_cq(&mut self, handle: u32) -> Result<()>;
    fn poll_cq(&mut self, handle: u32, max_num_entries: usize) -> Vec<Completion>;
    fn post_send(&mut self, qpn: u32, wr: SendWr) -> Result<()>;
    fn post_recv(&mut self, qpn: u32, wr: RecvWr) -> Result<()>;
    fn alloc_pd(&mut self) -> Result<u32>;
    fn dealloc_pd(&mut self, handle: u32) -> Result<()>;
}

pub(crate) struct HwDeviceCtx<H: HwDevice> {
    net_config: NetworkConfig,
    device: H,
    mtt: Mtt,
    mtt_buffer: DmaBuf,
    qp_manager: QpManager,
    qp_attr_table: QpTableShared<QpAttr>,
    cq_manager: CqManager,
    cq_table: CompletionQueueTable,
    cmd_controller: CommandConfigurator<H::Adaptor>,
    ip_tx_table: IpTxTable,
    post_recv_tx_table: PostRecvTxTable,
    recv_wr_queue_table: RecvWrQueueTable,
    // TODO need to optimaze
    pending_post_recv_queue: RecvWrQueueTable,
    pending_send_queue_table: PendingSendQueueTable,
    rdma_write_tx: TaskTx<RdmaWriteTask>,
    completion_tx: TaskTx<CompletionTask>,
    config: DeviceConfig,
    allocator: H::DmaBufAllocator,
    pd_table: PdTable,
}

#[allow(private_bounds)]
impl<H> HwDeviceCtx<H>
where
    H: HwDevice,
    H::Adaptor: DeviceAdaptor + Send + 'static,
    H::DmaBufAllocator: DmaBufAllocator,
    H::UmemHandler: UmemHandler,
{
    pub(crate) fn initialize(device: H, config: DeviceConfig, sysfs_name: String) -> Result<Self> {
        debug!("begin initializ...");
        let mode = Mode::default();
        let net_config = NetConfigReader::read(sysfs_name);
        debug!("begin device adaptor initializ...");
        let adaptor = device.new_adaptor()?;
        debug!("device adaptor initialized...");
        let mut allocator = device.new_dma_buf_allocator()?;
        let mut rb_allocator = DefaultDescRingBufAllocator::new(&mut allocator);
        let cmd_controller =
            CommandConfigurator::init(&adaptor, rb_allocator.alloc()?, rb_allocator.alloc()?)?;
        debug!("command queue request controller initialized...");
        let send_bufs = iter::repeat_with(|| rb_allocator.alloc())
            .take(mode.num_channel())
            .collect::<std::result::Result<_, _>>()?;
        let meta_bufs = iter::repeat_with(|| rb_allocator.alloc())
            .take(mode.num_channel())
            .collect::<std::result::Result<_, _>>()?;

        let (rdma_write_tx, rdma_write_rx) = task_channel();
        let (completion_tx, completion_rx) = task_channel();
        let (ack_timeout_tx, ack_timeout_rx) = task_channel();
        let (packet_retransmit_tx, packet_retransmit_rx) = task_channel();
        let (ack_tx, ack_rx) = task_channel();

        let abort = AbortSignal::new();
        let rx_buffer = rb_allocator.alloc()?;
        let rx_buffer_pa = rx_buffer.phys_addr;
        let qp_attr_table =
            QpTableShared::new_with(|| QpAttr::new_with_ip(net_config.ip.ip().to_bits()));

        debug!("qp table initialized...");
        let qp_manager = QpManager::new();
        let cq_manager = CqManager::new();
        let cq_table = CompletionQueueTable::new();
        let simple_nic_controller = SimpleNicController::init(
            &adaptor,
            rb_allocator.alloc()?,
            rb_allocator.alloc()?,
            rb_allocator.alloc()?,
            rx_buffer,
        )?;
        debug!("simple_nic_controller initialized...");
        let (simple_nic_tx, simple_nic_rx) = simple_nic_controller.into_split();
        let handle = send::spawn(&adaptor, send_bufs, mode, &abort)?;
        AckResponder::new(qp_attr_table.clone(), Box::new(simple_nic_tx)).spawn(
            ack_rx,
            "AckResponder",
            abort.clone(),
        );
        PacketRetransmitWorker::new(handle.clone()).spawn(
            packet_retransmit_rx,
            "PacketRetransmitWorker",
            abort.clone(),
        );
        QpAckTimeoutWorker::new(packet_retransmit_tx.clone(), config.ack()).spawn_polling(
            ack_timeout_rx,
            "QpAckTimeoutWorker",
            abort.clone(),
            Duration::from_nanos(4096u64 << config.ack().check_duration_exp),
        );

        RdmaWriteWorker::new(
            qp_attr_table.clone(),
            handle,
            ack_timeout_tx.clone(),
            packet_retransmit_tx.clone(),
            completion_tx.clone(),
        )
        .spawn_roundrobin(rdma_write_rx, "RdmaWriteWorker", abort.clone());

        CompletionWorker::new(
            cq_table.clone_arc(),
            qp_attr_table.clone(),
            ack_tx.clone(),
            ack_timeout_tx.clone(),
            rdma_write_tx.clone(),
        )
        .spawn(completion_rx, "CompletionWorker", abort.clone());

        meta_report::spawn(
            &adaptor,
            meta_bufs,
            mode,
            ack_tx.clone(),
            ack_timeout_tx.clone(),
            packet_retransmit_tx.clone(),
            completion_tx.clone(),
            rdma_write_tx.clone(),
            abort.clone(),
        )?;
        debug!("meta_report worker spawn called...");

        cmd_controller.set_network(net_config);
        debug!("set network param finished...");
        cmd_controller.set_raw_packet_recv_buffer(RecvBufferMeta::new(rx_buffer_pa));
        debug!("set_raw_packet_recv_buffer finished...");

        #[allow(clippy::mem_forget)]
        std::mem::forget(simple_nic_rx); // prevent libc::munmap being called

        let recv_wr_queue_table = RecvWrQueueTable::new();
        let pending_post_recv_queue = RecvWrQueueTable::new();
        let pending_send_queue_table = PendingSendQueueTable::new();

        RecvWorkers::new(
            net_config.ip.ip(),
            RECV_WORKER_PORT,
            qp_attr_table.clone(),
            recv_wr_queue_table.clone(),
            pending_send_queue_table.clone(),
            rdma_write_tx.clone(),
        )
        .spawn();

        Ok(Self {
            net_config,
            device,
            cmd_controller,
            qp_manager,
            qp_attr_table,
            cq_manager,
            cq_table,
            mtt_buffer: rb_allocator.alloc()?,
            mtt: Mtt::new(),
            ip_tx_table: IpTxTable::new(),
            post_recv_tx_table: PostRecvTxTable::new(),
            recv_wr_queue_table,
            pending_post_recv_queue,
            pending_send_queue_table,
            rdma_write_tx,
            completion_tx,
            config,
            allocator,
            pd_table: PdTable::new(),
        })
    }
}

impl<H: HwDevice> HwDeviceCtx<H> {
    // fn send(&self, qpn: u32, wr: SendWrBase) -> Result<()> {
    //     // 统一处理：加入 pending 队列
    //     if !self.pending_send_queue_table.try_push(qpn, SendWr::Send(wr)) {
    //         return Err(RdmaError::ResourceExhausted(format!(
    //             "Pending send queue for QP {} is full (capacity: {})",
    //             qpn, PENDING_SEND_QUEUE_CAPACITY
    //         )));
    //     }

    //     debug!(
    //         "QP {}: Buffered SEND to pending queue (pending count: {})",
    //         qpn,
    //         self.pending_send_queue_table.len(qpn)
    //     );

    //     // 尝试匹配
    //     try_match_pendings(
    //         qpn,
    //         &self.pending_send_queue_table.clone_queue(qpn).ok_or_else(|| {
    //             RdmaError::NotFound(format!("Pending send queue for QP {} not found", qpn))
    //         })?,
    //         &self.recv_wr_queue_table.clone_recv_wr_queue(qpn).ok_or_else(|| {
    //             RdmaError::NotFound(format!("Receive WR queue for QP {} not found", qpn))
    //         })?,
    //         &self.rdma_write_tx,
    //     )?;

    //     Ok(())
    // }

    fn rdma_read(&self, qpn: u32, wr: SendWrRdma) {
        let task = RdmaWriteTask::new_write(qpn, wr);
        self.rdma_write_tx.send(task);
    }

    fn rdma_write(&self, qpn: u32, wr: SendWrRdma) {
        let task = RdmaWriteTask::new_write(qpn, wr);
        self.rdma_write_tx.send(task);
    }
}

// TODO 这些可以封装为一个数据结构，try_match_pendings可以变为这个结构的方法
pub(crate) fn try_match_pendings(
    qpn: u32,
    pending_send_queue: &SharedPendingSendQueue,
    recv_wr_queue: &SharedRecvWrQueue,
    rdma_write_tx: &TaskTx<RdmaWriteTask>,
) -> Result<()> {
    loop {
        // 1. Peek 队首元素（不立即移除）
        let pending_send = {
            let queue = pending_send_queue.lock();
            match queue.front() {
                Some(send) => send.clone(),
                None => break, // 队列为空，退出
            }
        };

        // 2. 判断操作是否需要 recv WR
        let needs_recv_wr = match &pending_send {
            SendWr::Rdma(rdma_wr) => {
                matches!(rdma_wr.opcode(), send::WorkReqOpCode::RdmaWriteWithImm)
            }
            SendWr::Send(_) => true, // SEND 和 SEND_WITH_IMM 都需要
        };

        // 3. 分类处理
        if needs_recv_wr {
            // 需要 recv WR 的操作
            let recv_wr = match recv_wr_queue.lock().pop_front() {
                Some(recv) => recv,
                None => {
                    // 没有可用的 recv WR，停止处理（队首阻塞）
                    debug!(
                        "QP {}: Head operation needs recv WR but none available, blocking queue",
                        qpn
                    );
                    break;
                }
            };

            // 从队列中移除队首元素
            let pending_send = pending_send_queue.lock().pop_front().unwrap();

            // 匹配并发送
            match pending_send {
                SendWr::Rdma(rdma_wr) => {
                    // WRITE_WITH_IMM: 只消费 recv WR，不需要匹配长度
                    assert!(rdma_wr.opcode() == send::WorkReqOpCode::RdmaWriteWithImm);
                    debug!("QP {}: Matched RDMA_WRITE_WITH_IMM with recv WR", qpn);
                    let task = RdmaWriteTask::new_write(qpn, rdma_wr);
                    rdma_write_tx.send(task);
                }
                SendWr::Send(send_base) => {
                    // SEND: 需要匹配长度
                    if send_base.length != recv_wr.length {
                        // 长度不匹配 - 错误处理
                        error!(
                            "QP {}: Length mismatch - SEND len={}, recv WR len={}",
                            qpn, send_base.length, recv_wr.length
                        );

                        // 将操作放回队首
                        pending_send_queue
                            .lock()
                            .push_front(SendWr::Send(send_base));
                        recv_wr_queue.lock().push_front(recv_wr);

                        // TODO: 使 QP 进入错误状态
                        return Err(RdmaError::InvalidInput(format!(
                            "QP {}: Send/Recv length mismatch (send={}, recv={})",
                            qpn, send_base.length, recv_wr.length
                        )));
                    }

                    let rdma_wr = SendWrRdma::new_from_base(
                        send_base,
                        RemoteAddr::new(recv_wr.addr.as_u64()),
                        recv_wr.lkey,
                    );
                    debug!(
                        "QP {}: Matched SEND (len={}) with recv WR",
                        qpn, recv_wr.length
                    );
                    let task = RdmaWriteTask::new_write(qpn, rdma_wr);
                    rdma_write_tx.send(task);
                }
            }
        } else {
            // 不需要 recv WR 的操作（RDMA_WRITE, RDMA_READ）
            // 从队列中移除队首元素
            let pending_send = pending_send_queue.lock().pop_front().unwrap();

            match pending_send {
                SendWr::Rdma(rdma_wr) => {
                    debug!(
                        "QP {}: Processing {:?} without recv WR",
                        qpn,
                        rdma_wr.opcode()
                    );
                    let task = RdmaWriteTask::new_write(qpn, rdma_wr);
                    rdma_write_tx.send(task);
                }
                SendWr::Send(_) => {
                    unreachable!("SendWr::Send should always need recv WR");
                }
            }
        }
    }

    Ok(())
}

impl<H> VerbsOps for HwDeviceCtx<H>
where
    H: HwDevice,
    H::Adaptor: DeviceAdaptor + Send + 'static,
    H::UmemHandler: UmemHandler,
{
    fn reg_mr(&mut self, addr: u64, length: usize, pd_handle: u32, access: u8) -> Result<u32> {
        fn chunks(entry: PgtEntry) -> Vec<PgtEntry> {
            /// Maximum number of Page Table entries (PGT entries) that can be allocated in a single `PCIe` transaction.
            /// A `PCIe` transaction size is 128 bytes, and each PGT entry is a u64 (8 bytes).
            /// Therefore, 128 bytes / 8 bytes per entry = 16 entries per allocation.
            const MAX_NUM_PGT_ENTRY_PER_ALLOC: usize = 16;

            let base_index = entry.index;
            let end_index = base_index + entry.count;
            (base_index..end_index)
                .step_by(MAX_NUM_PGT_ENTRY_PER_ALLOC)
                .map(|index| PgtEntry {
                    index,
                    count: (MAX_NUM_PGT_ENTRY_PER_ALLOC as u32).min(end_index - index),
                })
                .collect()
        }

        let umem_handler = self.device.new_umem_handler();
        let virt_addr = VirtAddr::new(addr);
        // umem_handler.pin_pages(virt_addr, length)?;

        //TODO maybe need to optimaze, it cost a lot
        #[cfg(feature = "page_size_2m")]
        assert!(check_addr_is_anon_hugepage(VirtAddr::new(addr), length));

        let num_pages = get_num_page(addr, length);
        debug!("generate page table entries: addr=0x{addr:x}, length=0x{length:x} --> num_pages={num_pages}");
        let (mr_key, pgt_entry) = self
            .mtt
            .register(num_pages, virt_addr, length, &umem_handler)?;
        let length_u32 = u32::try_from(length)
            .map_err(|_err| RdmaError::InvalidInput("Length too large".into()))?;

        // Use type-safe alignment instead of manual bit manipulation
        let aligned_va = virt_addr.to_alignd();
        let phys_addrs = umem_handler
            .virt_to_phys_range(aligned_va, num_pages)?
            .into_iter()
            .collect::<Option<Vec<_>>>()
            .ok_or(RdmaError::MemoryError("Physical address not found".into()))?;
        let phys_addrs_for_debug = phys_addrs.clone();
        // .into_iter();
        let buf = &mut self.mtt_buffer.buf;
        let base_index = pgt_entry.index;
        let mtt_update = MttUpdate::new(
            VirtAddr::new(addr),
            length_u32,
            mr_key,
            pd_handle,
            access,
            base_index,
        );
        // TODO: makes updates atomic
        self.cmd_controller.update_mtt(mtt_update);
        let mut phys_addrs = phys_addrs.into_iter();
        let mut va_start_for_debug = addr & !(PAGE_SIZE as u64 - 1);
        for PgtEntry { index, count } in chunks(pgt_entry) {
            let chunk_phys_addrs: Vec<_> = phys_addrs.by_ref().take(count as usize).collect();
            let bytes: Vec<u8> = chunk_phys_addrs
                .iter()
                .flat_map(|pa| pa.as_u64().to_ne_bytes())
                .collect();
            buf.copy_from(0, &bytes);
            let pgt_update = PgtUpdate::new(self.mtt_buffer.phys_addr, index, count - 1);
            debug!("new pgt update request: {pgt_update:?}");
            for phy_addr in &chunk_phys_addrs {
                debug!(
                    "pgt map va -> pa: 0x{va_start_for_debug:x} -> 0x{:x}",
                    phy_addr.as_u64()
                );
                va_start_for_debug += PAGE_SIZE as u64;
            }
            self.cmd_controller.update_pgt(pgt_update);
        }

        Ok(mr_key)
    }

    fn dereg_mr(&mut self, mr_key: u32) -> Result<()> {
        let umem_handler = self.device.new_umem_handler();
        self.mtt.deregister(mr_key, &umem_handler)
    }

    fn create_qp(&mut self, attr: IbvQpInitAttr) -> Result<u32> {
        let qpn = self
            .qp_manager
            .create_qp()
            .ok_or(RdmaError::ResourceExhausted(
                "No QP numbers available".into(),
            ))?;
        let _ignore = self.qp_attr_table.map_qp_mut(qpn, |current| {
            current.qpn = qpn;
            current.qp_type = attr.qp_type();
            current.send_cq = attr.send_cq();
            current.recv_cq = attr.recv_cq();
            current.mac_addr = CARD_MAC_ADDRESS;
            current.pmtu = ibverbs_sys::IBV_MTU_4096 as u8;
        });
        let entry = UpdateQp {
            ip_addr: 0,
            peer_mac_addr: 0,
            local_udp_port: 0x100,
            qp_type: attr.qp_type(),
            qpn,
            ..Default::default()
        };
        self.cmd_controller.update_qp(entry);

        Ok(qpn)
    }

    fn update_qp(&mut self, qpn: u32, attr: IbvQpAttr) -> Result<()> {
        // TODO: This is a workaround for read-to-write conversion. Consider modifying the
        // hardware to allow remote writes for read responses.
        let rq_access_flags = (ibverbs_sys::ibv_access_flags::IBV_ACCESS_LOCAL_WRITE.0
            | ibverbs_sys::ibv_access_flags::IBV_ACCESS_REMOTE_READ.0
            | ibverbs_sys::ibv_access_flags::IBV_ACCESS_REMOTE_WRITE.0)
            as u8;

        debug!("before modify qp_attr_table");
        let entry = self
            .qp_attr_table
            .map_qp_mut(qpn, |current| {
                let current_ip = (current.dqp_ip != 0).then_some(current.dqp_ip);
                let attr_ip = attr.dest_qp_ip().map(Ipv4Addr::to_bits);
                let ip_addr = attr_ip.or(current_ip).unwrap_or_else(|| {
                    if attr.qp_state() == Some(ibverbs_sys::ibv_qp_state::IBV_QPS_INIT) {
                        0
                    } else {
                        panic!(
                            "QP {qpn} dest_qp_ip must be set in state {:?}, addr is {:?}",
                            attr.qp_state(),
                            attr.dest_qp_ip()
                        );
                    }
                });
                log::info!("update_qp set dqp_ip={:?}", Ipv4Addr::from_bits(ip_addr));
                let entry = UpdateQp {
                    qpn,
                    ip_addr,
                    local_udp_port: 0x100,
                    peer_mac_addr: CARD_MAC_ADDRESS,
                    qp_type: current.qp_type,
                    peer_qpn: attr.dest_qp_num().unwrap_or(current.dqpn),
                    rq_access_flags,
                    pmtu: attr.path_mtu().map_or(current.pmtu, |x| x as u8),
                };
                current.dqpn = entry.peer_qpn;
                current.access_flags = rq_access_flags;
                current.pmtu = entry.pmtu;
                current.dqp_ip = ip_addr;
                entry
            })
            .ok_or(RdmaError::NotFound(format!("QP {qpn} not found",)))?;

        debug!("before send qp update request to hardware");
        self.cmd_controller.update_qp(entry);

        let qp = self
            .qp_attr_table
            .get_qp(qpn)
            .ok_or(RdmaError::NotFound(format!("QP {qpn} not found",)))?;

        if qp.dqpn != 0 && qp.dqp_ip != 0 && self.post_recv_tx_table.get_qp_mut(qpn).is_none() {
            log::info!("start RTS!!!!!!");
            let dqp_ip = Ipv4Addr::from_bits(qp.dqp_ip);
            debug!("update_qp get dqp_ip={dqp_ip:?}");
            log::info!("qp local ip is {},remote ip is {}", qp.ip, qp.dqp_ip);
            //TODO 这里不会有并发问题吗？在 qp 准备好之后，马上 post_recv，会不会出现问题？
            let tx = self.ip_tx_table.get_or_connect(
                self.net_config.ip.ip(),
                dqp_ip,
                RECV_WORKER_PORT,
            )?;
            self.post_recv_tx_table.insert(qpn, tx);

            // 刷新 pending 队列中缓存的 RecvWr
            if let Some(pending_queue) = self.pending_post_recv_queue.clone_recv_wr_queue(qpn) {
                let mut queue = pending_queue.lock();
                let pending_count = queue.len();
                if pending_count > 0 {
                    debug!("Flushing {pending_count} pending RecvWr for QP {qpn}");
                    // 获取 tx 发送所有 pending 的 RecvWr
                    if let Some(tx) = self.post_recv_tx_table.get_qp_mut(qpn) {
                        while let Some(wr) = queue.pop_front() {
                            if let Err(e) = tx.lock().send(RecvWrQpn { wr, qpn }) {
                                error!("Failed to send pending RecvWr for QP {qpn}: {e}");
                            }
                        }
                    }
                }
            }
        }

        Ok(())
    }

    fn destroy_qp(&mut self, qpn: u32) -> Result<()> {
        // Clear pending send queue (optional warning)
        if let Some(queue) = self.pending_send_queue_table.clone_queue(qpn) {
            let pending_count = queue.lock().len();
            if pending_count > 0 {
                log::warn!(
                    "Destroying QP {} with {} pending send WRs, they will be dropped",
                    qpn,
                    pending_count
                );
                queue.lock().clear();
            }
        }

        // Original destroy logic
        if self.qp_manager.destroy_qp(qpn) {
            Ok(())
        } else {
            Err(RdmaError::InvalidInput(format!("QPN {qpn} not present")))
        }
    }

    fn create_cq(&mut self) -> Result<u32> {
        self.cq_manager
            .create_cq()
            .ok_or(RdmaError::ResourceExhausted("No CQ available".into()))
    }

    fn destroy_cq(&mut self, handle: u32) -> Result<()> {
        if self.cq_manager.destroy_cq(handle) {
            Ok(())
        } else {
            Err(RdmaError::InvalidInput(format!(
                "CQ handle {handle} not present"
            )))
        }
    }

    fn post_send(&mut self, qpn: u32, wr: SendWr) -> Result<()> {
        debug!("post_send called, qpn is {qpn}, wr is {wr:?}");

        // 统一处理：所有操作都先加入 pending 队列
        if !self.pending_send_queue_table.try_push(qpn, wr) {
            return Err(RdmaError::ResourceExhausted(format!(
                "Pending send queue for QP {} is full (capacity: {})",
                qpn, PENDING_SEND_QUEUE_CAPACITY
            )));
        }

        debug!(
            "QP {}: Buffered operation to pending queue (pending count: {})",
            qpn,
            self.pending_send_queue_table.len(qpn)
        );

        // 尝试匹配并发送队列中的操作
        let HwDeviceCtx::<H> {
            pending_send_queue_table,
            recv_wr_queue_table,
            rdma_write_tx,
            ..
        } = self;

        try_match_pendings(
            qpn,
            &pending_send_queue_table.clone_queue(qpn).ok_or_else(|| {
                RdmaError::NotFound(format!("Pending send queue for QP {} not found", qpn))
            })?,
            &recv_wr_queue_table
                .clone_recv_wr_queue(qpn)
                .ok_or_else(|| {
                    RdmaError::NotFound(format!("Receive WR queue for QP {} not found", qpn))
                })?,
            rdma_write_tx,
        )?;

        Ok(())
    }

    fn poll_cq(&mut self, handle: u32, max_num_entries: usize) -> Vec<Completion> {
        let Some(cq) = self.cq_table.get_cq(handle) else {
            return vec![];
        };
        let ret: Vec<Completion> = iter::repeat_with(|| cq.pop_front())
            .take_while(Option::is_some)
            .take(max_num_entries)
            .flatten()
            .collect();
        if !ret.is_empty() {
            debug!("poll_cq returned {ret:?}");
        }
        ret
    }

    fn post_recv(&mut self, qpn: u32, wr: RecvWr) -> Result<()> {
        debug!("post_recv called, qpn is {qpn}, wr is {wr:?}");
        let qp = self
            .qp_attr_table
            .get_qp(qpn)
            .ok_or(RdmaError::QpError(format!("QP {qpn} not found",)))?;

        // 注册 PostRecv 事件
        let event = Event::PostRecv(PostRecvEvent::new(qpn, wr.wr_id));
        self.completion_tx
            .send(CompletionTask::Register { qpn, event });

        // 检查 tx 是否已创建
        if let Some(tx) = self.post_recv_tx_table.get_qp_mut(qpn) {
            // RTR/RTS 状态：直接发送
            debug!("Sending RecvWr for QP {qpn} in RTR/RTS state");

            let result = tx.lock().send(RecvWrQpn { wr, qpn });
            debug!("result is {:?}", result);
            result?;
        } else {
            // INIT 状态：缓存到 pending 队列
            if let Some(queue) = self.pending_post_recv_queue.clone_recv_wr_queue(qpn) {
                queue.lock().push_back(wr);
                debug!("Buffered RecvWr for QP {qpn} in INIT state");
            }
        }

        Ok(())
    }

    fn alloc_pd(&mut self) -> Result<u32> {
        self.pd_table
            .alloc()
            .ok_or(RdmaError::ResourceExhausted("No PD available".into()))
    }

    fn dealloc_pd(&mut self, handle: u32) -> Result<()> {
        if self.pd_table.dealloc(handle) {
            Ok(())
        } else {
            Err(RdmaError::InvalidInput(format!(
                "PD handle {handle} not present"
            )))
        }
    }
}
