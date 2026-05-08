use super::{desc_ring::DmaBuffer, RingPtr};
use crate::ring::csr::ring_csr::ReaderOps;
use crate::ring::{
    csr::RingCsr,
    traits::{DeviceAdaptor, FromRingBytes, RingSpecToHost},
};
use std::fmt::Debug;
use std::sync::atomic::Ordering;
use std::sync::atomic::fence;

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
/// - **Software manages**: `cached_tail` (consumer index)
/// - **Memory ordering**: Acquire fence after reading descriptors
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

    /// Cached local tail (software consumer pointer)
    cached_tail: RingPtr<Spec>,

    /// Cached hardware head (hardware producer pointer).
    /// MODULAR value in [0, BUF_SIZE). NOT monotonically increasing.
    cached_hw_head: RingPtr<Spec>,
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
            cached_tail: RingPtr::zero(),
            cached_hw_head: RingPtr::zero(),
        }
    }

    /// Get number of available elements to consume.
    pub(crate) fn available(&mut self) -> usize {
        let hw_head = RingPtr::<Spec>::new(self.csr_ring.read_head());
        self.cached_hw_head = hw_head;
        let available = hw_head.wrapping_sub(self.cached_tail);
        available as usize
    }

    fn read_and_advance(&mut self) -> <Spec::Element as FromRingBytes>::Bytes {
        let index = self.cached_tail.index();
        let ret = self.buffer.read(index);
        self.buffer.zero(index);
        self.cached_tail = self.cached_tail.wrapping_add(1);
        ret
    }

    fn write_tail_csr(&mut self) {
        // Write tail pointer to hardware including the guard bit.
        // Hardware uses a {guard, idx} pointer of width BUF_SIZE_EXP+1 bits;
        // stripping the guard bit (using BUF_SIZE_MASK) would send the wrong
        // wrap generation and cause hardware to misdetect full/empty.
        self.csr_ring.write_tail(self.cached_tail.raw());
    }

    fn read_head_csr(&mut self) -> u32 {
        let hw_head = RingPtr::<Spec>::new(self.csr_ring.read_head());
        self.cached_hw_head = hw_head;
        hw_head.raw()
    }

    /// Pop single element with validation
    ///
    pub(crate) fn try_pop(&mut self) -> Option<Spec::Element> {
        // std::thread::sleep(std::time::Duration::from_millis(1));

        // use std::sync::atomic::{AtomicU64, Ordering};
        // use std::time::{SystemTime, UNIX_EPOCH};
        // let avai = self.available()?;
        // if avai > 4000 {
        //     log::warn!(
        //         "try_pop near overflow: available={}, hw_head is {}, tail is {}",
        //         avai,
        //         self.cached_hw_head,
        //         self.cached_tail
        //     );
        // }
        // {
        //     static LAST_LOG_SECS: AtomicU64 = AtomicU64::new(0);
        //     let now_secs = SystemTime::now()
        //         .duration_since(UNIX_EPOCH)
        //         .unwrap_or_default()
        //         .as_secs();
        //     let last = LAST_LOG_SECS.load(Ordering::Relaxed);
        //     if now_secs > last
        //         && LAST_LOG_SECS
        //             .compare_exchange(last, now_secs, Ordering::Relaxed, Ordering::Relaxed)
        //             .is_ok()
        //     {
        //         log::info!(
        //             "[available] try_pop: available={}, hw_head is {}",
        //             avai,
        //             self.cached_hw_head
        //         );
        //     }
        // }

        let idx_first = self.cached_tail.index();

        let first_element = self.buffer.read(idx_first);

        if Spec::Element::is_valid(&first_element) {
            if Spec::Element::has_next(&first_element) {
                let idx_next = self.cached_tail.add_index(1);
                let second_element = self.buffer.read(idx_next);
                if Spec::Element::is_valid(&second_element) {
                    fence(Ordering::Acquire);
                    let a = self.read_and_advance();
                    let b = self.read_and_advance();
                    self.write_tail_csr();
                    Spec::Element::from_bytes(&[a, b])
                } else {
                    None
                }
            } else {
                fence(Ordering::Acquire);
                let a = self.read_and_advance();
                self.write_tail_csr();
                Spec::Element::from_bytes(&[a])
            }
        } else {
            None
        }
        // todo!()
        // if(<Spec as RingSpecToHost>::Element)
        // if()

        // let idx_next = idx_first.wrapping_add(1) & RING_BUF_LEN_MASK;
        // let value_first = self.read_index(idx_first);
        // let value_next = self.read_index(idx_next);

        // match (
        //     cond(&value_first),
        //     cond(&value_next),
        //     require_next(&value_first),
        // ) {
        //     (true, true, true) => {
        //         fence(Ordering::Acquire);
        //         let value_first = self.read_and_advance(idx_first);
        //         let value_next = self.read_and_advance(idx_next);
        //         (Some(value_first), Some(value_next))
        //     }
        //     (true, _, false) => {
        //         fence(Ordering::Acquire);
        //         let value_first = self.read_and_advance(idx_first);
        //         (Some(value_first), None)
        //     }
        //     (true, false, true) | (false, _, _) => (None, None),
        // }
    }

    /// Batch pop operation
    ///
    /// Pops up to `max_count` valid elements in a single operation.
    /// Stops at the first invalid descriptor.
    // pub(crate) fn pop_batch(&mut self, max_count: usize) -> io::Result<Vec<Spec::Bytes>> {
    //     let available = self.available()?;
    //     let count = available.min(max_count);
    //     let mut results = Vec::with_capacity(count);

    //     for _ in 0..count {
    //         let index = (self.cached_tail as usize) & Self::BUF_SIZE_MASK;
    //         let bytes = self.buffer.read(index);

    //         // Check if descriptor is valid
    //         if !Spec::Element::is_valid(&bytes) {
    //             break;
    //         }

    //         // Deserialize (always succeeds)
    //         let value = Spec::Element::from_bytes(bytes);
    //         results.push(value);
    //         self.buffer.zero(index);
    //         self.cached_tail = self.cached_tail.wrapping_add(1);
    //     }

    //     if !results.is_empty() {
    //         fence(Ordering::Acquire);
    //         self.csr_ring.write_tail(self.cached_tail)?;
    //     }

    //     Ok(results)
    // }

    pub(crate) fn tail(&self) -> u32 {
        self.cached_tail.raw()
    }

    pub(crate) fn cached_head(&self) -> u32 {
        self.cached_hw_head.raw()
    }
}
