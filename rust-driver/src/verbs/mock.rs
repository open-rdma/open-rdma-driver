#![allow(
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::unwrap_in_result,
    unsafe_code,
    clippy::too_many_lines,
    clippy::wildcard_enum_match_arm
)]

use core::panic;
use std::{
    collections::{HashMap, VecDeque},
    fmt,
    io::{self, BufReader, Write},
    iter,
    net::{Ipv4Addr, TcpListener, TcpStream},
    ptr,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::Duration,
};

use crate::{
    error::RdmaError,
    rdma_utils::{
        pagemaps::check_addr_is_anon_hugepage,
        pd::PdTable,
        qp::{QpManager, QpTable},
        types::{
            ibv_qp_attr::{IbvQpAttr, IbvQpInitAttr},
            RecvWr, SendWr,
        },
    },
    ring::traits::DeviceAdaptor,
    types::{PhysAddr, VirtAddr},
};

use bincode::{Decode, Encode};
use log::{debug, error, info, warn};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};

use crate::{
    mem::{
        mmap::MmapMut, AddressResolver, DmaBuf, DmaBufAllocator, MemoryPinner,
        PhysAddrResolverLinuxX86, UmemHandler,
    },
    workers::{completion::Completion, send::WorkReqOpCode},
};

use super::ctx::VerbsOps;

#[derive(Debug, Clone, Copy)]
pub(crate) struct MockDeviceAdaptor;

impl DeviceAdaptor for MockDeviceAdaptor {
    fn read_csr(&self, addr: usize) -> io::Result<u32> {
        Ok(0)
    }

    fn write_csr(&self, addr: usize, data: u32) -> io::Result<()> {
        Ok(())
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct MockDmaBufAllocator;

impl DmaBufAllocator for MockDmaBufAllocator {
    fn alloc(&mut self, len: usize) -> io::Result<DmaBuf> {
        const LEN: usize = 4096 * 32;
        #[allow(unsafe_code)]
        let ptr = unsafe {
            libc::mmap(
                ptr::null_mut(),
                LEN,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED | libc::MAP_ANON,
                -1,
                0,
            )
        };

        if ptr == libc::MAP_FAILED {
            return Err(io::Error::last_os_error());
        }

        let mmap = MmapMut::new(ptr, usize::MAX);
        Ok(DmaBuf::new(mmap, PhysAddr::new(0)))
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct MockUmemHandler;

impl MemoryPinner for MockUmemHandler {
    fn pin_pages(&self, _addr: VirtAddr, _length: usize) -> io::Result<()> {
        Ok(())
    }

    fn unpin_pages(&self, _addr: VirtAddr, _length: usize) -> io::Result<()> {
        Ok(())
    }
}

impl AddressResolver for MockUmemHandler {
    fn virt_to_phys(&self, _virt_addr: VirtAddr) -> io::Result<Option<PhysAddr>> {
        Ok(Some(PhysAddr::new(0)))
    }
}

impl UmemHandler for MockUmemHandler {}

#[derive(Debug, Default)]
struct QpCtx {
    dpq_ip: Option<Ipv4Addr>,
    dpqn: Option<u32>,
    conn: Option<QpConnetion>,
    abort_signal: Option<Arc<AtomicBool>>,
    handle: Option<thread::JoinHandle<()>>,
    local_task_tx: Option<flume::Sender<LocalTask>>,
}

impl QpCtx {
    fn conn(&self) -> &QpConnetion {
        self.conn.as_ref().unwrap()
    }
}

#[derive(Debug)]
pub(crate) struct MockDeviceCtx {
    mr_key: u32,
    cq_handle: u32,
    self_ip: u32,
    cq_table: HashMap<u32, CompletionQueue>,
    send_qp_cq_map: HashMap<u32, u32>,
    recv_qp_cq_map: HashMap<u32, u32>,
    qp_ctx_table: QpTable<QpCtx>,
    qp_manager: QpManager,
    mr_table: MrTable,
    pd_table: PdTable,
}

impl Default for MockDeviceCtx {
    fn default() -> Self {
        Self {
            mr_key: 0,
            cq_handle: 0,
            self_ip: 0,
            cq_table: HashMap::new(),
            send_qp_cq_map: HashMap::new(),
            recv_qp_cq_map: HashMap::new(),
            qp_ctx_table: QpTable::new(),
            qp_manager: QpManager::new(),
            mr_table: MrTable::default(),
            pd_table: PdTable::default(),
        }
    }
}

impl MockDeviceCtx {}

impl VerbsOps for MockDeviceCtx {
    fn reg_mr(
        &mut self,
        addr: u64,
        length: usize,
        pd_handle: u32,
        access: u8,
    ) -> crate::error::Result<u32> {
        #[cfg(feature = "page_size_2m")]
        assert!(check_addr_is_anon_hugepage(VirtAddr::new(addr), length));
        let addr_resolver = PhysAddrResolverLinuxX86;
        let pa = addr_resolver
            .virt_to_phys(VirtAddr::new(addr))
            .map_err(|e| {
                log::warn!("Failed to resolve physical address: {e}");
                RdmaError::MemoryError(format!("Failed to resolve physical address: {e}",))
            })?;

        info!("mock reg mr, virt addr: {addr:x}, length: {length}, access: {access}, phys_addr: {pa:?}");

        if pa.is_some() {
            self.mr_table.reg(Mr::new(addr, length));
        } else {
            return Err(RdmaError::MemoryError("Physical address not found".into()));
        }

        self.mr_key += 1;
        Ok(self.mr_key)
    }

    //TODO mock实现不完整
    fn dereg_mr(&mut self, mr_key: u32) -> crate::error::Result<()> {
        info!("mock dereg mr");
        Ok(())
    }

    fn create_qp(&mut self, attr: IbvQpInitAttr) -> crate::error::Result<u32> {
        let qpn = self
            .qp_manager
            .create_qp()
            .ok_or_else(|| RdmaError::QpError("No available QP slots".into()))?;
        if let Some(h) = attr.send_cq() {
            info!("set send cq: {h} for qp: {qpn}");
            let _ignore = self.send_qp_cq_map.insert(qpn, h);
        }
        if let Some(h) = attr.recv_cq() {
            info!("set recv cq: {h} for qp: {qpn}");
            let _ignore = self.recv_qp_cq_map.insert(qpn, h);
        }
        let send_cq = attr.send_cq().and_then(|h| self.cq_table.get(&h)).cloned();
        let recv_cq = attr.recv_cq().and_then(|h| self.cq_table.get(&h)).cloned();

        let conn = QpConnetion::new(self.self_ip.into(), qpn);
        let conn_c = conn.clone();
        let (tx, rx) = flume::unbounded::<LocalTask>();
        let tx_for_ctx = tx;
        let mut recv_reqs = VecDeque::new();
        let mut pending_write_with_imm: VecDeque<RdmaWriteReq> = VecDeque::new();
        let mr_table = self.mr_table.clone_arc();
        let abort_signal = Arc::new(AtomicBool::new(false));
        let abort_signal_c = Arc::clone(&abort_signal);
        let handle = thread::spawn(move || loop {
            for task in rx.try_iter() {
                debug!("recv task: {task:?}");
                match task {
                    LocalTask::PostRecv(req) => {
                        // Check if there are pending WRITE_WITH_IMM messages
                        if let Some(pending) = pending_write_with_imm.pop_front() {
                            // Process queued WRITE_WITH_IMM with this RecvWr
                            log::info!("Processing queued WRITE_WITH_IMM on QPN {qpn}");
                            write_local_addr(&mr_table, pending.raddr, &pending.data);
                            if let Some(x) = recv_cq.as_ref() {
                                let completion = Completion::RecvRdmaWithImm {
                                    wr_id: req.wr.wr_id,
                                    imm: pending.imm,
                                };
                                info!("new completion, qpn: {qpn}, completion: {completion:?}");
                                x.push(completion);
                            }
                            let resp = WriteOrSendResp {
                                wr_id: pending.wr_id,
                                ack_req: pending.ack_req,
                            };
                            conn_c.send(QpTransportMessage::WriteResp(resp));
                        } else {
                            // No pending messages, queue RecvWr normally
                            recv_reqs.push_back(req);
                        }
                    }
                }
            }

            if abort_signal.load(Ordering::Relaxed) {
                break;
            }

            let Some(msg) = conn_c.recv::<QpTransportMessage>() else {
                continue;
            };

            debug!("recv msg from connection: {msg:?}");

            match msg {
                // Requests
                QpTransportMessage::WriteReq(RdmaWriteReq {
                    raddr,
                    imm,
                    data,
                    wr_id,
                    ack_req,
                }) => {
                    write_local_addr(&mr_table, raddr, &data);
                    let resp = WriteOrSendResp { wr_id, ack_req };
                    conn_c.send(QpTransportMessage::WriteResp(resp));
                }
                QpTransportMessage::WriteWithImmReq(req) => {
                    if let Some(recv_req) = recv_reqs.pop_front() {
                        // RecvWr available, process immediately
                        write_local_addr(&mr_table, req.raddr, &req.data);
                        if let Some(x) = recv_cq.as_ref() {
                            let completion = Completion::RecvRdmaWithImm {
                                wr_id: recv_req.wr.wr_id,
                                imm: req.imm,
                            };
                            info!("new completion, qpn: {qpn}, completion: {completion:?}");
                            x.push(completion);
                        }
                        let resp = WriteOrSendResp {
                            wr_id: req.wr_id,
                            ack_req: req.ack_req,
                        };
                        conn_c.send(QpTransportMessage::WriteResp(resp));
                    } else {
                        // No RecvWr available, queue for later processing
                        log::warn!(
                            "No RecvWr available for WRITE_WITH_IMM on QPN {qpn}, queuing message"
                        );
                        pending_write_with_imm.push_back(req);
                    }
                }
                QpTransportMessage::SendReq(SendReq {
                    wr_id,
                    data,
                    imm,
                    ack_req,
                }) => {
                    let req = recv_reqs
                        .pop_front()
                        .ok_or_else(|| {
                            log::error!("No receive request available for QPN {qpn}");
                            RdmaError::QpError("No receive request available".into())
                        })
                        .expect("No receive request available");

                    write_local_addr(&mr_table, req.wr.addr.as_u64(), &data);
                    if let Some(x) = recv_cq.as_ref() {
                        let completion = Completion::Recv { wr_id, imm };
                        info!("new completion, qpn: {qpn}, completion: {completion:?}");
                        x.push(completion);
                    }
                    let resp = WriteOrSendResp { wr_id, ack_req };
                    conn_c.send(QpTransportMessage::WriteResp(resp));
                }
                QpTransportMessage::ReadReq(x) => {
                    let data = read_local_addr(x.raddr, x.len as usize);
                    let resp = RdmaReadResp {
                        laddr: x.laddr,
                        raddr: x.raddr,
                        ack_req: x.ack_req,
                        wr_id: x.wr_id,
                        data,
                    };
                    conn_c.send(QpTransportMessage::ReadResp(resp));
                }

                // Responses
                QpTransportMessage::WriteResp(WriteOrSendResp { wr_id, ack_req })
                | QpTransportMessage::WriteWithImmResp(WriteOrSendResp { wr_id, ack_req })
                    if ack_req =>
                {
                    if let Some(x) = send_cq.as_ref() {
                        let completion = Completion::RdmaWrite { wr_id };
                        info!("new completion, qpn: {qpn}, completion: {completion:?}");
                        x.push(completion);
                    }
                }
                QpTransportMessage::SendResp(WriteOrSendResp { wr_id, ack_req }) if ack_req => {
                    if let Some(x) = send_cq.as_ref() {
                        let completion = Completion::Send { wr_id };
                        info!("new completion, qpn: {qpn}, completion: {completion:?}");
                        x.push(completion);
                    }
                }
                QpTransportMessage::ReadResp(RdmaReadResp {
                    laddr,
                    raddr,
                    data,
                    ack_req,
                    wr_id,
                }) => {
                    write_local_addr(&mr_table, laddr, &data);
                    if ack_req {
                        if let Some(x) = recv_cq.as_ref() {
                            let completion = Completion::RdmaRead { wr_id };
                            info!("new completion, qpn: {qpn}, completion: {completion:?}");
                            x.push(completion);
                        }
                    }
                }
                QpTransportMessage::WriteResp(_)
                | QpTransportMessage::WriteWithImmResp(_)
                | QpTransportMessage::SendResp(_) => {}
            }
        });

        // Store QP context - must succeed or we leak the thread
        let result = self.qp_ctx_table.map_qp_mut(qpn, move |ctx| {
            if ctx.conn.is_some() {
                panic!("QP context for QPN {qpn} already exists");
            }
            ctx.conn = Some(conn);
            ctx.abort_signal = Some(abort_signal_c);
            ctx.handle = Some(handle);
            ctx.local_task_tx = Some(tx_for_ctx);
        });

        if result.is_none() {
            // Critical failure: QP slot not found, must cleanup the spawned thread
            error!("Failed to store QP context for QPN {qpn}, aborting thread");
            // abort_signal.store(true, Ordering::Relaxed);
            // self.qpn_set.remove(&qpn);
            // return Err(RdmaError::QpError(format!(
            //     "QP slot not found for QPN {qpn} (index {})",
            //     qpn >> 8
            // )));
            panic!("");
        }

        info!("mock create qp: {qpn}");

        Ok(qpn)
    }

    fn update_qp(&mut self, qpn: u32, attr: IbvQpAttr) -> crate::error::Result<()> {
        log::info!(
            "[MOCK DEBUG] mock update qp: {qpn}, dest_qp_ip is: {:?}",
            attr.dest_qp_ip()
        );
        // FIXME: use actual addr
        let dqp_ip = attr.dest_qp_ip().unwrap_or(Ipv4Addr::new(0, 0, 0, 0));
        let dqpn = attr.dest_qp_num();

        let result = self.qp_ctx_table.map_qp_mut(qpn, |ctx| {
            if dqpn.is_some() {
                ctx.dpqn = dqpn;
            }
            if dqp_ip != Ipv4Addr::new(0, 0, 0, 0) {
                ctx.dpq_ip = Some(dqp_ip);
            }
            if let Some((dqpn, dqp_ip)) = ctx.dpqn.zip(ctx.dpq_ip) {
                info!("connect to dqpn: {dqpn}, ip: {dqp_ip}");
                ctx.conn().connect(dqpn, dqp_ip);
            } else if let Some(dqpn) = ctx.dpqn {
                let fallback_ip = Ipv4Addr::LOCALHOST;
                info!("dest ip not provided for qp {qpn}, defaulting to loopback {fallback_ip}");
                ctx.dpq_ip = Some(fallback_ip);
                ctx.conn().connect(dqpn, fallback_ip);
            }
        });

        if result.is_none() {
            return Err(RdmaError::QpError(format!("QP {qpn} not found",)));
        }

        info!("mock update qp: {qpn}, peer qp: {dqpn:?}");

        Ok(())
    }

    fn destroy_qp(&mut self, qpn: u32) -> crate::error::Result<()> {
        info!("destroying qp: {qpn}");
        _ = self.qp_ctx_table.map_qp_mut(qpn, move |ctx| {
            ctx.abort_signal
                .take()
                .unwrap()
                .store(true, Ordering::Relaxed);
            ctx.handle.take().unwrap().join().unwrap();
        });

        // Free the QPN in the bitmap
        if !self.qp_manager.destroy_qp(qpn) {
            warn!("QPN {qpn} was not allocated in QpManager");
        }

        info!("qp: {qpn} destroyed");

        Ok(())
    }

    fn create_cq(&mut self) -> crate::error::Result<u32> {
        self.cq_handle += 1;
        let _ignore = self.cq_table.insert(self.cq_handle, CompletionQueue::new());
        info!("mock create cq, handle: {}", self.cq_handle);
        Ok(self.cq_handle)
    }

    //TODO 需要完善实现
    fn destroy_cq(&mut self, handle: u32) -> crate::error::Result<()> {
        info!("mock destroy cq, handle: {handle}");

        Ok(())
    }

    fn poll_cq(&mut self, handle: u32, max_num_entries: usize) -> Vec<Completion> {
        let completions = if let Some(cq) = self.cq_table.get_mut(&handle) {
            iter::repeat_with(|| cq.pop())
                .take_while(Option::is_some)
                .take(max_num_entries)
                .flatten()
                .collect()
        } else {
            vec![]
        };
        if !completions.is_empty() {
            info!("poll cq, cq handle: {handle}, completions: {completions:?}");
        }
        completions
    }

    fn post_send(&mut self, qpn: u32, wr: SendWr) -> crate::error::Result<()> {
        info!("post send wr: {wr:?}, qpn: {qpn}");
        let ack_req = wr.send_flags() & ibverbs_sys::ibv_send_flags::IBV_SEND_SIGNALED.0 != 0;
        let to_send = match wr {
            SendWr::Rdma(x) => match x.opcode() {
                WorkReqOpCode::RdmaWrite => {
                    let data = read_local_addr(x.laddr().as_u64(), x.length() as usize);
                    QpTransportMessage::WriteReq(RdmaWriteReq {
                        raddr: x.raddr().as_u64(),
                        imm: x.imm(),
                        wr_id: x.wr_id(),
                        data,
                        ack_req,
                    })
                }
                WorkReqOpCode::RdmaWriteWithImm => {
                    let data = read_local_addr(x.laddr().as_u64(), x.length() as usize);
                    QpTransportMessage::WriteWithImmReq(RdmaWriteReq {
                        raddr: x.raddr().as_u64(),
                        imm: x.imm(),
                        wr_id: x.wr_id(),
                        data,
                        ack_req,
                    })
                }
                WorkReqOpCode::RdmaRead => QpTransportMessage::ReadReq(RdmaReadReq {
                    raddr: x.raddr().as_u64(),
                    wr_id: x.wr_id(),
                    ack_req,
                    laddr: x.laddr().as_u64(),
                    len: x.length(),
                }),
                _ => {
                    return Err(RdmaError::Unimplemented(format!(
                        "Unsupported opcode: {:?}",
                        x.opcode()
                    )))
                }
            },
            SendWr::Send(x) => {
                let data = read_local_addr(x.laddr.as_u64(), x.length as usize);
                QpTransportMessage::SendReq(SendReq {
                    data,
                    wr_id: wr.wr_id(),
                    imm: (wr.imm_data() != 0).then_some(wr.imm_data()),
                    ack_req,
                })
            }
        };

        let result = self
            .qp_ctx_table
            .map_qp_mut(qpn, |ctx| ctx.conn().send(to_send));

        if result.is_none() {
            return Err(RdmaError::QpError(format!("QP {qpn} not found",)));
        }

        Ok(())
    }

    fn post_recv(&mut self, qpn: u32, wr: RecvWr) -> crate::error::Result<()> {
        info!("post recv wr: {wr:?}, qpn: {qpn}");

        // 获取指定 QPN 的 sender
        let result = self.qp_ctx_table.map_qp_mut(qpn, |ctx| {
            if let Some(tx) = ctx.local_task_tx.as_ref() {
                tx.send(LocalTask::PostRecv(PostRecvReq { wr }))
                    .map_err(|e| {
                        RdmaError::QpError(format!(
                            "Failed to post receive request for QP {qpn}: {e}"
                        ))
                    })
            } else {
                Err(RdmaError::QpError(format!(
                    "Task channel not initialized for QP {qpn}"
                )))
            }
        });

        match result {
            Some(Ok(())) => Ok(()),
            Some(Err(e)) => Err(e),
            None => Err(RdmaError::QpError(format!("QP {qpn} not found"))),
        }
    }

    fn alloc_pd(&mut self) -> crate::error::Result<u32> {
        self.pd_table
            .alloc()
            .ok_or(RdmaError::ResourceExhausted("No PD available".into()))
    }

    fn dealloc_pd(&mut self, handle: u32) -> crate::error::Result<()> {
        if self.pd_table.dealloc(handle) {
            Ok(())
        } else {
            Err(RdmaError::InvalidInput(format!(
                "PD handle {handle} not present"
            )))
        }
    }
}

fn read_local_addr(addr: u64, len: usize) -> Vec<u8> {
    // Handle zero-length reads to avoid undefined behavior with null pointers
    if len == 0 {
        return Vec::new();
    }
    let mut data = vec![0u8; len];
    let slice = unsafe { std::slice::from_raw_parts(addr as *const u8, len) };
    data.copy_from_slice(slice);
    data
}

#[cfg(test)]
fn write_local_addr(table: &MrTable, addr: u64, data: &[u8]) {
    // Handle zero-length writes to avoid undefined behavior with null pointers
    if data.is_empty() {
        return;
    }
    unsafe {
        ptr::copy_nonoverlapping(data.as_ptr(), addr as *mut u8, data.len());
    }
}

#[cfg(not(test))]
fn write_local_addr(table: &MrTable, addr: u64, data: &[u8]) {
    // Handle zero-length writes to avoid undefined behavior with null pointers
    if data.is_empty() {
        return;
    }
    if table.valid(addr, data.len()) {
        debug!("valid mr, addr: {addr:x}, length: {}", data.len());
        unsafe {
            ptr::copy_nonoverlapping(data.as_ptr(), addr as *mut u8, data.len());
        }
    } else {
        warn!("invalid mr, addr: {addr:x}, length: {}", data.len());
    }
}

#[derive(Debug, Default)]
struct MrTable {
    inner: Arc<Mutex<Vec<Mr>>>,
}

impl MrTable {
    fn clone_arc(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
        }
    }

    fn reg(&self, mr: Mr) {
        self.inner.lock().push(mr);
    }

    fn valid(&self, a: u64, l: usize) -> bool {
        let inner = self.inner.lock();
        inner
            .iter()
            .any(|&Mr { addr, length }| a >= addr && (a + l as u64) < addr + length as u64)
    }
}

#[derive(Debug, Clone, Copy)]
struct Mr {
    addr: u64,
    length: usize,
}

impl Mr {
    fn new(addr: u64, length: usize) -> Self {
        Self { addr, length }
    }
}

#[derive(Debug, Clone)]
struct CompletionQueue {
    inner: Arc<Mutex<VecDeque<Completion>>>,
}

impl CompletionQueue {
    fn new() -> Self {
        CompletionQueue {
            inner: Arc::new(Mutex::new(VecDeque::new())),
        }
    }

    fn push(&self, completion: Completion) {
        self.inner.lock().push_back(completion);
    }

    fn pop(&self) -> Option<Completion> {
        self.inner.lock().pop_front()
    }
}

#[derive(Debug)]
enum LocalTask {
    PostRecv(PostRecvReq),
}

#[derive(Debug)]
struct PostRecvReq {
    wr: RecvWr,
}

#[derive(Encode, Decode, Serialize, Deserialize)]
struct RdmaWriteReq {
    raddr: u64,
    imm: u32,
    data: Vec<u8>,
    wr_id: u64,
    ack_req: bool,
}

impl fmt::Debug for RdmaWriteReq {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RdmaWriteReq")
            .field("raddr", &self.raddr)
            .field("imm", &self.imm)
            .field("data", &format!("<{} bytes>", self.data.len()))
            .field("wr_id", &self.wr_id)
            .field("ack_req", &self.ack_req)
            .finish()
    }
}

#[derive(Encode, Decode, Debug, Serialize, Deserialize)]
struct RdmaReadReq {
    laddr: u64,
    raddr: u64,
    len: u32,
    ack_req: bool,
    wr_id: u64,
}

#[derive(Encode, Decode, Serialize, Deserialize)]
struct RdmaReadResp {
    laddr: u64,
    raddr: u64,
    data: Vec<u8>,
    wr_id: u64,
    ack_req: bool,
}

impl fmt::Debug for RdmaReadResp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RdmaReadResp")
            .field("laddr", &self.laddr)
            .field("raddr", &self.raddr)
            .field("data", &format!("<{} bytes>", self.data.len()))
            .field("wr_id", &self.wr_id)
            .field("ack_req", &self.ack_req)
            .finish()
    }
}

#[derive(Encode, Decode, Debug, Serialize, Deserialize)]
struct WriteOrSendResp {
    wr_id: u64,
    ack_req: bool,
}

#[derive(Encode, Decode, Serialize, Deserialize)]
struct SendReq {
    wr_id: u64,
    data: Vec<u8>,
    imm: Option<u32>,
    ack_req: bool,
}

impl fmt::Debug for SendReq {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SendReq")
            .field("wr_id", &self.wr_id)
            .field("data", &format!("<{} bytes>", self.data.len()))
            .field("imm", &self.imm)
            .field("ack_req", &self.ack_req)
            .finish()
    }
}

#[derive(Encode, Decode, Debug, Serialize, Deserialize)]
enum QpTransportMessage {
    WriteReq(RdmaWriteReq),
    WriteWithImmReq(RdmaWriteReq),
    ReadReq(RdmaReadReq),
    SendReq(SendReq),

    WriteResp(WriteOrSendResp),
    WriteWithImmResp(WriteOrSendResp),
    ReadResp(RdmaReadResp),
    SendResp(WriteOrSendResp),
}

fn get_port(qpn: u32) -> u16 {
    PORT_START_ADDR + qpn as u16
}

#[derive(Debug, Clone)]
struct QpConnetion {
    qpn: u32,
    inner: Arc<Inner>,
}

impl Drop for QpConnetion {
    fn drop(&mut self) {
        info!("dropping qp connection for qpn: {}", self.qpn);
    }
}

impl QpConnetion {
    fn new(self_ip: Ipv4Addr, qpn: u32) -> Self {
        let inner = Inner::new(self_ip, qpn);
        Self {
            qpn,
            inner: Arc::new(inner),
        }
    }

    fn connect(&self, qpn: u32, addr: Ipv4Addr) {
        self.inner.connect(addr, qpn);
    }

    fn send<T: Encode>(&self, data: T) {
        self.inner.send(data);
    }

    fn recv<T: Decode<()>>(&self) -> Option<T> {
        self.inner.recv()
    }
}

const PORT_START_ADDR: u16 = 10000;

#[derive(Debug)]
struct Inner {
    listener: Mutex<TcpListener>,
    rx_chan: Mutex<Option<BufReader<TcpStream>>>,
    tx_chan: Mutex<Option<TxChan>>,
    addr: Mutex<Option<(Ipv4Addr, u16)>>,
    qpn: u32,
}

#[derive(Debug)]
struct TxChan {
    tx_chan: TcpStream,
    qpn: u32,
}

impl Write for TxChan {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.tx_chan.write(buf)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.tx_chan.flush()
    }
}

impl Drop for TxChan {
    fn drop(&mut self) {
        warn!("dropping TxChan, qpn: {}", self.qpn);
    }
}

impl Inner {
    fn new(ip: Ipv4Addr, qpn: u32) -> Self {
        log::info!("device binding to {}:{}", ip, Self::get_port(qpn));
        let rx_chan = TcpListener::bind((ip, Self::get_port(qpn))).expect("failed to bind to addr");
        rx_chan.set_nonblocking(true).unwrap();
        Self {
            listener: Mutex::new(rx_chan),
            rx_chan: Mutex::default(),
            tx_chan: Mutex::default(),
            addr: Mutex::default(),
            qpn,
        }
    }

    fn connect(&self, addr: Ipv4Addr, qpn: u32) {
        _ = self.addr.lock().replace((addr, Self::get_port(qpn)));
    }

    fn get_port(qpn: u32) -> u16 {
        PORT_START_ADDR + qpn as u16
    }

    fn send<T: Encode>(&self, data: T) {
        let mut tx_chan = self.tx_chan.lock();
        if tx_chan.is_none() {
            let addr = self.addr.lock().unwrap();
            let stream = TcpStream::connect(addr).unwrap();
            let chan = TxChan {
                tx_chan: stream,
                qpn: self.qpn,
            };
            _ = tx_chan.replace(chan);
        }
        let tx = tx_chan.as_mut().unwrap();
        tx.write_all(&bincode::encode_to_vec(data, bincode::config::standard()).unwrap())
            .unwrap();
    }

    fn recv<T: Decode<()>>(&self) -> Option<T> {
        if self.rx_chan.lock().is_none() {
            let listener = self.listener.lock();
            match listener.accept() {
                Ok((stream, _)) => {
                    let _ = stream.set_read_timeout(Some(Duration::from_millis(1)));
                    _ = self.rx_chan.lock().replace(BufReader::new(stream));
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(1));
                    return None;
                }
                Err(e) => {
                    error!("failed to accept new stream: {e}");
                    return None;
                }
            }
        }
        let mut rx_l = self.rx_chan.lock();
        bincode::decode_from_reader(rx_l.as_mut().unwrap(), bincode::config::standard()).ok()
    }
}

#[cfg(test)]
mod tests {
    use crate::{
        rdma_utils::types::{SendWrBase, SendWrRdma},
        types::{RemoteAddr, VirtAddr},
    };

    use super::*;
    use bincode::{Decode, Encode};
    use serde::{Deserialize, Serialize};
    use std::net::Ipv4Addr;
    use std::thread;
    use std::time::Duration;

    #[derive(Encode, Decode, Debug, PartialEq, Serialize, Deserialize)]
    struct TestMessage {
        id: u32,
        content: String,
    }

    #[test]
    fn test_tcp_channel() {
        let qpn0 = 10;
        let qpn1 = 11;
        let server = Inner::new(Ipv4Addr::LOCALHOST, qpn0);
        server.connect(Ipv4Addr::LOCALHOST, qpn1);

        thread::spawn(move || {
            let client = Inner::new(Ipv4Addr::LOCALHOST, qpn1);
            client.connect(Ipv4Addr::LOCALHOST, qpn0);
            let msg = TestMessage {
                id: 1,
                content: "foo".into(),
            };
            client.send(msg);
        });
        std::thread::sleep(Duration::from_millis(10));

        let received: TestMessage = server.recv().unwrap();
        assert_eq!(
            received,
            TestMessage {
                id: 1,
                content: "foo".into()
            }
        );
    }

    struct Ctx {
        dev: MockDeviceCtx,
        cq: u32,
        qpn: u32,
        ip: Ipv4Addr,
    }

    fn create_dev(ip: Ipv4Addr) -> Ctx {
        let mut dev = MockDeviceCtx::default();
        dev.self_ip = ip.to_bits();
        let cq = dev.create_cq().unwrap();
        let mut attr_init = IbvQpInitAttr::new_rc();
        attr_init.send_cq = Some(cq);
        attr_init.recv_cq = Some(cq);
        let qpn = dev.create_qp(attr_init).unwrap();
        Ctx { dev, cq, qpn, ip }
    }

    fn handshake(x: &mut Ctx, y: &Ctx) {
        let mut attr = IbvQpAttr::default();
        attr.dest_qp_num = Some(y.qpn);
        attr.dest_qp_ip = Some(y.ip);
        x.dev.update_qp(x.qpn, attr).unwrap();
    }

    #[test]
    fn rdma_write_basic() {
        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        let buf0 = Box::new([1u8; 128]);
        let buf1 = Box::new([0u8; 128]);
        let wr_base = SendWrBase::new(
            0,
            0,
            VirtAddr::new(buf0.as_ptr() as u64),
            buf0.len() as u32,
            0,
            0,
            WorkReqOpCode::RdmaWrite,
        );
        let wr = SendWrRdma::new_from_base(
            wr_base,
            RemoteAddr::new(buf1.as_ptr() as u64),
            buf1.len() as u32,
        );
        dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        thread::sleep(Duration::from_millis(10));
        assert!(buf1.iter().all(|x| *x == 1));
    }

    #[test]
    fn rdma_write_with_completion() {
        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        // Post receive buffer for RDMA_WRITE_WITH_IMM (num_sge = 0)
        let recv_wr = RecvWr {
            wr_id: 0,
            addr: VirtAddr::new(0),
            length: 0,
            lkey: 0,
        };
        dev1.dev.post_recv(dev1.qpn, recv_wr).unwrap();

        let buf0 = Box::new([1u8; 128]);
        let buf1 = Box::new([0u8; 128]);
        let wr_base = SendWrBase::new(
            0,
            ibverbs_sys::ibv_send_flags::IBV_SEND_SIGNALED.0,
            VirtAddr::new(buf0.as_ptr() as u64),
            buf0.len() as u32,
            0,
            0,
            WorkReqOpCode::RdmaWriteWithImm,
        );
        let wr = SendWrRdma::new_from_base(
            wr_base,
            RemoteAddr::new(buf1.as_ptr() as u64),
            buf1.len() as u32,
        );
        dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        thread::sleep(Duration::from_millis(10));
        assert!(buf1.iter().all(|x| *x == 1));
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 1);
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 1);
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 0);
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 0);
    }

    #[test]
    fn rdma_write_with_completion_multi() {
        const NUM_WRITES: usize = 10;

        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        // Post receive buffers for RDMA_WRITE_WITH_IMM (num_sge = 0)
        for i in 0..NUM_WRITES {
            let recv_wr = RecvWr {
                wr_id: i as u64,
                addr: VirtAddr::new(0),
                length: 0,
                lkey: 0,
            };
            dev1.dev.post_recv(dev1.qpn, recv_wr).unwrap();
        }

        let buf0 = Box::new([1u8; 128]);
        let buf1 = Box::new([0u8; 128]);
        let wr_base = SendWrBase::new(
            0,
            ibverbs_sys::ibv_send_flags::IBV_SEND_SIGNALED.0,
            VirtAddr::new(buf0.as_ptr() as u64),
            buf0.len() as u32,
            0,
            0,
            WorkReqOpCode::RdmaWriteWithImm,
        );
        for _ in 0..NUM_WRITES {
            let wr = SendWrRdma::new_from_base(
                wr_base,
                RemoteAddr::new(buf1.as_ptr() as u64),
                buf1.len() as u32,
            );
            dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        }
        thread::sleep(Duration::from_millis(10));
        assert!(buf1.iter().all(|x| *x == 1));
        for _ in 0..NUM_WRITES {
            assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 1);
            assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 1);
        }
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 0);
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 0);
    }

    #[test]
    fn rdma_read() {
        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        let buf0 = Box::new([0u8; 128]);
        let buf1 = Box::new([1u8; 128]);
        let wr_base = SendWrBase::new(
            0,
            0,
            VirtAddr::new(buf0.as_ptr() as u64),
            buf0.len() as u32,
            0,
            0,
            WorkReqOpCode::RdmaRead,
        );
        let wr = SendWrRdma::new_from_base(
            wr_base,
            RemoteAddr::new(buf1.as_ptr() as u64),
            buf1.len() as u32,
        );
        dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        thread::sleep(Duration::from_millis(10));
        assert!(buf0.iter().all(|x| *x == 1));
    }

    #[test]
    fn rdma_read_with_completion() {
        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        let buf0 = Box::new([0u8; 128]);
        let buf1 = Box::new([1u8; 128]);
        let wr_base = SendWrBase::new(
            0,
            ibverbs_sys::ibv_send_flags::IBV_SEND_SIGNALED.0,
            VirtAddr::new(buf0.as_ptr() as u64),
            buf0.len() as u32,
            0,
            0,
            WorkReqOpCode::RdmaRead,
        );
        let wr = SendWrRdma::new_from_base(
            wr_base,
            RemoteAddr::new(buf1.as_ptr() as u64),
            buf1.len() as u32,
        );
        dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        thread::sleep(Duration::from_millis(10));
        assert!(buf0.iter().all(|x| *x == 1));
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 1);
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 0);
    }

    #[test]
    fn send_recv() {
        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        let buf0 = Box::new([1u8; 128]);
        let buf1 = Box::new([0u8; 128]);

        let recv_wr = RecvWr {
            wr_id: 0,
            addr: VirtAddr::new(buf1.as_ptr() as u64),
            length: buf1.len() as u32,
            lkey: 0,
        };
        dev1.dev.post_recv(dev1.qpn, recv_wr).unwrap();

        let wr = SendWrBase::new(
            0,
            0,
            VirtAddr::new(buf0.as_ptr() as u64),
            buf0.len() as u32,
            0,
            0,
            WorkReqOpCode::Send,
        );
        dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        thread::sleep(Duration::from_millis(10));
        assert!(buf1.iter().all(|x| *x == 1));
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 1);
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 0);
    }

    #[test]
    fn send_recv_with_completion() {
        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        let buf0 = Box::new([1u8; 128]);
        let buf1 = Box::new([0u8; 128]);

        let recv_wr = RecvWr {
            wr_id: 0,
            addr: VirtAddr::new(buf1.as_ptr() as u64),
            length: buf1.len() as u32,
            lkey: 0,
        };
        dev1.dev.post_recv(dev1.qpn, recv_wr).unwrap();

        let wr = SendWrBase::new(
            0,
            ibverbs_sys::ibv_send_flags::IBV_SEND_SIGNALED.0,
            VirtAddr::new(buf0.as_ptr() as u64),
            buf0.len() as u32,
            0,
            0,
            WorkReqOpCode::Send,
        );
        dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        thread::sleep(Duration::from_millis(10));
        assert!(buf1.iter().all(|x| *x == 1));
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 1);
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 1);
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 0);
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 0);
    }

    #[test]
    fn send_recv_with_completion_multi() {
        const NUM_SEND_RECV: usize = 10;

        let mut dev0 = create_dev(Ipv4Addr::new(127, 0, 0, 1));
        let mut dev1 = create_dev(Ipv4Addr::new(127, 0, 0, 2));
        handshake(&mut dev0, &dev1);
        handshake(&mut dev1, &dev0);

        let buf0 = Box::new([1u8; 128]);
        let buf1 = Box::new([0u8; 128]);

        for _ in 0..NUM_SEND_RECV {
            let recv_wr = RecvWr {
                wr_id: 0,
                addr: VirtAddr::new(buf1.as_ptr() as u64),
                length: buf1.len() as u32,
                lkey: 0,
            };
            dev1.dev.post_recv(dev1.qpn, recv_wr).unwrap();
        }

        for _ in 0..NUM_SEND_RECV {
            let wr = SendWrBase::new(
                0,
                ibverbs_sys::ibv_send_flags::IBV_SEND_SIGNALED.0,
                VirtAddr::new(buf0.as_ptr() as u64),
                buf0.len() as u32,
                0,
                0,
                WorkReqOpCode::Send,
            );
            dev0.dev.post_send(dev0.qpn, wr.into()).unwrap();
        }
        thread::sleep(Duration::from_millis(10));
        assert!(buf1.iter().all(|x| *x == 1));

        for _ in 0..NUM_SEND_RECV {
            assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 1);
            assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 1);
        }
        assert_eq!(dev0.dev.poll_cq(dev0.cq, 1).len(), 0);
        assert_eq!(dev1.dev.poll_cq(dev1.cq, 1).len(), 0);
    }
}
