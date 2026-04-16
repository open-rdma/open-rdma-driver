//! Memory management module.
//!
//! This module provides abstractions for memory allocation, address translation,
//! and user memory handling in RDMA operations.
//!
//! # Module Structure
//!
//! - `address`: Address translation (virtual to physical) and PA-VA mapping
//! - `allocator`: DMA buffer allocators (udmabuf, emulated)
//! - `handler`: User memory handlers (host, emulated)
//! - `mmap`: Memory-mapped region abstraction
//! - `experimental`: Unused code preserved for reference

/// Address translation and mapping utilities
pub(crate) mod address;

/// Memory allocators for DMA buffers
pub(crate) mod allocator;

/// User memory handler implementations
pub(crate) mod handler;

/// Memory-mapped region abstraction
pub(crate) mod mmap;

/// Experimental and unused code
pub(crate) mod experimental;

mod utils;

pub(crate) use utils::get_num_page;

use std::{
    io,
    ops::{Deref, DerefMut},
};

use crate::types::{PhysAddr, VirtAddr};
use mmap::MmapMut;

// Re-export commonly used types
pub(crate) use address::{AddressResolver, PaVaMap, PhysAddrResolverLinuxX86};
pub(crate) use allocator::{EmulatedPageAllocator, UDmaBufAllocator};
pub(crate) use handler::{EmulatedUmemHandler, HostUmemHandler, MemoryPinner, UmemHandler};

/// Number of bits for a 4KB page size
#[cfg(all(target_arch = "x86_64", feature = "page_size_4k"))]
pub(crate) const PAGE_SIZE_BITS: u8 = 12;

/// Number of bits for a 2MB huge page size
#[cfg(feature = "page_size_2m")]
pub(crate) const PAGE_SIZE_BITS: u8 = 21;

/// Size of a page in bytes
pub(crate) const PAGE_SIZE: usize = 1 << PAGE_SIZE_BITS;

/// Asserts system page size matches the expected page size.
///
/// # Panics
///
/// Panics if the system page size does not equal `PAGE_SIZE`.
pub(crate) fn assert_equal_page_size() {
    assert_eq!(page_size(), PAGE_SIZE, "page size not match");
}

/// Returns the current system page size.
#[allow(
    unsafe_code, // Safe because sysconf(_SC_PAGESIZE) is guaranteed to return a valid value.
    clippy::as_conversions,
    clippy::cast_sign_loss,
    clippy::cast_possible_truncation
)]
pub(crate) fn page_size() -> usize {
    unsafe { libc::sysconf(libc::_SC_PAGESIZE) as usize }
}

/// A DMA buffer with its physical address.
pub(crate) struct DmaBuf {
    /// The underlying memory-mapped buffer
    pub(crate) buf: MmapMut,
    /// The physical address of the buffer
    pub(crate) phys_addr: PhysAddr,
}

impl DmaBuf {
    /// Creates a new DMA buffer.
    pub(crate) fn new(buf: MmapMut, phys_addr: PhysAddr) -> Self {
        Self { buf, phys_addr }
    }

    /// Returns the physical address of the buffer.
    pub(crate) fn phys_addr(&self) -> PhysAddr {
        self.phys_addr
    }

    /// Returns the virtual address of the buffer.
    pub(crate) fn virt_addr(&self) -> VirtAddr {
        VirtAddr::new(self.buf.as_ptr() as u64)
    }
}

impl Deref for DmaBuf {
    type Target = MmapMut;

    fn deref(&self) -> &Self::Target {
        &self.buf
    }
}

impl DerefMut for DmaBuf {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.buf
    }
}

/// Trait for allocating DMA buffers.
pub(crate) trait DmaBufAllocator {
    /// Allocates a DMA buffer of the specified length.
    ///
    /// # Errors
    ///
    /// Returns an error if allocation fails.
    fn alloc(&mut self, len: usize) -> io::Result<DmaBuf>;
}
