use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::Arc,
};

use parking_lot::RwLock;
use pci_info::PciInfo;

use crate::memory_proxy_simple::{SimpleMemoryProxyClient, SimpleTcpClient};

use crate::ring::traits::DeviceAdaptor;
use crate::{
    constants::{DEVICE_ID, PCI_SYSFS_BUS_PATH, VENDER_ID},
    error::Result,
    mem::{EmulatedPageAllocator, EmulatedUmemHandler, HostUmemHandler, PaVaMap, UDmaBufAllocator},
    ring::csr::{emulated::EmulatedDevice, hardware::SysfsPciCsrAdaptor},
};

use super::mock::{MockDeviceAdaptor, MockDmaBufAllocator, MockUmemHandler};

pub(crate) trait HwDevice {
    type Adaptor: DeviceAdaptor;
    type DmaBufAllocator: crate::mem::DmaBufAllocator;
    type UmemHandler: crate::mem::UmemHandler;

    fn new_adaptor(&self) -> Result<Self::Adaptor>;
    fn new_dma_buf_allocator(&self) -> Result<Self::DmaBufAllocator>;
    fn new_umem_handler(&self) -> Self::UmemHandler;
}

pub(crate) struct PciHwDevice {
    sysfs_path: PathBuf,
}

impl PciHwDevice {
    pub(crate) fn new(sysfs_path: impl AsRef<Path>) -> Self {
        Self {
            sysfs_path: sysfs_path.as_ref().into(),
        }
    }

    pub(crate) fn open_default() -> io::Result<Self> {
        let build_err = || io::Error::new(io::ErrorKind::Other, "Failed to open device");
        let info = PciInfo::enumerate_pci().map_err(|_err| build_err())?;
        let device = info
            .iter()
            .flatten()
            .find(|d| d.vendor_id() == VENDER_ID && d.device_id() == DEVICE_ID)
            .ok_or_else(build_err)?;
        let location = device.location().map_err(|_err| build_err())?;
        let sysfs_path = PathBuf::from(PCI_SYSFS_BUS_PATH).join(location.to_string());

        Ok(Self { sysfs_path })
    }

    pub(crate) fn reset(&self) -> io::Result<()> {
        let path = self.sysfs_path.join("reset");
        fs::write(path, "1")
    }

    #[cfg(feature = "debug_csrs")]
    pub(crate) fn set_custom(&self) -> io::Result<()> {
        use log::info;

        use crate::csr::hardware::CustomCsrConfigurator;

        let mut cfg = CustomCsrConfigurator::new(&self.sysfs_path)?;
        if std::env::var("ENABLE_LOOPBACK").unwrap_or_default() == "1" {
            cfg.set_loopback();
            info!("loopback enabled");
        }
        let drop_thresh = std::env::var("DROP_THRESHOLD")
            .ok()
            .and_then(|s| s.parse::<u8>().ok())
            .unwrap_or(1);
        cfg.set_drop_thresh(drop_thresh);
        info!("packet drop threshold set to: {drop_thresh}");
        let seed = std::env::var("SEED")
            .ok()
            .and_then(|s| u32::from_str_radix(&s, 16).ok())
            .unwrap_or(0x3131_3131);
        cfg.set_seed(seed);
        info!("packet drop rng seed set to: {seed}");

        Ok(())
    }
}

impl HwDevice for PciHwDevice {
    type Adaptor = SysfsPciCsrAdaptor;

    type DmaBufAllocator = UDmaBufAllocator;

    type UmemHandler = HostUmemHandler;

    fn new_adaptor(&self) -> Result<Self::Adaptor> {
        SysfsPciCsrAdaptor::new(&self.sysfs_path).map_err(Into::into)
    }

    fn new_dma_buf_allocator(&self) -> Result<Self::DmaBufAllocator> {
        UDmaBufAllocator::open().map_err(Into::into)
    }

    fn new_umem_handler(&self) -> Self::UmemHandler {
        HostUmemHandler::new()
    }
}

pub(crate) struct EmulatedHwDevice {
    addr: String,
    pa_va_map: Arc<RwLock<PaVaMap>>,
}

impl EmulatedHwDevice {
    pub(crate) fn new(csr_addr: String, pcie_addr: String) -> Self {
        // 需要启动pcie client
        let pa_va_map = Arc::new(RwLock::new(PaVaMap::new()));
        let tcp_server_addr = pcie_addr.parse().unwrap();
        let tcp_client = SimpleTcpClient::new(tcp_server_addr).unwrap();
        let mem_proxy_client = SimpleMemoryProxyClient::new(tcp_client, pa_va_map.clone());

        let _ = mem_proxy_client.start_processing();
        Self {
            addr: csr_addr,
            pa_va_map,
        }
    }

    /// Get reference to the PA↔VA mapping table (simulation mode only)
    pub(crate) fn pa_va_map(&self) -> &Arc<RwLock<PaVaMap>> {
        &self.pa_va_map
    }
}

impl HwDevice for EmulatedHwDevice {
    type Adaptor = EmulatedDevice;

    type DmaBufAllocator = EmulatedPageAllocator<1>;

    type UmemHandler = EmulatedUmemHandler;

    fn new_adaptor(&self) -> Result<Self::Adaptor> {
        Ok(EmulatedDevice::new_with_addr(&self.addr))
    }

    fn new_dma_buf_allocator(&self) -> Result<Self::DmaBufAllocator> {
        let mut pa_va_map = self.pa_va_map.write();
        Ok(EmulatedPageAllocator::new(None, &mut pa_va_map))
    }

    fn new_umem_handler(&self) -> Self::UmemHandler {
        EmulatedUmemHandler::new(self.pa_va_map.clone())
    }
}

#[derive(Debug)]
pub(crate) struct MockHwDevice;

impl HwDevice for MockHwDevice {
    type Adaptor = MockDeviceAdaptor;

    type DmaBufAllocator = MockDmaBufAllocator;

    type UmemHandler = MockUmemHandler;

    fn new_adaptor(&self) -> Result<Self::Adaptor> {
        Ok(MockDeviceAdaptor)
    }

    fn new_dma_buf_allocator(&self) -> Result<Self::DmaBufAllocator> {
        Ok(MockDmaBufAllocator)
    }

    fn new_umem_handler(&self) -> Self::UmemHandler {
        MockUmemHandler
    }
}
