#[cfg(feature = "hw")]
use core::panic;
use std::{
    fs::File,
    io::{self, Read},
    net::IpAddr,
    path::PathBuf,
};

#[cfg(feature = "mock")]
use default_net::Interface;
use ipnetwork::Ipv4Network;

use crate::{
    constants::BLUE_RDMA_SYSFS_PATH,
    net::config::NetworkConfig,
};

use super::config::MacAddress;

pub(crate) struct NetConfigReader;

impl NetConfigReader {
    pub(crate) fn read(sysfs_name: String) -> NetworkConfig {
        // TODO: need to test
        #[cfg(feature = "hw")]
        let interface = {
            let interface_name = match sysfs_name.as_str() {
                "uverbs0" => "blue0",
                "uverbs1" => "blue1",
                _ => panic!("unknown sysfs_name for sim: {}", sysfs_name),
            };
            default_net::get_interfaces()
                .into_iter()
                .find(|x| x.name == interface_name)
                .expect("blue-rdma netdev not present")
        };

        #[cfg(feature = "sim")]
        let interface = {
            let interface_name = Self::interface_name_from_sysfs(&sysfs_name);
            default_net::get_interfaces()
                .into_iter()
                .find(|x| x.name == interface_name)
                .expect("blue-rdma netdev not present")
        };

        #[cfg(feature = "mock")]
        // to pass the compiler
        let interface = Interface::default().unwrap();

        let ip = interface
            .ipv4
            .into_iter()
            .next()
            .expect("no ipv4 address configured");
        let ip = Ipv4Network::new(ip.addr, ip.prefix_len).expect("invalid address format");
        let gateway = interface.gateway.map(|x| match x.ip_addr {
            IpAddr::V4(ip) => ip,
            IpAddr::V6(ip) => unreachable!(),
        });
        let mac = interface.mac_addr.expect("no mac address configured");
        let mac = MacAddress(mac.octets());

        NetworkConfig {
            ip,
            peer_ip: 0.into(),
            gateway,
            mac,
        }
    }

    fn interface_name_from_sysfs(sysfs_name: &str) -> String {
        let port_index = sysfs_name
            .strip_prefix("uverbs")
            .and_then(|suffix| suffix.parse::<usize>().ok())
            .unwrap_or_else(|| panic!("unknown sysfs_name: {}", sysfs_name));

        format!("blue{}", port_index)
    }

    pub(crate) fn read_mac_sysfs() -> io::Result<u64> {
        let mac = Self::read_attribute_sysfs("mac")?;
        let bytes = mac
            .split(':')
            .map(|s| u8::from_str_radix(s, 16))
            .collect::<Result<Vec<_>, _>>()
            .expect("invalid mac format");
        assert_eq!(bytes.len(), 6, "invalid mac format");

        let mut result = 0;
        for b in bytes {
            result = (result << 8) | u64::from(b);
        }

        Ok(result)
    }

    #[allow(clippy::indexing_slicing)]
    pub(crate) fn read_ip_sysfs() -> io::Result<Option<u32>> {
        let gids = Self::read_attribute_sysfs("gids")?;
        for gid in gids.lines() {
            let bytes = gid
                .split(':')
                .map(|s| u16::from_str_radix(s, 16))
                .collect::<Result<Vec<_>, _>>()
                .expect("invalid gid format");
            assert_eq!(bytes.len(), 8, "invalid gid format");
            // only consider the first ipv4 address
            if bytes[0] == 0
                && bytes[1] == 0
                && bytes[2] == 0
                && bytes[3] == 0
                && bytes[4] == 0
                && bytes[5] == 0xffff
            {
                let result = (u32::from(bytes[6]) << 16) & u32::from(bytes[7]);
                return Ok(Some(result));
            }
        }

        Ok(None)
    }

    fn read_attribute_sysfs(attr: &str) -> io::Result<String> {
        let path = PathBuf::from(BLUE_RDMA_SYSFS_PATH).join(attr);
        let mut content = String::new();
        let _ignore = File::open(&path)?.read_to_string(&mut content)?;
        Ok(content.trim().to_owned())
    }
}
