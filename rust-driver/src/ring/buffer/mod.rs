use std::fmt::{Display, Formatter};
use std::io;
use std::marker::PhantomData;

use super::csr::constants::RING_BUF_LEN;
use super::descriptors::DESC_SIZE;
use crate::mem::{DmaBuf, DmaBufAllocator};
use crate::ring::traits::RingSpec;

mod consumer;
pub(crate) mod desc_ring;
mod producer;

pub(crate) use consumer::ConsumerRing;
pub(crate) use producer::ProducerRing;

pub(crate) type ConsumerRingDefault<Dev, Spec> = ConsumerRing<Dev, Spec>;
pub(crate) type ProducerRingDefault<Dev, Spec> = ProducerRing<Dev, Spec>;

/// Hardware ring pointer encoded as `{guard, idx}` for a specific ring spec.
#[repr(transparent)]
pub(crate) struct RingPtr<Spec: RingSpec> {
    raw: u32,
    _marker: PhantomData<Spec>,
}

impl<Spec: RingSpec> Copy for RingPtr<Spec> {}

impl<Spec: RingSpec> Clone for RingPtr<Spec> {
    fn clone(&self) -> Self {
        *self
    }
}

impl<Spec: RingSpec> Display for RingPtr<Spec> {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "RingPtr {{ raw: {}, index: {} }}",
            self.raw(),
            self.index()
        )
    }
}

impl<Spec: RingSpec> RingPtr<Spec> {
    #[inline(always)]
    pub(crate) fn zero() -> Self {
        Self::new(0)
    }

    #[inline(always)]
    pub(crate) fn new(raw: u32) -> Self {
        Self {
            raw: raw & Self::hw_ptr_mask(),
            _marker: PhantomData,
        }
    }

    #[inline(always)]
    pub(crate) fn raw(self) -> u32 {
        self.raw
    }

    #[inline(always)]
    pub(crate) fn index(self) -> u32 {
        self.raw & Self::buf_size_mask()
    }

    #[inline(always)]
    pub(crate) fn wrapping_add(self, rhs: u32) -> Self {
        Self::new(self.raw.wrapping_add(rhs))
    }

    #[inline(always)]
    pub(crate) fn wrapping_sub(self, rhs: Self) -> u32 {
        self.raw.wrapping_sub(rhs.raw) & Self::hw_ptr_mask()
    }

    #[inline(always)]
    pub(crate) fn add_index(self, rhs: u32) -> u32 {
        self.wrapping_add(rhs).index()
    }

    #[inline(always)]
    pub(crate) fn has_same_index(self, rhs: Self) -> bool {
        self.index() == rhs.index()
    }

    #[inline(always)]
    pub(crate) fn has_same_raw(self, rhs: Self) -> bool {
        self.raw == rhs.raw
    }

    #[inline(always)]
    pub(crate) fn buf_size() -> u32 {
        Spec::element_num()
    }

    #[inline(always)]
    pub(crate) fn buf_size_mask() -> u32 {
        Self::buf_size() - 1
    }

    #[inline(always)]
    pub(crate) fn hw_ptr_mask() -> u32 {
        Self::buf_size() * 2 - 1
    }
}

pub(crate) struct DefaultDescRingBufAllocator<'a, A> {
    dma_buf_allocator: &'a mut A,
}

impl<'a, A: DmaBufAllocator> DefaultDescRingBufAllocator<'a, A> {
    pub(crate) fn new(dma_buf_allocator: &'a mut A) -> Self {
        Self { dma_buf_allocator }
    }

    // TODO 可能返回长于这个数的 dma 缓冲区
    pub(crate) fn alloc_for_spec<Spec: RingSpec>(&mut self) -> io::Result<DmaBuf> {
        self.dma_buf_allocator
            .alloc(Spec::element_num_usize() * DESC_SIZE)
    }

    pub(crate) fn alloc(&mut self) -> io::Result<DmaBuf> {
        self.dma_buf_allocator.alloc(RING_BUF_LEN * DESC_SIZE)
    }
}
