//! Device Operating Mode Configuration
//!
//! This module defines the operating modes for the RDMA device, which determine
//! the number of active channels and corresponding hardware resources.
//!
//! # Operating Modes
//!
//! The device supports three link speed configurations:
//!
//! ## Mode100G (Default)
//! - Single 100 Gbps channel
//! - 1 send ring, 1 metadata report ring
//! - Channel IDs: [0]
//! - Suitable for: Single high-speed connection
//!
//! ## Mode200G
//! - Dual 100 Gbps channels (200 Gbps total)
//! - 2 send rings, 2 metadata report rings
//! - Channel IDs: [0, 1]
//! - Suitable for: Load balancing or redundancy across two links
//!
//! ## Mode400G
//! - Quad 100 Gbps channels (400 Gbps total)
//! - 4 send rings, 4 metadata report rings
//! - Channel IDs: [0, 1, 2, 3]
//! - Suitable for: Maximum throughput deployments
//!
//! # Usage
//!
//! The mode is typically read from hardware CSR (`CSR_DEVICE_MODE_ADDR`) during
//! device initialization, then used to allocate the appropriate number of rings:
//!
//! ```rust,ignore
//! use crate::csr::{Mode, build_send_rings, EmulatedDevice};
//!
//! let dev = EmulatedDevice::new("uverbs0")?;
//! let mode = Mode::Mode400G;  // Or read from device CSR
//!
//! // Allocate rings based on mode
//! let send_rings = build_send_rings(dev, mode);
//! assert_eq!(send_rings.len(), mode.num_channel());  // 4 for Mode400G
//! ```
//!
//! # Channel Mapping
//!
//! Each mode provides:
//! - `num_channel()`: Total number of active channels
//! - `channel_ids()`: Slice of valid channel indices for iteration

#[derive(Default, Clone, Copy)]
pub(crate) enum Mode {
    Mode400G,
    Mode200G,
    #[default]
    Mode100G,
}

impl Mode {
    pub(crate) const fn num_channel(self) -> usize {
        match self {
            Mode::Mode100G => 1,
            Mode::Mode200G => 2,
            Mode::Mode400G => 4,
        }
    }

    pub(crate) const fn channel_ids(self) -> &'static [usize] {
        match self {
            Mode::Mode100G => &[0],
            Mode::Mode200G => &[0, 1],
            Mode::Mode400G => &[0, 1, 2, 3],
        }
    }
}
