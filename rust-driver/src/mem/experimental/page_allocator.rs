//! Page allocator trait and contiguous pages wrapper.
//!
//! This module is currently unused but provides abstractions for allocating
//! contiguous physical memory pages. It may be useful for future DMA operations
//! that require physically contiguous memory.

use std::{
    io,
    ops::{Deref, DerefMut},
};

use crate::mem::mmap::MmapMut;

/// A trait for allocating contiguous physical memory pages.
///
/// The generic parameter `N` specifies the number of contiguous pages to allocate.
pub(crate) trait PageAllocator<const N: usize> {
    /// Allocates N contiguous physical memory pages.
    ///
    /// # Returns
    ///
    /// Returns a `Result` containing either:
    /// - `Ok(ContiguousPages<N>)` - The allocated contiguous pages
    /// - `Err(e)` - An I/O error if allocation fails
    fn alloc(&mut self) -> io::Result<ContiguousPages<N>>;
}

/// A wrapper around mapped memory that ensures physical memory pages are consecutive.
pub(crate) struct ContiguousPages<const N: usize> {
    /// Mmap handle
    pub(crate) inner: MmapMut,
}

impl<const N: usize> ContiguousPages<N> {
    /// Returns the start address
    #[allow(clippy::as_conversions)] // converting *mut c_void to u64
    pub(crate) fn addr(&self) -> u64 {
        self.inner.ptr as u64
    }

    /// Creates a new `ContiguousPages`
    pub(crate) fn new(inner: MmapMut) -> Self {
        Self { inner }
    }
}

impl<const N: usize> Deref for ContiguousPages<N> {
    type Target = MmapMut;

    fn deref(&self) -> &Self::Target {
        &self.inner
    }
}

impl<const N: usize> DerefMut for ContiguousPages<N> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.inner
    }
}
