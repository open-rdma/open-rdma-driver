//! Hardware-Synced Ring Buffer Implementation
//!
//! This module provides high-performance ring buffer abstractions that integrate with
//! hardware CSR registers for producer-consumer synchronization in RDMA operations.
//!
//! # Architecture
//!
//! ```text
//! ┌─────────────────────────────────────────────────┐
//! │ ProducerRing (Host → Card)                      │
//! │ - Software maintains: cached_head               │
//! │ - Hardware provides: tail (via CSR)             │
//! │ - Lazy sync: read CSR tail on available()       │
//! │ - Batch support: ReservedSlots RAII guard       │
//! └─────────────────────────────────────────────────┘
//!          ↓ writes to
//! ┌─────────────────────────────────────────────────┐
//! │ DmaBuffer<T> (Volatile DMA Memory)              │
//! │ - Volatile read/write semantics                 │
//! │ - Ring wraparound logic                         │
//! └─────────────────────────────────────────────────┘
//!          ↑ synchronized via
//! ┌─────────────────────────────────────────────────┐
//! │ Ring<Dev, Spec> (CSR Abstraction)               │
//! │ - read_tail() / write_head()                    │
//! │ - Hardware MMIO or UDP emulation               │
//! └─────────────────────────────────────────────────┘
//! ```

use crate::ring::traits::RingSpec;
use crate::{mem::DmaBuf, types::PhysAddr};
use std::ptr::{self, NonNull};

// ============================================================================
// Volatile Memory Wrapper
// ============================================================================

/// Wrapper ensuring volatile memory access semantics for DMA-shared memory.
///
/// This is critical for hardware-visible memory regions where the compiler
/// must not optimize away or reorder memory accesses.
#[repr(transparent)]
pub(crate) struct Volatile<T>(T);

#[allow(unsafe_code)]
impl<T: Copy> Volatile<T> {
    /// Read value with volatile semantics (prevents compiler optimization)
    #[inline]
    pub(crate) fn read(&self) -> T {
        unsafe { ptr::read_volatile(&self.0) }
    }

    /// Write value with volatile semantics (forces memory write)
    #[inline]
    pub(crate) fn write(&mut self, value: T) {
        unsafe { ptr::write_volatile(&mut self.0, value) }
    }
}

// ============================================================================
// DMA Buffer Abstraction
// ============================================================================

/// DMA-accessible circular buffer with volatile memory access.
///
/// Provides safe indexed access to a memory-mapped DMA region with automatic
/// wraparound and volatile semantics. Contains both virtual address (for CPU access)
/// and physical address (for hardware DMA).
pub(crate) struct DmaBuffer<T> {
    buf: NonNull<Volatile<T>>,
    dma_buf: DmaBuf,
    capacity: u32,
}

#[allow(unsafe_code)]
impl<T: Copy> DmaBuffer<T> {
    /// Create a new DMA buffer from DmaBuf.
    ///
    /// # Arguments
    /// * `dma_buf` - DMA buffer with physical address mapping
    /// * `capacity` - Number of elements in the ring buffer
    ///
    /// # Panics
    /// Panics if buffer is too small or pointer is null
    pub(crate) fn new(dma_buf: DmaBuf, capacity: u32) -> Self {
        log::debug!(
            "DmaBuffer: pa=0x{:x}, va={:?}, len={}",
            dma_buf.phys_addr().as_u64(),
            dma_buf.as_ptr(),
            dma_buf.len()
        );
        assert!(
            dma_buf.len() % size_of::<T>() == 0,
            "DMA buffer size must be multiple of element size"
        );

        assert!(
            dma_buf.len() >= capacity as usize * size_of::<T>(),
            "DMA buffer too small for ring capacity"
        );

        // #[allow(clippy::as_conversions)]
        let ptr = dma_buf.as_ptr() as *mut Volatile<T>;
        let buf = NonNull::new(ptr).expect("DMA buffer pointer is null");

        Self {
            buf,
            dma_buf,
            capacity,
        }
    }

    #[inline]
    pub(crate) fn new_for_spec<Spec: RingSpec>(dma_buf: DmaBuf) -> Self {
        Self::new(dma_buf, Spec::element_num())
    }

    /// Get the physical address of the DMA buffer (for hardware access)
    #[inline]
    pub(crate) fn phys_addr(&self) -> PhysAddr {
        self.dma_buf.phys_addr()
    }

    /// Read element at index with volatile semantics
    ///
    /// # Panics
    /// Panics if index >= capacity
    #[inline]
    pub(crate) fn read(&self, index: u32) -> T {
        assert!(index < self.capacity, "index out of bounds");
        unsafe {
            self.buf
                .as_ptr()
                .add(index as usize)
                .as_ref()
                .unwrap()
                .read()
        }
    }

    /// Write element at index with volatile semantics
    ///
    /// # Panics
    /// Panics if index >= capacity
    #[inline]
    pub(crate) fn write(&mut self, index: u32, value: T) {
        assert!(index < self.capacity, "index out of bounds");
        unsafe {
            self.buf
                .as_ptr()
                .add(index as usize)
                .as_mut()
                .unwrap()
                .write(value);
        }
    }

    /// Zero out element at index (for cleanup after consumption)
    #[inline]
    pub(crate) fn zero(&mut self, index: u32) {
        assert!(index < self.capacity, "index out of bounds");
        unsafe {
            self.buf
                .as_ptr()
                .add(index as usize)
                .as_mut()
                .unwrap()
                .write(std::mem::zeroed());
        }
    }

    /// Get buffer capacity
    #[inline]
    pub(crate) const fn capacity(&self) -> u32 {
        self.capacity
    }
}

#[allow(unsafe_code)]
unsafe impl<T> Send for DmaBuffer<T> {}

// ============================================================================
// Tests
// ============================================================================

// #[cfg(test)]
// mod tests {
//     use super::*;
//     use crate::{
//         csr::{emulated::EmulatedDevice, ring_specs::SendRingSpec},
//         mem::page::MmapMut,
//         types::PhysAddr,
//     };

//     // Test-only implementation for u32
//     impl ToRingBytes for u32 {
//         type Bytes = u32;

//         fn to_bytes(&self) -> u32 {
//             *self
//         }
//     }

//     impl FromRingBytes for u32 {
//         type Bytes = u32;

//         fn from_bytes(bytes: u32) -> Self {
//             bytes
//         }

//         fn is_valid(_bytes: &u32) -> bool {
//             true
//         }
//     }

//     #[test]
//     fn test_volatile_read_write() {
//         let mut val = Volatile(42u32);
//         assert_eq!(val.read(), 42);

//         val.write(100);
//         assert_eq!(val.read(), 100);
//     }

//     #[test]
//     fn test_dma_buffer_basic() {
//         #[allow(unsafe_code)]
//         let mmap = unsafe {
//             let ptr = libc::mmap(
//                 ptr::null_mut(),
//                 4096,
//                 libc::PROT_READ | libc::PROT_WRITE,
//                 libc::MAP_SHARED | libc::MAP_ANON,
//                 -1,
//                 0,
//             );
//             MmapMut::new(ptr, 4096)
//         };

//         // Create DmaBuf with a fake physical address for testing
//         let dma_buf = DmaBuf::new(mmap, PhysAddr::new(0x1000));
//         let mut buffer = DmaBuffer::<u32>::new(dma_buf, 1024);

//         buffer.write(0, 0xDEADBEEF);
//         assert_eq!(buffer.read(0), 0xDEADBEEF);

//         buffer.write(100, 0xCAFEBABE);
//         assert_eq!(buffer.read(100), 0xCAFEBABE);

//         buffer.zero(100);
//         assert_eq!(buffer.read(100), 0);
//     }

//     #[test]
//     fn test_producer_ring_single_push() {
//         let dev = EmulatedDevice::new("test").unwrap();
//         let csr_ring = RingCsr::new(dev, SendRingSpec(0));

//         #[allow(unsafe_code)]
//         let mmap = unsafe {
//             let ptr = libc::mmap(
//                 ptr::null_mut(),
//                 4096 * 32, // 4096 entries * 32 bytes
//                 libc::PROT_READ | libc::PROT_WRITE,
//                 libc::MAP_SHARED | libc::MAP_ANON,
//                 -1,
//                 0,
//             );
//             MmapMut::new(ptr, 4096 * 32)
//         };

//         let dma_buf = DmaBuf::new(mmap, PhysAddr::new(0x10000));
//         let buffer = DmaBuffer::<[u8; 32]>::new(dma_buf, 4096);

//         // Note: This will fail because EmulatedDevice isn't actually connected to a simulator.
//         // This test is just to verify compilation and basic structure.
//         let result = ProducerRing::<_, _, [u8; 32], 12>::new(buffer, csr_ring);

//         // Expect connection refused error when simulator is not running
//         assert!(result.is_err());
//     }

//     #[test]
//     fn test_producer_ring_batch_write() {
//         let dev = EmulatedDevice::new("test").unwrap();
//         let csr_ring = RingCsr::new(dev, SendRingSpec(0));

//         #[allow(unsafe_code)]
//         let mmap = unsafe {
//             let ptr = libc::mmap(
//                 ptr::null_mut(),
//                 4096 * 4, // 1024 entries * 4 bytes
//                 libc::PROT_READ | libc::PROT_WRITE,
//                 libc::MAP_SHARED | libc::MAP_ANON,
//                 -1,
//                 0,
//             );
//             MmapMut::new(ptr, 4096 * 4)
//         };

//         let dma_buf = DmaBuf::new(mmap, PhysAddr::new(0x20000));
//         let buffer = DmaBuffer::<u32>::new(dma_buf, 1024);

//         // This will fail because EmulatedDevice isn't connected to a simulator
//         let result = ProducerRing::<_, _, u32, 10>::new(buffer, csr_ring);

//         // Expect connection refused error when simulator is not running
//         assert!(result.is_err());
//     }
// }
