//! Emulated page allocator for simulation mode.

use std::io;

use crate::{
    mem::{address::PaVaMap, mmap::MmapMut, DmaBuf, DmaBufAllocator, PAGE_SIZE},
    types::{PhysAddr, VirtAddr},
};

const DEFAULT_ALLOCATOR_SIZE: usize = 128 * 1024 * 1024; // 128 MiB

/// A page allocator for allocating pages of emulated physical memory.
///
/// This allocator pre-allocates a large block of memory and divides it into
/// individual pages for allocation.
#[derive(Debug)]
pub(crate) struct EmulatedPageAllocator<const N: usize> {
    /// Stack of available memory pages
    inner: Vec<MmapMut>,
}

impl<const N: usize> EmulatedPageAllocator<N> {
    /// TODO: implements allocating multiple consecutive pages
    const _OK: () = assert!(
        N == 1,
        "allocating multiple contiguous pages is currently unsupported"
    );

    /// Creates a new `EmulatedPageAllocator`
    ///
    /// # Arguments
    /// * `size` - Optional size of the memory pool (defaults to 128 MiB)
    /// * `pa_va_map` - PA-VA mapping table to register the allocated memory
    #[allow(clippy::as_conversions)] // usize to *mut c_void is safe
    pub(crate) fn new(size: Option<usize>, pa_va_map: &mut PaVaMap) -> Self {
        let size = size.unwrap_or(DEFAULT_ALLOCATOR_SIZE);
        let ptr = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                size,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_ANONYMOUS | libc::MAP_PRIVATE,
                -1,
                0,
            )
        };

        if ptr == libc::MAP_FAILED {
            panic!("Failed to allocate memory");
        }

        // WARN: Assumes VA will never overlap with real PA
        pa_va_map.insert(PhysAddr::new(ptr as u64), VirtAddr::new(ptr as u64), size);

        let inner: Vec<_> = (0..size)
            .step_by(PAGE_SIZE)
            .map(|offset| MmapMut::new(unsafe { ptr.offset(offset as isize) }, PAGE_SIZE))
            .collect();

        Self { inner }
    }
}

impl DmaBufAllocator for EmulatedPageAllocator<1> {
    #[allow(clippy::unwrap_in_result, clippy::unwrap_used)]
    fn alloc(&mut self, _len: usize) -> io::Result<DmaBuf> {
        let buf = self
            .inner
            .pop()
            .ok_or(io::Error::from(io::ErrorKind::OutOfMemory))?;
        // WARN: Assumes DMA buffer VA = PA (emulation simplification)
        let phys_addr = PhysAddr::new(buf.as_ptr() as u64);
        Ok(DmaBuf::new(buf, phys_addr))
    }
}

#[test]
fn test_libc_behave() {
    unsafe {
        let ptr = libc::mmap(
            std::ptr::null_mut(),
            PAGE_SIZE,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_PRIVATE | libc::MAP_ANON,
            -1,
            0,
        );

        let result = libc::mlock(ptr, PAGE_SIZE);

        println!("result is {}", result);
        let result = libc::mlock(ptr, PAGE_SIZE);

        println!("result is {}", result);

        let result = libc::munlock(ptr as *const std::ffi::c_void, PAGE_SIZE);

        println!("result is {}", result);
        let result = libc::munlock(ptr as *const std::ffi::c_void, PAGE_SIZE);

        println!("result is {}", result);
        assert_ne!(ptr, libc::MAP_FAILED);
        println!("mmap ptr: {:p}", ptr);
        println!("page size: {}", PAGE_SIZE);
        let _ = libc::munmap(ptr, PAGE_SIZE);
    }
}
