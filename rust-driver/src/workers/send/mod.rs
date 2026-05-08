use std::{io, iter, sync::Arc};

use crate::{
    mem::DmaBuf,
    ring::{
        buffer::{desc_ring::DmaBuffer, ProducerRingDefault},
        csr::mode::Mode,
        spec::build_send_rings,
        traits::DeviceAdaptor,
    },
    workers::spawner::{AbortSignal, SingleThreadPollingWorker},
};
use types::{WrInjector, WrWorker};
use worker::SendWorker;

mod types;
mod worker;

pub(crate) use types::*;
pub(crate) use worker::SendHandle;

pub(crate) fn spawn<Dev>(
    dev: &Dev,
    bufs: Vec<DmaBuf>,
    mode: Mode,
    abort: &AbortSignal,
) -> io::Result<SendHandle>
where
    Dev: DeviceAdaptor + Clone + Send + 'static,
{
    let injector = Arc::new(WrInjector::new());
    let handle = SendHandle::new(Arc::clone(&injector));
    let sq_rings = build_send_rings(dev.clone(), mode);

    let producer_rings = bufs
        .into_iter()
        .map(DmaBuffer::new_for_spec::<crate::ring::spec::SendRingSpec>)
        .zip(sq_rings)
        .map(|(q, ring)| ProducerRingDefault::new(q, ring));

    // let send_queues: Vec<_> = bufs
    //     .into_iter()
    //     .map(|p| SendQueue::new(DescRingBuffer::new(p.buf)))
    //     .collect();
    let workers: Vec<_> = iter::repeat_with(WrWorker::new_fifo)
        .take(producer_rings.len())
        .collect();
    let stealers: Vec<_> = workers.iter().map(WrWorker::stealer).collect();
    // let sqs = send_queues
    //     .into_iter()
    //     .zip(sq_rings)
    //     .map(|(sq, ring)| SendQueueSync::new(sq, ring));
    for (id, (local, sq)) in workers.into_iter().zip(producer_rings).enumerate() {
        let worker = SendWorker::new(
            id,
            local,
            Arc::clone(&injector),
            stealers
                .clone()
                .into_iter()
                .enumerate()
                .filter_map(|(i, x)| (i != id).then_some(x))
                .collect(),
            sq,
        );
        let name = format!("SendWorker{id}");
        worker.spawn(&name, abort.clone());
    }

    Ok(handle)
}
