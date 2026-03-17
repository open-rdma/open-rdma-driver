//! Memory-mapped region abstraction.

use std::{ffi::c_void, ptr};

/// Memory-mapped region of host memory.
#[derive(Debug)]
pub(crate) struct MmapMut {
    /// Raw pointer to the start of the mapped memory region
    pub(crate) ptr: *mut c_void,
    /// Length of the mapped memory region in bytes
    pub(crate) len: usize,
}

impl MmapMut {
    /// Creates a new `MmapMut`
    pub(crate) fn new(ptr: *mut c_void, len: usize) -> Self {
        Self { ptr, len }
    }

    /// Returns the length of the mapped region
    pub(crate) fn len(&self) -> usize {
        self.len
    }

    // TODO: optimize read/write performance
    /// Copies data from a slice to the mapped memory at the given offset
    #[allow(clippy::needless_pass_by_ref_mut)]
    pub(crate) fn copy_from(&mut self, offset: usize, src: &[u8]) {
        assert!(
            offset.saturating_add(src.len()) <= self.len,
            "copy beyond mmap boundaries"
        );
        let ptr = self.ptr.cast::<u8>();
        for (i, x) in src.iter().enumerate() {
            unsafe {
                let ptr = ptr.add(offset + i);
                ptr::write_volatile(ptr, *x);
            }
        }
    }

    /// Reads data from the mapped memory at the given offset
    pub(crate) fn get(&self, offset: usize, len: usize) -> Vec<u8> {
        assert!(
            offset.saturating_add(len) <= self.len,
            "get beyond mmap boundaries"
        );
        let mut buf = Vec::with_capacity(len);
        let ptr = self.ptr.cast::<u8>();
        for i in offset..(offset + len) {
            unsafe {
                let ptr = ptr.add(i);
                buf.push(ptr::read_volatile(ptr));
            }
        }
        buf
    }

    /// Returns a const pointer to the mapped memory
    pub(crate) fn as_ptr(&self) -> *const c_void {
        self.ptr
    }
}

#[allow(unsafe_code)]
#[allow(clippy::as_conversions, clippy::ptr_as_ptr)] // converting among different pointer types
/// Safety implementations for `MmapMut`
mod mmap_mut_impl {
    use super::MmapMut;

    impl Drop for MmapMut {
        fn drop(&mut self) {
            let _ignore = unsafe { libc::munmap(self.ptr, self.len) };
        }
    }

    unsafe impl Sync for MmapMut {}
    #[allow(unsafe_code)]
    unsafe impl Send for MmapMut {}
}
