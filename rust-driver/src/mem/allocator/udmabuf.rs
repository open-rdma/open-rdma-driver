//! UDmaBuf allocator using the u-dma-buf kernel module.

use std::{
    fs::{File, OpenOptions},
    io::{self, Read},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
    path::PathBuf,
    ptr,
};

use crate::{constants::U_DMA_BUF_CLASS_PATH, types::PhysAddr};

use crate::mem::mmap::MmapMut;
use crate::mem::DmaBuf;
use crate::mem::DmaBufAllocator;

/// Allocator for DMA buffers using the u-dma-buf kernel module.
pub(crate) struct UDmaBufAllocator {
    fd: File,
    sysfs_path: PathBuf,
    offset: usize,
}

impl UDmaBufAllocator {
    /// Opens the udmabuf device and creates a new allocator.
    pub(crate) fn open() -> io::Result<Self> {
        Self::open_with_index(0)
    }

    /// Opens `/dev/udmabuf{index}` and creates a new allocator for it.
    pub(crate) fn open_with_index(index: usize) -> io::Result<Self> {
        Self::open_with_name(&format!("udmabuf{index}"))
    }

    /// Opens a named u-dma-buf device, such as `udmabuf0`.
    pub(crate) fn open_with_name(name: &str) -> io::Result<Self> {
        let fd = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_SYNC)
            .open(format!("/dev/{name}"))?;

        Ok(Self {
            fd,
            sysfs_path: PathBuf::from(U_DMA_BUF_CLASS_PATH).join(name),
            offset: 0,
        })
    }

    /// Returns the total size of the DMA buffer.
    pub(crate) fn size_total(&self) -> io::Result<usize> {
        self.read_attribute("size")?.parse().map_err(|e| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("Failed to parse size: {e}"),
            )
        })
    }

    /// Returns the physical address of the DMA buffer.
    pub(crate) fn phys_addr(&self) -> io::Result<u64> {
        let str = self.read_attribute("phys_addr")?;
        u64::from_str_radix(str.trim_start_matches("0x"), 16).map_err(|e| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("Failed to parse size: {e}"),
            )
        })
    }

    fn read_attribute(&self, attr: &str) -> io::Result<String> {
        let path = self.sysfs_path.join(attr);
        let mut content = String::new();
        let _ignore = File::open(&path)?.read_to_string(&mut content)?;
        Ok(content.trim().to_owned())
    }

    #[allow(clippy::cast_possible_wrap)]
    fn create(&mut self, len: usize) -> io::Result<DmaBuf> {
        let size_total = self.size_total()?;
        if self.offset.checked_add(len).is_none_or(|x| x > size_total) {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                format!("Failed to allocate memory of length: {len} bytes"),
            ));
        }

        let ptr = unsafe {
            libc::mmap(
                ptr::null_mut(),
                len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                self.fd.as_raw_fd(),
                self.offset as i64,
            )
        };

        if ptr == libc::MAP_FAILED {
            return Err(io::Error::new(io::ErrorKind::Other, "Failed to map memory"));
        }

        unsafe {
            ptr::write_bytes(ptr.cast::<u8>(), 0, len);
        }

        let mmap = MmapMut::new(ptr, len);
        let phys_addr_raw = self.phys_addr()? + self.offset as u64;
        let phys_addr = PhysAddr::new(phys_addr_raw);

        self.offset += len;

        Ok(DmaBuf::new(mmap, phys_addr))
    }
}

impl DmaBufAllocator for UDmaBufAllocator {
    fn alloc(&mut self, len: usize) -> io::Result<DmaBuf> {
        self.create(len)
    }
}

#[cfg(test)]
mod tests {

    use super::*;

    #[test]
    #[allow(clippy::print_stderr)]
    fn allocate_pages() {
        let Ok(mut allocator) = UDmaBufAllocator::open() else {
            eprintln!("WARN: test 'allocate_pages' was skipped as it needs u-dma-buf kernel module to be loaded");
            return;
        };
        let mut x = allocator.create(0x1000).unwrap();
        assert_eq!(x.len(), 0x1000);
        x.copy_from(0, &[1; 1]);
        let mut x = allocator.create(0x4000).unwrap();
        assert_eq!(x.len(), 0x4000);
        x.copy_from(0, &[1; 1]);
    }
}
