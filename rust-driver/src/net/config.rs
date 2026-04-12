#![allow(clippy::module_name_repetitions)] // exported

use std::{io, net::Ipv4Addr, str::FromStr};

use ipnetwork::Ipv4Network;
use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Trait for network devices that can provide MAC address and DHCP resolution
pub trait NetworkResolver: Send + Sync + 'static {
    /// Resolve network configuration using DHCP
    ///
    /// # Errors
    /// Returns an error if dynamic discovery fails
    fn resolve_dynamic(&self) -> io::Result<NetworkConfig>;
}

/// MAC address represented as 6 bytes
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub struct MacAddress(pub [u8; 6]);

impl FromStr for MacAddress {
    type Err = ParseMacAddressError;

    #[inline]
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let bytes: Vec<&str> = s.split(':').collect();
        if bytes.len() != 6 {
            return Err(ParseMacAddressError);
        }

        let mut addr = [0u8; 6];
        for (x, byte) in addr.iter_mut().zip(bytes) {
            *x = u8::from_str_radix(byte, 16).map_err(|_err| ParseMacAddressError)?;
        }

        Ok(MacAddress(addr))
    }
}

/// Parse error
#[non_exhaustive]
#[derive(Debug, Error, Clone, Copy)]
#[error("invalid MAC address")]
pub struct ParseMacAddressError;

impl<'de> Deserialize<'de> for MacAddress {
    #[inline]
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let s = <String>::deserialize(deserializer)?;
        MacAddress::from_str(&s).map_err(serde::de::Error::custom)
    }
}

impl Serialize for MacAddress {
    #[inline]
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.collect_str(self)
    }
}

impl std::fmt::Display for MacAddress {
    #[inline]
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}",
            self.0[0], self.0[1], self.0[2], self.0[3], self.0[4], self.0[5]
        )
    }
}

impl From<MacAddress> for u64 {
    #[inline]
    fn from(mac: MacAddress) -> u64 {
        let mut bytes = [0u8; 8];
        bytes[..6].copy_from_slice(&mac.0);
        u64::from_le_bytes(bytes)
    }
}

impl From<u64> for MacAddress {
    #[inline]
    fn from(mac: u64) -> MacAddress {
        let bytes = mac.to_le_bytes();
        let mut mac_bytes = [0u8; 6];
        mac_bytes.copy_from_slice(&bytes[..6]);
        MacAddress(mac_bytes)
    }
}

/// Static network configuration containing IP network, gateway and MAC address
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[non_exhaustive]
pub struct NetworkConfig {
    /// IP network (address and subnet)
    pub ip: Ipv4Network,
    /// Peer ip
    pub peer_ip: Ipv4Addr,
    /// Gateway IP address
    pub gateway: Option<Ipv4Addr>,
    /// MAC address
    pub mac: MacAddress,
}

impl Default for NetworkConfig {
    #[inline]
    fn default() -> Self {
        Self {
            ip: Ipv4Network::new(Ipv4Addr::new(0, 0, 0, 0), 0).expect("invalid address"),
            peer_ip: Ipv4Addr::new(0, 0, 0, 0),
            gateway: None,
            mac: MacAddress::default(),
        }
    }
}

/// Network mode configuration - either static or DHCP
#[non_exhaustive]
pub enum NetworkMode {
    /// Static network configuration
    Static(NetworkConfig),
    /// Dynamic network configuration
    Dynamic {
        /// Network device to use for dynamic resolution
        device: Box<dyn NetworkResolver>,
    },
}

impl NetworkMode {
    /// Resolve the network configuration based on the mode
    ///
    /// For static mode, returns the static config directly.
    /// For DHCP mode, resolves configuration using the device.
    pub(crate) fn resolve(&self) -> io::Result<NetworkConfig> {
        match *self {
            NetworkMode::Static(ref config) => Ok(*config),
            NetworkMode::Dynamic { ref device } => device.resolve_dynamic(),
        }
    }
}

impl std::fmt::Debug for NetworkMode {
    #[inline]
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match *self {
            NetworkMode::Static(ref config) => f.debug_tuple("Static").field(config).finish(),
            NetworkMode::Dynamic { .. } => f.debug_struct("DHCP").finish(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct MockDevice;

    impl NetworkResolver for MockDevice {
        fn resolve_dynamic(&self) -> io::Result<NetworkConfig> {
            Ok(NetworkConfig {
                ip: Ipv4Network::new("10.0.0.2".parse().unwrap(), 24).unwrap(),
                gateway: Some("10.0.0.1".parse().unwrap()),
                mac: MacAddress([0; 6]),
                peer_ip: "10.0.0.1".parse().unwrap(),
            })
        }
    }

    fn dhcp_resolution_ok() {
        let device = MockDevice;
        let mode = NetworkMode::Dynamic {
            device: Box::new(device),
        };
        let result = mode.resolve();
        assert!(result.is_ok());
    }
}
