//! Virtual to physical address resolution.

use std::{
    fs::File,
    io::{self, Read, Seek},
};

use crate::types::{PageAlignedPhysAddr, PageAlignedVirtAddr, PhysAddr, VirtAddr};

/// Size of the PFN (Page Frame Number) mask in bytes
const PFN_MASK_SIZE: usize = 8;
/// PFN are bits 0-54 (see pagemap.txt in Linux Documentation)
const PFN_MASK: u64 = (1 << 55) - 1;
/// Bit indicating if a page is present in memory
const PAGE_PRESENT_BIT: u8 = 63;

#[cfg(feature = "page_size_2m")]
const PAGE_SIZE: u64 = 0x20_0000;
#[cfg(feature = "page_size_4k")]
const PAGE_SIZE: u64 = 0x1000;

/// Returns the system's base page size in bytes.
#[allow(unsafe_code, clippy::cast_sign_loss)]
fn get_base_page_size() -> u64 {
    unsafe { libc::sysconf(libc::_SC_PAGESIZE) as u64 }
}

/// Trait for resolving virtual addresses to physical addresses.
pub(crate) trait AddressResolver {
    /// Converts a virtual address to a physical address
    ///
    /// # Returns
    ///
    /// An optional physical address. `None` indicates
    /// the page is not present in physical memory.
    ///
    /// # Errors
    ///
    /// Returns an IO error if address resolving fails.
    fn virt_to_phys(&self, virt_addr: VirtAddr) -> io::Result<Option<PhysAddr>>;

    /// Converts a range of page-aligned virtual addresses to physical addresses
    ///
    /// # Arguments
    ///
    /// * `start_addr` - Page-aligned virtual address (alignment guaranteed by type system)
    /// * `num_pages` - Number of pages to translate
    ///
    /// # Returns
    ///
    /// A vector of optional page-aligned physical addresses. `None` indicates
    /// the page is not present in physical memory.
    ///
    /// # Errors
    ///
    /// Returns an IO error if address resolving fails.
    #[allow(clippy::as_conversions, unsafe_code)]
    fn virt_to_phys_range(
        &self,
        start_addr: PageAlignedVirtAddr,
        num_pages: usize,
    ) -> io::Result<Vec<Option<PageAlignedPhysAddr>>> {
        // No need for runtime alignment check - type system guarantees it!
        (0..num_pages as u64)
            .map(|x| {
                let addr = start_addr
                    .into_inner()
                    .offset(x * PAGE_SIZE)
                    .ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidInput, "address overflow")
                    })?;
                let phys_addr = self.virt_to_phys(addr)?;
                // SAFETY: Physical pages from kernel are always page-aligned
                Ok(phys_addr.map(|pa| unsafe { PageAlignedPhysAddr::new_unchecked(pa) }))
            })
            .collect::<Result<_, _>>()
    }
}

/// Type alias for the default physical address resolver.
pub(crate) type PhysAddrResolver = PhysAddrResolverLinuxX86;

/// Physical address resolver for Linux x86-64 systems.
///
/// Uses `/proc/self/pagemap` to translate virtual addresses to physical addresses.
pub(crate) struct PhysAddrResolverLinuxX86;

#[allow(
    clippy::as_conversions,
    clippy::arithmetic_side_effects,
    clippy::host_endian_bytes,
    unsafe_code
)]
impl AddressResolver for PhysAddrResolverLinuxX86 {
    fn virt_to_phys(&self, virt_addr: VirtAddr) -> io::Result<Option<PhysAddr>> {
        let virt_addr_raw = virt_addr.as_u64();
        let base_page_size = get_base_page_size();
        let file = File::open("/proc/self/pagemap")?;
        let virt_pfn = virt_addr_raw / base_page_size;
        let offset = PFN_MASK_SIZE as u64 * virt_pfn;
        let mut buf = [0u8; PFN_MASK_SIZE];

        let mut get_pa_from_file = move |mut file: File| {
            let _pos = file.seek(io::SeekFrom::Start(offset))?;
            file.read_exact(&mut buf)?;
            let entry = u64::from_ne_bytes(buf);

            if (entry >> PAGE_PRESENT_BIT) & 1 != 0 {
                let phy_pfn = entry & PFN_MASK;
                let phys_addr = phy_pfn * base_page_size + virt_addr_raw % base_page_size;
                return Ok(Some(PhysAddr::new(phys_addr)));
            }

            log::warn!("translate fail! virt_addr = {virt_addr_raw:x}");
            Ok(None)
        };

        if let pa @ Some(_) = get_pa_from_file(file)? {
            return Ok(pa);
        }

        if let Ok(gpu_ptr_translator) = File::open("/dev/gpu_ptr_translator") {
            if let res @ Ok(Some(_)) = get_pa_from_file(gpu_ptr_translator) {
                return res;
            }
        }

        Ok(None)
    }

    fn virt_to_phys_range(
        &self,
        start_addr: PageAlignedVirtAddr,
        num_pages: usize,
    ) -> io::Result<Vec<Option<PageAlignedPhysAddr>>> {
        // Type system guarantees alignment - no runtime check needed!
        let start_addr_raw = start_addr.as_u64();
        let base_page_size = get_base_page_size();
        let mut phy_addrs = vec![None; num_pages];
        let mut file = File::open("/proc/self/pagemap")?;
        let mut buf = [0u8; PFN_MASK_SIZE];

        let mut maybe_gpu_ptr = true;

        let mut addr = start_addr_raw;
        for pa in &mut phy_addrs {
            // /proc/self/pagemap is always indexed by system base page size (4KB)
            // even when using huge pages (2MB). For page-aligned virtual addresses,
            // pagemap returns the PFN of the corresponding physical page.
            let virt_pfn = addr / base_page_size;
            let offset = PFN_MASK_SIZE as u64 * virt_pfn;
            let _pos = file.seek(io::SeekFrom::Start(offset))?;
            file.read_exact(&mut buf)?;
            let entry = u64::from_ne_bytes(buf);
            if (entry >> PAGE_PRESENT_BIT) & 1 != 0 {
                let phys_pfn = entry & PFN_MASK;
                // pagemap returns PFN in units of base_page_size (4KB)
                let phys_addr = phys_pfn * base_page_size;

                // Safety check: Verify physical address is valid
                if phys_addr == 0 {
                    log::error!(
                        "Physical address is zero for VA 0x{:x}, PFN=0x{:x}",
                        addr,
                        phys_pfn
                    );
                    // Don't set pa, leave it as None to trigger error in caller
                } else {
                    // SAFETY: Physical pages from kernel pagemap are always page-aligned
                    *pa = Some(unsafe {
                        PageAlignedPhysAddr::new_unchecked(PhysAddr::new(phys_addr))
                    });
                    maybe_gpu_ptr = false;
                }
            }

            addr += PAGE_SIZE;
        }

        if maybe_gpu_ptr {
            debug_assert!(phy_addrs.iter().all(Option::is_none), "invalid address");

            let Ok(gpu_ptr_translator) = File::open("/dev/gpu_ptr_translator") else {
                return Ok(phy_addrs);
            };

            addr = start_addr_raw;
            for pa in &mut phy_addrs {
                // GPU pointers also indexed by base_page_size in pagemap
                let virt_pfn = addr / base_page_size;
                let offset = PFN_MASK_SIZE as u64 * virt_pfn;
                let _pos = file.seek(io::SeekFrom::Start(offset))?;
                file.read_exact(&mut buf)?;
                let entry = u64::from_ne_bytes(buf);
                if (entry >> PAGE_PRESENT_BIT) & 1 != 0 {
                    let phys_pfn = entry & PFN_MASK;
                    let phys_addr = phys_pfn * base_page_size;

                    // Safety check: Verify GPU physical address is valid
                    if phys_addr == 0 {
                        log::error!(
                            "GPU physical address is zero for VA 0x{:x}, PFN=0x{:x}",
                            addr,
                            phys_pfn
                        );
                        // Don't set pa, leave it as None to trigger error in caller
                    } else {
                        // SAFETY: GPU physical pages are also page-aligned
                        *pa = Some(unsafe {
                            PageAlignedPhysAddr::new_unchecked(PhysAddr::new(phys_addr))
                        });
                    }
                }

                addr += PAGE_SIZE;
            }
        }
        log::info!(
            "virt_addr = {start_addr_raw:x},phy_addrs = {:?}\n",
            phy_addrs
        );
        Ok(phy_addrs)
    }
}

// TODO now: emulation mode is use PhysAddrResolverLinuxX86 as well

// pub(crate) struct PhysAddrResolverEmulated {
//     heap_start_addr: u64,
// }

// impl PhysAddrResolverEmulated {
//     pub(crate) fn new(heap_start_addr: u64) -> Self {
//         Self { heap_start_addr }
//     }
// }

// impl AddressResolver for PhysAddrResolverEmulated {
//     fn virt_to_phys(&self, virt_addr: VirtAddr) -> io::Result<Option<PhysAddr>> {
//         let virt_addr_raw = virt_addr.as_u64();
//         debug!(
//             "virt_addr = {virt_addr_raw:x}, heap_start_addr={:x}\n",
//             self.heap_start_addr
//         );
//         Ok(virt_addr_raw
//             .checked_sub(self.heap_start_addr)
//             .map(PhysAddr::new))
//     }
// }
