use std::{collections::VecDeque, iter, sync::Arc};

use bitvec::vec::BitVec;
use crossbeam_queue::SegQueue;
use log::debug;

use crate::{
    constants::MAX_CQ_CNT,
    rdma_utils::{
        msn::Msn,
        psn::Psn,
        qp::{QpTable, QpTableShared},
        types::QpAttr,
    },
    workers::{
        ack_responder::AckResponse,
        qp_timeout::AckTimeoutTask,
        rdma::RdmaWriteTask,
        spawner::{SingleThreadTaskWorker, TaskTx},
    },
};

struct EventRegister {
    message_id: MessageIdentifier,
    event: Event,
}

struct Message {
    id: MessageIdentifier,
    meta: MessageMeta,
}

struct MessageIdentifier {
    qpn: u32,
    msn: u16,
    is_send: bool,
}

struct CompletionQueueRegistry {
    inner: VecDeque<EventRegister>,
}

impl CompletionQueueRegistry {
    fn push(&mut self, register: EventRegister) {
        self.inner.push_back(register);
    }

    fn pop() -> Option<EventRegister> {
        None
    }
}

#[derive(Debug, PartialEq, Eq)]
#[allow(variant_size_differences)]
pub(crate) enum CompletionTask {
    Register { qpn: u32, event: Event },
    AckSend { qpn: u32, base_psn: Psn },
    AckRecv { qpn: u32, base_psn: Psn },
}

pub(crate) struct CompletionWorker {
    tracker_table: QpTable<QueuePairMessageTracker>,
    cq_table: CompletionQueueTable,
    qp_table: QpTableShared<QpAttr>,
    ack_resp_tx: TaskTx<AckResponse>,
    ack_timeout_tx: TaskTx<AckTimeoutTask>,
    rdma_write_tx: TaskTx<RdmaWriteTask>,
}

impl SingleThreadTaskWorker for CompletionWorker {
    type Task = CompletionTask;

    fn process(&mut self, task: Self::Task) {
        debug!("CompletionWorker got task: {:?}", task);
        let qpn = match task {
            CompletionTask::Register { qpn, .. }
            | CompletionTask::AckSend { qpn, .. }
            | CompletionTask::AckRecv { qpn, .. } => qpn,
        };
        let tracker = self
            .tracker_table
            .get_qp_mut(qpn)
            .expect("invalid qpn: {qpn}");
        let qp_attr = self.qp_table.get_qp(qpn).expect("invalid qpn: {qpn}");
        match task {
            CompletionTask::Register { event, .. } => {
                tracker.append(event);
            }
            CompletionTask::AckSend { base_psn, .. } => {
                let handle = qp_attr.send_cq.expect("no associated cq");
                let send_cq = self.cq_table.get_cq(handle).expect("invalid cq: {handle}");
                tracker.ack_send(base_psn);
                while let Some((event, completion)) = tracker.poll_send_completion() {
                    send_cq.push_back(completion);
                    self.ack_timeout_tx.send(AckTimeoutTask::ack(qpn));
                    self.rdma_write_tx
                        .send(RdmaWriteTask::new_complete(qpn, event.meta().msn));
                }
            }
            CompletionTask::AckRecv { base_psn, .. } => {
                let send_handle = qp_attr.send_cq.expect("no associated cq");
                let recv_handle = qp_attr.recv_cq.expect("no associated cq");
                let send_cq = self
                    .cq_table
                    .get_cq(send_handle)
                    .expect("invalid cq: {send_handle}");
                let recv_cq = self
                    .cq_table
                    .get_cq(recv_handle)
                    .expect("invalid cq: {send_handle}");
                tracker.ack_recv(base_psn);
                while let Some((event, completion)) = tracker.poll_send_completion() {
                    send_cq.push_back(completion);
                    self.ack_timeout_tx.send(AckTimeoutTask::ack(qpn));
                    self.rdma_write_tx
                        .send(RdmaWriteTask::new_complete(qpn, event.meta().msn));
                }
                while let Some((event, completion)) = tracker.poll_recv_completion() {
                    if event.ack_req {
                        self.ack_resp_tx.send(AckResponse::Ack {
                            qpn,
                            msn: event.meta().msn,
                            last_psn: event.meta().end_psn,
                        });
                    }
                    if let Some(c) = completion {
                        recv_cq.push_back(c);
                    }
                }
            }
        }
    }

    fn maintainance(&mut self) {}
}

impl CompletionWorker {
    pub(crate) fn new(
        cq_table: CompletionQueueTable,
        qp_table: QpTableShared<QpAttr>,
        ack_resp_tx: TaskTx<AckResponse>,
        ack_timeout_tx: TaskTx<AckTimeoutTask>,
        rdma_write_tx: TaskTx<RdmaWriteTask>,
    ) -> Self {
        Self {
            tracker_table: QpTable::new(),
            cq_table,
            qp_table,
            ack_resp_tx,
            ack_timeout_tx,
            rdma_write_tx,
        }
    }
}

pub(crate) struct EventWithQpn {
    qpn: u32,
    event: Event,
}

impl EventWithQpn {
    pub(crate) fn new(qpn: u32, event: Event) -> Self {
        Self { qpn, event }
    }
}

/// Used for merge a read request/response
#[derive(Default)]
struct MergeQueue {
    send: VecDeque<SendEvent>,
    recv: VecDeque<RecvEvent>,
    recv_read_resp: VecDeque<RecvEvent>,
}

impl MergeQueue {
    fn push_send(&mut self, event: SendEvent) {
        self.send.push_back(event);
    }

    fn push_recv(&mut self, event: RecvEvent) {
        match event.op {
            RecvEventOp::ReadResp => {
                self.recv_read_resp.push_back(event);
            }
            RecvEventOp::Write
            | RecvEventOp::WriteWithImm { .. }
            | RecvEventOp::Recv
            | RecvEventOp::RecvWithImm { .. }
            | RecvEventOp::RecvRead => {
                self.recv.push_back(event);
            }
        }
    }

    fn pop_send(&mut self) -> Option<SendEvent> {
        let event = self.send.front()?;
        match event.op {
            SendEventOp::WriteSignaled | SendEventOp::SendSignaled => self.send.pop_front(),
            SendEventOp::ReadSignaled => self
                .recv_read_resp
                .pop_front()
                .and_then(|_e| self.send.pop_front()),
        }
    }

    fn pop_recv(&mut self) -> Option<RecvEvent> {
        self.recv.pop_front()
    }
}

#[derive(Default)]
struct QueuePairMessageTracker {
    send: MessageTracker<SendEvent>,
    recv: MessageTracker<RecvEvent>,
    read_resp_queue: VecDeque<RecvEvent>,
    post_recv_queue: VecDeque<PostRecvEvent>,
    merge: MergeQueue,
}

impl QueuePairMessageTracker {
    fn append(&mut self, event: Event) {
        match event {
            Event::Send(x) => {
                debug!(
                    "QueuePairMessageTracker::append Send: qpn={} op={:?} meta={:?} wr_id={}",
                    x.qpn, x.op, x.meta, x.wr_id
                );
                self.send.append(x);
            }
            Event::Recv(x) => {
                debug!(
                    "QueuePairMessageTracker::append Recv: qpn={} op={:?} meta={:?} ack_req={}",
                    x.qpn, x.op, x.meta, x.ack_req
                );
                self.recv.append(x);
            }
            Event::PostRecv(x) => {
                debug!(
                    "QueuePairMessageTracker::append PostRecv: qpn={} wr_id={}",
                    x.qpn, x.wr_id
                );
                self.post_recv_queue.push_back(x);
            }
        }
    }

    fn ack_send(&mut self, psn: Psn) {
        self.send.ack(psn);
        while let Some(event) = self.send.pop() {
            self.merge.push_send(event);
        }
    }

    fn ack_recv(&mut self, psn: Psn) {
        self.recv.ack(psn);
        while let Some(event) = self.recv.pop() {
            self.merge.push_recv(event);
        }
    }

    fn poll_send_completion(&mut self) -> Option<(SendEvent, Completion)> {
        let event = self.merge.pop_send()?;
        let completion = match event.op {
            SendEventOp::WriteSignaled => Completion::RdmaWrite { wr_id: event.wr_id },
            SendEventOp::SendSignaled => Completion::Send { wr_id: event.wr_id },
            SendEventOp::ReadSignaled => Completion::RdmaRead { wr_id: event.wr_id },
        };

        Some((event, completion))
    }

    fn poll_recv_completion(&mut self) -> Option<(RecvEvent, Option<Completion>)> {
        let event = self.merge.pop_recv()?;
        let completion = match event.op {
            RecvEventOp::WriteWithImm { imm } => {
                let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
                Some(Completion::RecvRdmaWithImm {
                    wr_id: x.wr_id,
                    imm,
                })
            }
            RecvEventOp::Recv => {
                let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
                Some(Completion::Recv {
                    wr_id: x.wr_id,
                    imm: None,
                })
            }
            RecvEventOp::RecvWithImm { imm } => {
                let x = self.post_recv_queue.pop_front().expect("no posted recv wr");
                Some(Completion::Recv {
                    wr_id: x.wr_id,
                    imm: Some(imm),
                })
            }
            RecvEventOp::ReadResp => unreachable!("invalid branch"),
            RecvEventOp::RecvRead | RecvEventOp::Write => None,
        };

        Some((event, completion))
    }
}

#[derive(Debug)]
struct MessageTracker<E> {
    inner: VecDeque<E>,
    base_psn: Psn,
}

impl<E> Default for MessageTracker<E> {
    fn default() -> Self {
        Self {
            inner: VecDeque::default(),
            base_psn: Psn::default(),
        }
    }
}

impl<E: EventMeta> MessageTracker<E> {
    fn append(&mut self, event: E) {
        let pos = self
            .inner
            .iter()
            .rev()
            .position(|e| Msn(e.meta().msn) < Msn(event.meta().msn))
            .unwrap_or(self.inner.len());
        let index = self.inner.len() - pos;
        if self
            .inner
            .get(index)
            .is_none_or(|e| e.meta().msn != event.meta().msn)
        {
            self.inner.insert(index, event);
        }
    }

    fn ack(&mut self, base_psn: Psn) {
        self.base_psn = base_psn;
    }

    fn peek(&self) -> Option<&E> {
        let front = self.inner.front()?;
        (front.meta().end_psn <= self.base_psn).then_some(front)
    }

    fn pop(&mut self) -> Option<E> {
        let front = self.inner.front()?;
        if front.meta().end_psn <= self.base_psn {
            self.inner.pop_front()
        } else {
            None
        }
    }
}

trait EventMeta {
    fn meta(&self) -> MessageMeta;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Event {
    // Generated when verbs that will transmit packet called, e.g., read or write
    Send(SendEvent),
    // Generated when NIC receive all kinds of header meta.
    Recv(RecvEvent),
    //Generated when verbs post_recv api called
    PostRecv(PostRecvEvent),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct SendEvent {
    qpn: u32,
    op: SendEventOp,
    meta: MessageMeta,
    wr_id: u64,
}

impl SendEvent {
    pub(crate) fn new(qpn: u32, op: SendEventOp, meta: MessageMeta, wr_id: u64) -> Self {
        Self {
            qpn,
            op,
            meta,
            wr_id,
        }
    }
}

impl EventMeta for SendEvent {
    fn meta(&self) -> MessageMeta {
        self.meta
    }
}

#[allow(clippy::enum_variant_names)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SendEventOp {
    WriteSignaled,
    SendSignaled,
    ReadSignaled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct RecvEvent {
    pub(crate) qpn: u32,
    pub(crate) op: RecvEventOp,
    pub(crate) meta: MessageMeta,
    pub(crate) ack_req: bool,
}

impl RecvEvent {
    pub(crate) fn new(qpn: u32, op: RecvEventOp, meta: MessageMeta, ack_req: bool) -> Self {
        Self {
            qpn,
            op,
            meta,
            ack_req,
        }
    }
}

impl EventMeta for RecvEvent {
    fn meta(&self) -> MessageMeta {
        self.meta
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RecvEventOp {
    Write,
    WriteWithImm { imm: u32 },
    Recv,
    RecvWithImm { imm: u32 },
    ReadResp,
    RecvRead,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct PostRecvEvent {
    qpn: u32,
    wr_id: u64,
}

impl PostRecvEvent {
    pub(crate) fn new(qpn: u32, wr_id: u64) -> Self {
        Self { qpn, wr_id }
    }
}

#[derive(Default, Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct MessageMeta {
    pub(crate) msn: u16,
    pub(crate) end_psn: Psn,
}

impl MessageMeta {
    pub(crate) fn new(msn: u16, end_psn: Psn) -> Self {
        Self { msn, end_psn }
    }
}

pub(crate) struct CompletionQueueTable {
    inner: Arc<[CompletionQueue]>,
}

impl CompletionQueueTable {
    pub(crate) fn new() -> Self {
        Self {
            inner: iter::repeat_with(CompletionQueue::default)
                .take(MAX_CQ_CNT)
                .collect(),
        }
    }

    pub(crate) fn clone_arc(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
        }
    }

    pub(crate) fn get_cq(&self, handle: u32) -> Option<&CompletionQueue> {
        self.inner.get(handle as usize)
    }
}

#[derive(Default)]
pub(crate) struct CompletionQueue {
    inner: SegQueue<Completion>,
}

impl CompletionQueue {
    pub(crate) fn push_back(&self, event: Completion) {
        self.inner.push(event);
    }

    pub(crate) fn pop_front(&self) -> Option<Completion> {
        self.inner.pop()
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum Completion {
    Send { wr_id: u64 },
    RdmaWrite { wr_id: u64 },
    RdmaRead { wr_id: u64 },
    Recv { wr_id: u64, imm: Option<u32> },
    RecvRdmaWithImm { wr_id: u64, imm: u32 },
}

impl Completion {
    pub(crate) fn opcode(&self) -> u32 {
        match *self {
            Completion::Send { .. } => ibverbs_sys::ibv_wc_opcode::IBV_WC_SEND,
            Completion::RdmaWrite { .. } => ibverbs_sys::ibv_wc_opcode::IBV_WC_RDMA_WRITE,
            Completion::RdmaRead { .. } => ibverbs_sys::ibv_wc_opcode::IBV_WC_RDMA_READ,
            Completion::Recv { .. } => ibverbs_sys::ibv_wc_opcode::IBV_WC_RECV,
            Completion::RecvRdmaWithImm { .. } => {
                ibverbs_sys::ibv_wc_opcode::IBV_WC_RECV_RDMA_WITH_IMM
            }
        }
    }
}

/// Manages CQs
pub(crate) struct CqManager {
    /// Bitmap tracking allocated CQ handles
    bitmap: BitVec,
}

#[allow(clippy::as_conversions, clippy::indexing_slicing)]
impl CqManager {
    /// Creates a new `CqManager`
    pub(crate) fn new() -> Self {
        let mut bitmap = BitVec::with_capacity(MAX_CQ_CNT);
        bitmap.resize(MAX_CQ_CNT, false);
        Self { bitmap }
    }

    /// Allocates a new cq and returns its cqN
    #[allow(clippy::cast_possible_truncation)] // no larger than u32
    pub(crate) fn create_cq(&mut self) -> Option<u32> {
        let handle = self.bitmap.first_zero()? as u32;
        self.bitmap.set(handle as usize, true);
        Some(handle)
    }

    /// Removes and returns the cq associated with the given cqN
    pub(crate) fn destroy_cq(&mut self, handle: u32) -> bool {
        let ret = self.bitmap.get(handle as usize).is_some_and(|x| *x);
        self.bitmap.set(handle as usize, false);

        ret
    }
}
