//! Hardware CSR Access Implementations
//!
//! This module provides direct hardware register access implementations for PCIe devices.
//! It supports two access methods:
//!
//! # Access Methods
//!
//! ## VFIO-based Access (`VfioPciCsrAdaptor`)
//! - Uses the Linux VFIO (Virtual Function I/O) framework
//! - Provides userspace driver capability with device isolation
//! - Enables DMA mapping and interrupt handling
//! - Preferred for production deployments requiring IOMMU protection
//!
//! ## Sysfs-based Access (`SysfsPciCsrAdaptor`)
//! - Direct MMIO access via `/sys/bus/pci/devices/<bdf>/resource0`
//! - Simpler setup without VFIO kernel modules
//! - Useful for development and debugging
//! - Requires root privileges
//!
//! # Memory Management
//!
//! Both adaptors integrate with the memory subsystem to provide:
//! - DMA buffer allocation via DMA-BUF or udmabuf
//! - Physical address resolution for user memory
//! - Page-aligned memory registration
//!
//! # Register Access
//!
//! All CSR access is performed through BAR0 (Base Address Register 0) of the PCIe device:
//! - 32-bit read/write operations at aligned addresses
//! - Memory-mapped I/O (MMIO) with proper synchronization
//!
//! # Example
//!
//! ```rust,ignore
//! use crate::csr::hardware::VfioPciCsrAdaptor;
//!
//! // Open device via VFIO
//! let adaptor = VfioPciCsrAdaptor::new("uverbs0")?;
//!
//! // Read/write CSRs
//! let mode = adaptor.read_csr(0x1000)?;
//! adaptor.write_csr(0x2000, 0x42)?;
//! ```

use log::{debug, trace};
use memmap2::{MmapMut, MmapOptions};
use parking_lot::Mutex;
use pci_driver::{
    backends::vfio::VfioPciDevice,
    device::PciDevice,
    regions::{MappedOwningPciRegion, PciRegion, Permissions},
};
use std::{fs::OpenOptions, io, path::Path, sync::Arc};

use crate::ring::traits::DeviceAdaptor;

const BAR_INDEX: usize = 0;
const BAR_MAP_RANGE_END: u64 = 4096;

#[derive(Clone, Debug)]
pub(crate) struct VfioPciCsrAdaptor {
    bar: Arc<MappedOwningPciRegion>,
}

impl VfioPciCsrAdaptor {
    fn new(sysfs_path: impl AsRef<Path>) -> io::Result<Self> {
        let path = sysfs_path.as_ref();
        let device = VfioPciDevice::open(path).map_err(|err| {
            io::Error::new(
                io::ErrorKind::Other,
                format!("Failed to open sysfs_path: {err}"),
            )
        })?;
        let bar = device.bar(BAR_INDEX).ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "Expected device to have BAR")
        })?;
        let mapped_bar = bar.map(..BAR_MAP_RANGE_END, Permissions::ReadWrite)?;
        Ok(Self {
            bar: Arc::new(mapped_bar),
        })
    }
}

// TODO: use u64 instead of usize
impl DeviceAdaptor for VfioPciCsrAdaptor {
    fn read_csr(&self, addr: usize) -> io::Result<u32> {
        self.bar.read_le_u32(addr as u64)
    }

    fn write_csr(&self, addr: usize, data: u32) -> io::Result<()> {
        self.bar.write_le_u32(addr as u64, data)
    }
}

#[derive(Clone, Debug)]
pub(crate) struct SysfsPciCsrAdaptor {
    bar: Arc<Mutex<MmapMut>>,
}

#[allow(unsafe_code)]
impl SysfsPciCsrAdaptor {
    pub(crate) fn new(sysfs_path: impl AsRef<Path>) -> io::Result<Self> {
        let bar_path = sysfs_path.as_ref().join(format!("resource{BAR_INDEX}"));
        debug!("path for user space PCIe BAR access: {bar_path:?}");
        let file = OpenOptions::new().read(true).write(true).open(&bar_path)?;
        let mmap = unsafe { MmapOptions::new().map_mut(&file)? };

        Ok(Self {
            bar: Arc::new(Mutex::new(mmap)),
        })
    }
}

#[allow(unsafe_code, clippy::cast_ptr_alignment)]
impl DeviceAdaptor for SysfsPciCsrAdaptor {
    fn read_csr(&self, addr: usize) -> io::Result<u32> {
        if addr % 4 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unaligned access",
            ));
        }

        let bar = self.bar.lock();
        unsafe {
            let ptr = bar.as_ptr().add(addr);
            let ret = ptr.cast::<u32>().read_volatile();
            trace!(
                "read csr: addr=0x{:x}, bar_offset=0x{:x}, val=0x{:x}",
                ptr as usize,
                addr,
                ret
            );
            Ok(ret)
        }
    }

    fn write_csr(&self, addr: usize, data: u32) -> io::Result<()> {
        if addr % 4 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unaligned access",
            ));
        }

        let mut bar = self.bar.lock();
        unsafe {
            let ptr = bar.as_mut_ptr().add(addr);
            trace!(
                "write csr: addr=0x{:x}, bar_offset=0x{:x}, val=0x{:x}",
                ptr as usize,
                addr,
                data
            );
            ptr.cast::<u32>().write_volatile(data);
        }

        Ok(())
    }
}

pub(crate) struct CustomCsrConfigurator {
    bar: MmapMut,
}

#[allow(unsafe_code, clippy::cast_ptr_alignment)]
impl CustomCsrConfigurator {
    pub(crate) fn new(sysfs_path: impl AsRef<Path>) -> io::Result<Self> {
        let bar_path = sysfs_path.as_ref().join(format!("resource{BAR_INDEX}"));
        let file = OpenOptions::new().read(true).write(true).open(&bar_path)?;
        let mmap = unsafe { MmapOptions::new().map_mut(&file)? };

        Ok(Self { bar: mmap })
    }

    pub(crate) fn set_loopback(&mut self) {
        const ADDR: usize = 0x180;
        unsafe {
            self.bar
                .as_mut_ptr()
                .add(ADDR)
                .cast::<u32>()
                .write_volatile(1);
        }
    }

    pub(crate) fn set_seed(&mut self, seed: u32) {
        const ADDR: usize = 0x184;
        unsafe {
            self.bar
                .as_mut_ptr()
                .add(ADDR)
                .cast::<u32>()
                .write_volatile(seed);
        }
    }

    pub(crate) fn set_drop_thresh(&mut self, rate: u8) {
        const ADDR: usize = 0x188;
        unsafe {
            self.bar
                .as_mut_ptr()
                .add(ADDR)
                .cast::<u32>()
                .write_volatile(u32::from(rate));
        }
    }
}
