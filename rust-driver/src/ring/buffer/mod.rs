use std::io;

use super::descriptors::DESC_SIZE;
use crate::mem::{DmaBuf, DmaBufAllocator};

mod consumer;
pub(crate) mod desc_ring;
mod producer;

pub(crate) use consumer::ConsumerRing;
pub(crate) use producer::ProducerRing;

pub(crate) type ConsumerRingDefault<Dev, Spec> = ConsumerRing<Dev, Spec, RING_BUF_LEN_BITS>;
pub(crate) type ProducerRingDefault<Dev, Spec> = ProducerRing<Dev, Spec, RING_BUF_LEN_BITS>;

/// Number of bits used to represent the length of the ring buffer.
const RING_BUF_LEN_BITS: u8 = 12;
/// Highest bit of the ring buffer
pub(crate) const RING_BUF_LEN: usize = 1 << RING_BUF_LEN_BITS;

pub(crate) struct DefaultDescRingBufAllocator<'a, A> {
    dma_buf_allocator: &'a mut A,
}

impl<'a, A: DmaBufAllocator> DefaultDescRingBufAllocator<'a, A> {
    pub(crate) fn new(dma_buf_allocator: &'a mut A) -> Self {
        Self { dma_buf_allocator }
    }

    // TODO 可能返回长于这个数的 dma 缓冲区
    pub(crate) fn alloc(&mut self) -> io::Result<DmaBuf> {
        self.dma_buf_allocator.alloc(RING_BUF_LEN * DESC_SIZE)
    }
}
