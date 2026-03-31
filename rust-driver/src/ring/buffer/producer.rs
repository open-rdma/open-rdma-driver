use super::desc_ring::DmaBuffer;
use crate::ring::{
    csr::RingCsr,
    traits::{DeviceAdaptor, RingSpecToCard, ToRingBytes},
};
use std::{
    io,
    marker::PhantomData,
    sync::atomic::{fence, AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

use crate::ring::csr::ring_csr::WriterOps;

// ============================================================================
// Producer Ring (Host → Card)
// ============================================================================

/// Producer-side ring buffer that writes descriptors for hardware consumption.
///
/// # Type Parameters
/// - `Dev`: Device adaptor (EmulatedDevice, SysfsPciCsrAdaptor, etc.)
/// - `Spec`: Ring specification implementing `RingSpecToCard`
/// - `T`: Element type (must be Copy for DMA)
/// - `BUF_SIZE_EXP`: Buffer size as power of 2 (e.g., 12 for 4096 entries)
///
/// # Synchronization Model
/// - **Software manages**: `cached_head` (producer index)
/// - **Hardware manages**: tail (consumer index, read via CSR)
/// - **Lazy sync**: Hardware tail is only read when checking space
/// - **Memory ordering**: Release fence before updating CSR head
///
/// # Empty vs Full Distinction
/// The hardware tail CSR register is **modular** in `[0, BUF_SIZE)` and does NOT
/// monotonically increase. An explicit `is_full` flag is used to distinguish:
/// - **Empty**: `head_mod == hw_tail` AND `!is_full` (used = 0)
/// - **Full**: `head_mod == hw_tail` AND `is_full` (used = BUF_SIZE)
/// - Buffer indexing: `index = cached_head & BUF_SIZE_MASK`
/// - `is_full` is set after a push fills the ring; cleared when `hw_tail` changes
///
/// # Example
/// ```rust,ignore
/// let ring: ProducerRing<_, SendRingSpec, [u8; 32], 12> = ...;
///
/// // Single push
/// ring.try_push(descriptor)?;
///
/// // Batch push (more efficient)
/// let mut slots = ring.reserve(10)?.unwrap();
/// for i in 0..10 {
///     slots.write(i, descriptors[i]);
/// }
/// slots.commit()?;  // Single CSR write for all 10 descriptors
/// ```
/// TODO need to change to lazy sync
pub(crate) struct ProducerRing<Dev, Spec, const BUF_SIZE_EXP: u8>
where
    Dev: DeviceAdaptor,
    Spec: RingSpecToCard,
{
    /// DMA buffer for descriptors (stores bytes representation)
    buffer: DmaBuffer<<Spec::Element as ToRingBytes>::Bytes>,

    /// CSR ring handle for head/tail synchronization
    csr_ring: RingCsr<Dev, Spec>,

    /// Cached local head (software producer pointer)
    cached_head: u32,

    /// Cached hardware tail (hardware consumer pointer).
    /// MODULAR value in [0, BUF_SIZE). NOT monotonically increasing.
    /// Updated lazily on space checks.
    cached_hw_tail: u32,

    /// Phantom data to mark the logical element type
    _phantom: PhantomData<<Spec::Element as ToRingBytes>::Bytes>,
}

impl<Dev, Spec, const BUF_SIZE_EXP: u8> ProducerRing<Dev, Spec, BUF_SIZE_EXP>
where
    Dev: DeviceAdaptor,
    Spec: RingSpecToCard,
    Spec::Element: ToRingBytes,
{
    const BUF_SIZE: u32 = 1 << BUF_SIZE_EXP;
    const BUF_SIZE_MASK: u32 = Self::BUF_SIZE - 1;
    /// 13-bit mask covering both guard bit and idx, matches hardware's pointer width.
    const HW_PTR_MASK: u32 = Self::BUF_SIZE * 2 - 1;

    /// Create a new producer ring
    ///
    /// # Arguments
    /// * `buffer` - DMA buffer (must have capacity >= BUF_SIZE)
    /// * `csr_ring` - CSR ring handle for hardware synchronization
    ///
    /// # Errors
    /// Returns an error if CSR write fails
    ///
    /// # Panics
    /// Panics if buffer capacity doesn't match BUF_SIZE
    pub(crate) fn new(
        buffer: DmaBuffer<<Spec::Element as ToRingBytes>::Bytes>,
        csr_ring: RingCsr<Dev, Spec>,
    ) -> io::Result<Self> {
        assert!(
            buffer.capacity() >= Self::BUF_SIZE,
            "buffer capacity mismatch"
        );

        // Write physical address to hardware CSR
        csr_ring.write_base_addr(buffer.phys_addr())?;
        log::debug!(
            "ProducerRing: buffer write base addr with Sepc {} , pa=0x{:x}, capacity={}",
            std::any::type_name::<Spec>(),
            buffer.phys_addr(),
            buffer.capacity()
        );

        Ok(Self {
            buffer,
            csr_ring,
            cached_head: 0,
            cached_hw_tail: 0,
            _phantom: PhantomData,
        })
    }

    /// Get number of available slots (triggers CSR read)
    ///
    /// This operation reads the hardware tail pointer via CSR, which may
    /// have performance implications. Consider using batch operations.
    pub(crate) fn available(&mut self) -> io::Result<u32> {
        // Read hardware tail pointer (modular, in [0, BUF_SIZE))
        let hw_tail = self.cached_hw_tail;

        let hw_tail_mod = hw_tail & Self::BUF_SIZE_MASK;

        let head_mod = self.cached_head & Self::BUF_SIZE_MASK;
        let head_with_guard = self.cached_head & Self::HW_PTR_MASK;

        if hw_tail_mod == head_mod {
            if head_with_guard == hw_tail {
                return Ok(Self::BUF_SIZE);
            } else {
                return Ok(0);
            }
        }
        let used =
            head_mod.wrapping_sub(hw_tail).wrapping_add(Self::BUF_SIZE) & Self::BUF_SIZE_MASK;

        Ok(Self::BUF_SIZE - used)
    }

    /// Batch write using a callback function
    ///
    /// This is the preferred method for writing multiple descriptors efficiently.
    /// It ensures atomic commit of all descriptors with a single CSR write.
    ///
    /// # Arguments
    /// * `count` - Number of descriptors to write
    /// * `writer` - Callback that produces descriptor at given index
    ///
    /// # Returns
    /// - `Ok(count)` if all descriptors written successfully
    /// - `Ok(0)` if insufficient space
    /// - `Err(_)` on CSR error
    ///
    /// # Example
    /// ```rust,ignore
    /// let written = ring.batch_write(10, |i| {
    ///     create_descriptor(i)
    /// })?;
    /// ```
    pub(crate) fn batch_write<F>(&mut self, count: u32, mut writer: F) -> io::Result<u32>
    where
        F: FnMut(u32) -> Spec::Element,
    {
        if count == 0 {
            return Ok(0);
        }

        if count > Self::BUF_SIZE {
            return Ok(0);
        }

        if self.available()? < count {
            self.sync_tail()?;
            if self.available()? < count {
                return Ok(0);
            }
        }

        let start_head = self.cached_head;

        // Write all descriptors to DMA buffer
        for i in 0..count {
            let value = writer(i);
            let bytes = value.to_bytes();
            let index = start_head.wrapping_add(i) & Self::BUF_SIZE_MASK;
            self.buffer.write(index, bytes);
        }

        // Release fence ensures all descriptor writes are visible to hardware
        fence(Ordering::Release);

        // Commit all descriptors with single CSR write
        let new_head = start_head.wrapping_add(count);
        self.csr_ring.write_head(new_head)?;
        self.cached_head = new_head;

        Ok(count)
    }

    // /// Batch write from a slice
    // ///
    // /// Convenience wrapper around `batch_write` for slice inputs.
    // ///
    // /// # Returns
    // /// Number of elements actually written (may be less than slice length if full)
    // pub(crate) fn push_slice(&mut self, values: &[Spec::Element]) -> io::Result<u32> {
    //     self.batch_write(values.len() as u32, |i| values[i as usize])
    // }

    // /// Push single element (convenience method)
    // ///
    // /// For better performance, use `batch_write()` for batch operations.
    // ///
    // /// # Returns
    // /// - `Ok(true)` if pushed successfully
    // /// - `Ok(false)` if ring is full
    // /// - `Err(_)` on CSR error
    // pub(crate) fn try_push(&mut self, value: Spec::Element) -> io::Result<bool> {
    //     if self.available()? == 0 {
    //         return Ok(false);
    //     }

    //     let index = self.cached_head & Self::BUF_SIZE_MASK;
    //     let bytes = value.to_bytes();
    //     self.buffer.write(index, bytes);

    //     // Release fence ensures descriptor write is visible to hardware
    //     fence(Ordering::Release);

    //     let new_head = self.cached_head.wrapping_add(1);
    //     self.csr_ring.write_head(new_head)?;
    //     self.cached_head = new_head;

    //     Ok(true)
    // }

    pub(crate) fn try_push_atomic(&mut self, elements: &[Spec::Element]) -> io::Result<bool> {
        // std::thread::sleep(std::time::Duration::from_nanos(1000));

        let avai = self.available()?;
        self.sync_tail()?;
        if avai < 1000 {
            log::warn!(
                "try_push_atomic near overflow: available={}, hw_head is {}, tail is {}",
                avai,
                self.cached_head,
                self.cached_hw_tail
            );
        }
        {
            static LAST_LOG_SECS: AtomicU64 = AtomicU64::new(0);
            let now_secs = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            let last = LAST_LOG_SECS.load(Ordering::Relaxed);
            if now_secs > last
                && LAST_LOG_SECS
                    .compare_exchange(last, now_secs, Ordering::Relaxed, Ordering::Relaxed)
                    .is_ok()
            {
                log::debug!("[available] try_push_atomic: available={}", avai);
            }
        }
        if (self.available()? as usize) < elements.len() {
            self.sync_tail()?;
            if (self.available()? as usize) < elements.len() {
                return Ok(false);
            }
        }
        let index = self.cached_head & Self::BUF_SIZE_MASK;

        elements.into_iter().enumerate().for_each(|(i, element)| {
            self.buffer
                .write((i as u32 + index) & Self::BUF_SIZE_MASK, element.to_bytes())
        });

        // Release fence ensures descriptor write is visible to hardware
        fence(Ordering::Release);

        let new_head = self.cached_head.wrapping_add(elements.len() as u32);
        self.csr_ring.write_head(new_head)?;
        self.cached_head = new_head;

        Ok(true)
    }

    /// Get current head pointer value
    pub(crate) fn head(&self) -> u32 {
        self.cached_head
    }

    /// Get current cached tail pointer value (may be stale)
    pub(crate) fn cached_tail(&self) -> u32 {
        self.cached_hw_tail
    }

    /// Manually synchronize tail from hardware
    pub(crate) fn sync_tail(&mut self) -> io::Result<()> {
        log::trace!("sync_tail");
        let hw_tail = self.csr_ring.read_tail()?;
        log::info!("sync_tail: hw_tail={}", hw_tail);
        self.cached_hw_tail = hw_tail;
        Ok(())
    }

    /// Force set head pointer (for recovery/initialization)
    ///
    /// # Safety
    /// Caller must ensure this doesn't create inconsistent state
    pub(crate) fn force_set_head(&mut self, head: u32) -> io::Result<()> {
        self.csr_ring.write_head(head)?;
        self.cached_head = head;

        Ok(())
    }
}
