use super::{desc_ring::DmaBuffer, RingPtr};
use crate::ring::csr::ring_csr::ReaderOps;
use crate::ring::{
    csr::RingCsr,
    traits::{DeviceAdaptor, FromRingBytes, RingSpecToHost},
};
use std::fmt::Debug;
use std::sync::atomic::fence;
use std::sync::atomic::Ordering;

// ============================================================================
// Consumer Ring (Card → Host)
// ============================================================================

/// Consumer-side ring buffer that reads descriptors produced by hardware.
///
/// # Type Parameters
/// Same as ProducerRing but for `RingSpecToHost` direction
///
/// # Synchronization Model
/// - **Hardware manages**: head (producer index, read via CSR)
/// - **Software manages**: `cached_released_tail` (locally released descriptor boundary)
/// - **Tail CSR writes**: delayed until the end of `try_pop()`, at most once per call
/// - **Memory ordering**: Acquire fence once a full logical element is assembled
///
/// # Empty vs Full Distinction
/// The hardware head CSR register is **modular** in `[0, BUF_SIZE)`.
/// When `tail_mod == hw_head` the ring could be either empty (0 items) or
/// full (BUF_SIZE items); this is inherently ambiguous. `available()` returns
/// `None` in that case. Callers must use `try_pop()` (flag-bit based) for
/// actual consumption without relying on `available()` for the boundary case.
///
/// WARN: 读取的时候不会看 head ptr，而只是看 tail ptr 指向的 element 的标志位是否到达
pub(crate) struct ConsumerRing<Dev, Spec>
where
    Dev: DeviceAdaptor,
    Spec: RingSpecToHost,
    Spec::Element: FromRingBytes,
{
    /// DMA buffer for descriptors (stores bytes representation)
    buffer: DmaBuffer<<Spec::Element as FromRingBytes>::Bytes>,

    /// CSR ring handle for head/tail synchronization
    csr_ring: RingCsr<Dev, Spec>,

    /// Cached local released tail (software-side descriptor release boundary).
    /// This advances when descriptors are taken into the local scratch buffer,
    /// and is flushed to hardware tail CSR at most once per `try_pop()` call.
    cached_released_tail: RingPtr<Spec>,

    /// Cached hardware head (hardware producer pointer).
    /// MODULAR value in [0, BUF_SIZE). NOT monotonically increasing.
    cached_hw_head: RingPtr<Spec>,

    /// Reusable scratch buffer for assembling the current logical element.
    scratch: Vec<<Spec::Element as FromRingBytes>::Bytes>,

    /// Pending logical element length, if the current element is not complete yet.
    pending_expected_desc_count: Option<usize>,
}

impl<Dev, Spec> ConsumerRing<Dev, Spec>
where
    Dev: DeviceAdaptor,
    Spec: RingSpecToHost,
    Spec::Element: FromRingBytes,
    <Spec::Element as FromRingBytes>::Bytes: Debug,
{
    /// Create a new consumer ring
    ///
    /// # Arguments
    /// * `buffer` - DMA buffer (must have capacity >= BUF_SIZE)
    /// * `csr_ring` - CSR ring handle for hardware synchronization
    ///
    /// # Panics
    /// Panics if buffer capacity less than BUF_SIZE
    pub(crate) fn new(
        buffer: DmaBuffer<<Spec::Element as FromRingBytes>::Bytes>,
        csr_ring: RingCsr<Dev, Spec>,
    ) -> Self {
        assert!(
            buffer.capacity() == RingPtr::<Spec>::buf_size(),
            "buffer capacity mismatch"
        );

        csr_ring.write_base_addr(buffer.phys_addr());

        log::debug!(
            "ConsumerRing: buffer write base addr with Sepc {} , pa=0x{:x}, capacity={}",
            std::any::type_name::<Spec>(),
            buffer.phys_addr(),
            buffer.capacity()
        );

        Self {
            buffer,
            csr_ring,
            cached_released_tail: RingPtr::zero(),
            cached_hw_head: RingPtr::zero(),
            scratch: Vec::with_capacity(Spec::Element::MAX_DESC_COUNT),
            pending_expected_desc_count: None,
        }
    }

    /// Get number of available elements to consume.
    pub(crate) fn available(&mut self) -> usize {
        let hw_head = RingPtr::<Spec>::new(self.csr_ring.read_head());
        self.cached_hw_head = hw_head;
        let available = hw_head.wrapping_sub(self.cached_released_tail);
        available as usize
    }

    fn take_desc_and_advance_tail(&mut self) -> <Spec::Element as FromRingBytes>::Bytes {
        let index = self.cached_released_tail.index();
        let ret = self.buffer.read(index);
        self.buffer.zero(index);
        self.cached_released_tail = self.cached_released_tail.wrapping_add(1);
        ret
    }

    fn write_tail_csr(&mut self) {
        // Write tail pointer to hardware including the guard bit.
        // Hardware uses a {guard, idx} pointer of width BUF_SIZE_EXP+1 bits;
        // stripping the guard bit (using BUF_SIZE_MASK) would send the wrong
        // wrap generation and cause hardware to misdetect full/empty.
        self.csr_ring.write_tail(self.cached_released_tail.raw());
    }

    fn read_head_csr(&mut self) -> u32 {
        let hw_head = RingPtr::<Spec>::new(self.csr_ring.read_head());
        self.cached_hw_head = hw_head;
        hw_head.raw()
    }

    fn try_read_desc(&mut self) -> Option<<Spec::Element as FromRingBytes>::Bytes> {
        // TODO: This reads the full descriptor once for valid checking and then
        // reads it again in `take_desc_and_advance_tail()`. For DMA correctness,
        // prefer a small volatile valid/meta probe, then Acquire, then one full
        // descriptor read.
        let desc = self.buffer.read(self.cached_released_tail.index());
        if Spec::Element::is_valid(&desc) {
            fence(Ordering::Acquire);
            Some(self.take_desc_and_advance_tail())
        } else {
            None
        }
    }

    /// Pop single element with validation
    ///
    fn try_pop_without_sync(&mut self) -> Option<Spec::Element> {
        while let Some(desc) = self.try_read_desc() {
            if let Some(pending_count) = self.pending_expected_desc_count {
                assert!(
                    pending_count > self.scratch.len(),
                    "pending element should not be complete yet when pushing desc"
                );
                self.scratch.push(desc);
                if self.scratch.len() == pending_count {
                    self.pending_expected_desc_count = None;
                    let elem = Spec::Element::from_bytes(&self.scratch);
                    self.scratch.clear();
                    return Some(elem);
                }
            } else {
                assert!(
                    self.pending_expected_desc_count.is_none() && self.scratch.is_empty(),
                    "scratch buffer must be empty when starting a new element"
                );
                let desc_count = Spec::Element::desc_count(&desc);
                if desc_count == 1 {
                    return Some(Spec::Element::from_bytes(std::slice::from_ref(&desc)));
                } else {
                    self.scratch.push(desc);
                    self.pending_expected_desc_count = Some(desc_count);
                }
            }
        }
        None
    }

    pub(crate) fn try_pop(&mut self) -> Option<Spec::Element> {
        // TODO: `try_pop_without_sync()` may consume and release descriptors but
        // still return `None` for an incomplete multi-desc element. Tail CSR
        // should be synced when `cached_released_tail` advances, not only when
        // a complete element is returned.
        let elem = self.try_pop_without_sync();
        if elem.is_some() {
            self.write_tail_csr();
        }
        elem
    }

    pub(crate) fn tail(&self) -> u32 {
        self.cached_released_tail.raw()
    }

    pub(crate) fn cached_head(&self) -> u32 {
        self.cached_hw_head.raw()
    }
}
