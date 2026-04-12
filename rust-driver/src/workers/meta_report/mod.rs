mod types;
mod worker;

use std::io;

use types::MetaReportQueueHandler;
use worker::{MetaHandler, MetaWorker};

use crate::{
    mem::DmaBuf,
    ring::{
        buffer::{desc_ring::DmaBuffer, ConsumerRingDefault},
        csr::mode::Mode,
        spec::build_meta_report_rings,
        traits::DeviceAdaptor,
    },
    workers::{
        ack_responder::AckResponse,
        completion::CompletionTask,
        qp_timeout::AckTimeoutTask,
        rdma::RdmaWriteTask,
        retransmit::PacketRetransmitTask,
        spawner::{AbortSignal, SingleThreadPollingWorker, TaskTx},
    },
};

pub(crate) use types::*;

#[allow(clippy::too_many_arguments)]
pub(crate) fn spawn<Dev>(
    dev: &Dev,
    pages: Vec<DmaBuf>,
    mode: Mode,
    ack_tx: TaskTx<AckResponse>,
    retransmit_tx: TaskTx<AckTimeoutTask>,
    packet_retransmit_tx: TaskTx<PacketRetransmitTask>,
    completion_tx: TaskTx<CompletionTask>,
    rdma_write_tx: TaskTx<RdmaWriteTask>,
    abort: AbortSignal,
) -> io::Result<()>
where
    Dev: Clone + DeviceAdaptor + Send + 'static,
{
    let mrq_rings = build_meta_report_rings(dev.clone(), mode);
    // for (ring, page) in mrq_rings.iter().zip(pages.iter()) {
    //     ring.write_base_addr(page.phys_addr)?;
    // }

    let ctxs = pages
        .into_iter()
        .map(DmaBuffer::new_for_spec::<crate::ring::spec::MetaReportRingSpec>)
        .zip(mrq_rings)
        .map(|(q, ring)| ConsumerRingDefault::new(q, ring).unwrap())
        .collect();

    let handler = MetaHandler::new(
        ack_tx,
        retransmit_tx,
        packet_retransmit_tx,
        completion_tx,
        rdma_write_tx,
    );
    MetaWorker::new(MetaReportQueueHandler::new(ctxs), handler).spawn("MetaWorker", abort);

    Ok(())
}
