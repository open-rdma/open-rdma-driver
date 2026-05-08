use std::{io, net::Ipv4Addr};

use ipnetwork::Ipv4Network;

use crate::{
    mem::DmaBufAllocator,
    net::config::{MacAddress, NetworkConfig},
    ringbuf::DescRingBufAllocator,
    verbs::dev::{EmulatedHwDevice, HwDevice, PciHwDevice},
};

use crate::{cmd::CommandConfigurator, csr::DeviceAdaptor};
static HEAP_ALLOCATOR: sim_alloc::Simalloc = sim_alloc::Simalloc::new();

/// Device for testing
#[derive(Debug, Clone, Copy)]
#[non_exhaustive]
pub struct TestDevice;

impl TestDevice {
    /// Init
    #[allow(
        clippy::unwrap_used,
        clippy::unwrap_in_result,
        clippy::missing_errors_doc,
        clippy::missing_panics_doc
    )]
    #[inline]
    pub fn init() -> io::Result<Self> {
        let device = PciHwDevice::open_default().unwrap();
        device.reset().unwrap();
        let adaptor = device.new_adaptor().unwrap();
        let mut allocator = device.new_dma_buf_allocator().unwrap();
        let mut rb_allocator = DescRingBufAllocator::new(&mut allocator);
        let cmd_controller =
            CommandConfigurator::init(&adaptor, rb_allocator.alloc()?, rb_allocator.alloc()?)
                .unwrap();
        let network_config = NetworkConfig {
            ip: Ipv4Network::new("10.0.0.2".parse().unwrap(), 24).unwrap(),
            peer_ip: "10.0.0.1".parse().unwrap(),
            gateway: Some("10.0.0.1".parse().unwrap()),
            mac: MacAddress([0; 6]),
        };
        cmd_controller.set_network(network_config);

        Ok(Self)
    }

    /// Init
    #[allow(
        clippy::unwrap_used,
        clippy::unwrap_in_result,
        clippy::missing_errors_doc,
        clippy::missing_panics_doc
    )]
    #[inline]
    pub fn init_emulated() -> io::Result<Self> {
        sim_alloc::init_global_allocator(0, &HEAP_ALLOCATOR);
        let device = EmulatedHwDevice::new("127.0.0.1:7701".into());
        // device.reset().unwrap();
        // device.init_dma_engine().unwrap();
        let adaptor = device.new_adaptor().unwrap();
        let mut allocator = device.new_dma_buf_allocator().unwrap();
        let mut rb_allocator = DescRingBufAllocator::new(&mut allocator);
        let cmd_controller =
            CommandConfigurator::init(&adaptor, rb_allocator.alloc()?, rb_allocator.alloc()?)
                .unwrap();
        let network_config = NetworkConfig {
            ip: Ipv4Network::new("10.0.0.2".parse().unwrap(), 24).unwrap(),
            peer_ip: "10.0.0.1".parse().unwrap(),
            gateway: Some("10.0.0.1".parse().unwrap()),
            mac: MacAddress([1; 6]),
        };
        cmd_controller.set_network(network_config);

        let data = adaptor.read_csr(0x100);
        //adaptor.write_csr(0x100, data).unwrap();
        Ok(Self)
    }
}
