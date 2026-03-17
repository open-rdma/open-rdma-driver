//! Alternative DMA buffer allocator using udmabuf and memfd.
//!
//! This implementation creates DMA buffers using the udmabuf kernel module
//! with memfd backing. It's an alternative approach that may provide different
//! characteristics compared to the u-dma-buf0 device approach.
//!
//! Currently unused but preserved for reference.

use nix::fcntl::{self, SealFlag};
use nix::ioctl_write_ptr;
use nix::sys::memfd::{memfd_create, MemFdCreateFlag};
use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::io::{FromRawFd, IntoRawFd};

use crate::constants::PAGE_SIZE_2MB;

use super::page_allocator::{ContiguousPages, PageAllocator};
use crate::mem::mmap::MmapMut;

const UDMABUF_IOCTL_TYPE: u8 = b'u';
const UDMABUF_CREATE_NR: u8 = 0x42;
const UDMABUF_FLAGS_CLOEXEC: u32 = 0x01;

#[repr(C)]
#[derive(Debug, Clone, Copy)]
struct UdmabufCreate {
    pub memfd: i32,
    pub flags: u32,
    pub offset: u64,
    pub size: u64,
}

ioctl_write_ptr!(
    udmabuf_create_ioctl,
    UDMABUF_IOCTL_TYPE,
    UDMABUF_CREATE_NR,
    UdmabufCreate
);

/// DMA buffer allocator using udmabuf with memfd backing.
pub(crate) struct DmaBufAllocator;

impl DmaBufAllocator {
    fn create() -> io::Result<MmapMut> {
        let udmabuf_dev = OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/udmabuf")?;
        let udmabuf_fd = udmabuf_dev.into_raw_fd();
        let memfd_name = std::ffi::CString::new("dma_memfd")?;
        let memfd = memfd_create(
            &memfd_name,
            MemFdCreateFlag::MFD_ALLOW_SEALING
                | MemFdCreateFlag::MFD_CLOEXEC
                | MemFdCreateFlag::MFD_HUGETLB
                | MemFdCreateFlag::MFD_HUGE_2MB,
        )?
        .into_raw_fd();
        let memfd_file = unsafe { File::from_raw_fd(memfd) };
        memfd_file.set_len(PAGE_SIZE_2MB as u64)?;

        let ret = fcntl::fcntl(
            memfd,
            fcntl::F_ADD_SEALS(SealFlag::F_SEAL_SHRINK | SealFlag::F_SEAL_SEAL),
        )?;

        if ret == -1 {
            return Err(io::Error::last_os_error());
        }

        let create_args = UdmabufCreate {
            memfd,
            flags: UDMABUF_FLAGS_CLOEXEC,
            offset: 0,
            size: PAGE_SIZE_2MB as u64,
        };

        let dmabuf_fd = unsafe { udmabuf_create_ioctl(udmabuf_fd, &raw const create_args)? };

        let ptr = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                PAGE_SIZE_2MB,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED | libc::MAP_ANON | libc::MAP_HUGETLB | libc::MAP_HUGE_2MB,
                dmabuf_fd,
                0,
            )
        };

        let mmap_mut = MmapMut::new(ptr, PAGE_SIZE_2MB);

        Ok(mmap_mut)
    }
}

impl PageAllocator<1> for DmaBufAllocator {
    fn alloc(&mut self) -> io::Result<ContiguousPages<1>> {
        Self::create().map(ContiguousPages::new)
    }
}
