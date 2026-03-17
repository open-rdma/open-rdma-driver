//! Emulated mode user memory handler.

use std::{io, sync::Arc};

use crate::{
    mem::{
        address::{AddressResolver, PaVaMap, PhysAddrResolverLinuxX86},
        get_num_page, PAGE_SIZE,
    },
    types::{PageAlignedVirtAddr, PhysAddr, VirtAddr},
};

use super::{MemoryPinner, UmemHandler};

/// Emulated mode user memory handler.
///
/// Uses PA-VA mapping table to simulate address translation, suitable for simulation environment.
/// 需要真正地pin住内存，来模仿实际的情况
pub(crate) struct EmulatedUmemHandler {
    resolver: PhysAddrResolverLinuxX86,
    pa_va_map: Arc<parking_lot::RwLock<PaVaMap>>,
}

impl EmulatedUmemHandler {
    /// Creates a new emulated mode user memory handler.
    pub(crate) fn new(pa_va_map: Arc<parking_lot::RwLock<PaVaMap>>) -> Self {
        Self {
            resolver: PhysAddrResolverLinuxX86,
            pa_va_map,
        }
    }
}

impl MemoryPinner for EmulatedUmemHandler {
    fn pin_pages(&self, addr: VirtAddr, length: usize) -> io::Result<()> {
        let result = unsafe { libc::mlock(addr.as_ptr::<std::ffi::c_void>(), length) };
        if result != 0 {
            return Err(io::Error::new(io::ErrorKind::Other, "failed to lock pages"));
        }

        let num_pages = get_num_page(addr.as_u64(), length);
        // Align down to page boundary for virt_to_phys_range
        let aligned_addr = PageAlignedVirtAddr::align_down(addr);
        let pas = self.resolver.virt_to_phys_range(aligned_addr, num_pages)?;
        for (i, pa) in pas.iter().enumerate() {
            // TODO 增加错误处理，不够严谨
            let pa = pa.unwrap();
            let va = addr
                .offset(i as u64 * PAGE_SIZE as u64)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "address overflow"))?;
            let mut pa_va_map = self.pa_va_map.write();
            // Convert aligned phys addr to regular PhysAddr for pa_va_map
            pa_va_map.insert(pa.into_inner(), va, PAGE_SIZE);
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

        let num_pages = get_num_page(addr.as_u64(), length);
        // Align down to page boundary for virt_to_phys_range
        let aligned_addr = PageAlignedVirtAddr::align_down(addr);
        let pas = self.resolver.virt_to_phys_range(aligned_addr, num_pages)?;
        for (i, pa) in pas.iter().enumerate() {
            // TODO 增加错误处理，不够严谨
            let pa = pa.unwrap();

            let mut pa_va_map = self.pa_va_map.write();
            // Convert aligned phys addr to regular PhysAddr for pa_va_map
            pa_va_map.remove(pa.into_inner());
        }
        Ok(())
    }
}

impl AddressResolver for EmulatedUmemHandler {
    fn virt_to_phys(&self, virt_addr: VirtAddr) -> io::Result<Option<PhysAddr>> {
        Ok(self.pa_va_map.read().lookup_by_va(virt_addr))
    }
}

impl UmemHandler for EmulatedUmemHandler {}
