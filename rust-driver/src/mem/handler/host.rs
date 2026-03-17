//! Host mode user memory handler.

use std::io;

use crate::{
    mem::address::{AddressResolver, PhysAddrResolverLinuxX86},
    types::{PageAlignedPhysAddr, PageAlignedVirtAddr, PhysAddr, VirtAddr},
};

use super::{MemoryPinner, UmemHandler};

/// Host mode user memory handler.
///
/// Uses real physical address resolution and locks pages via mlock.
pub(crate) struct HostUmemHandler {
    resolver: PhysAddrResolverLinuxX86,
}

impl HostUmemHandler {
    /// Creates a new host mode user memory handler.
    pub(crate) fn new() -> Self {
        Self {
            resolver: PhysAddrResolverLinuxX86,
        }
    }
}

// TODO cuda Unified Memory 和 Pin memory 这两套系统可能会冲突，需要再次确认，同时传进来的时候可能就已经pin住了
impl MemoryPinner for HostUmemHandler {
    fn pin_pages(&self, addr: VirtAddr, length: usize) -> io::Result<()> {
        let result = unsafe { libc::mlock(addr.as_ptr::<std::ffi::c_void>(), length) };
        if result != 0 {
            return Err(io::Error::new(io::ErrorKind::Other, "failed to lock pages"));
        }
        Ok(())
    }

    fn unpin_pages(&self, addr: VirtAddr, length: usize) -> io::Result<()> {
        let result = unsafe { libc::munlock(addr.as_ptr::<std::ffi::c_void>(), length) };
        if result != 0 {
            return Err(io::Error::new(
                io::ErrorKind::Other,
                "failed to unlock pages",
            ));
        }
        Ok(())
    }
}

impl AddressResolver for HostUmemHandler {
    fn virt_to_phys(&self, virt_addr: VirtAddr) -> io::Result<Option<PhysAddr>> {
        self.resolver.virt_to_phys(virt_addr)
    }

    fn virt_to_phys_range(
        &self,
        start_addr: PageAlignedVirtAddr,
        num_pages: usize,
    ) -> io::Result<Vec<Option<PageAlignedPhysAddr>>> {
        self.resolver.virt_to_phys_range(start_addr, num_pages)
    }
}

impl UmemHandler for HostUmemHandler {}
