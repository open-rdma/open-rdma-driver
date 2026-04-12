//! Ring Buffer Specifications and Constructors
//!
//! This module defines concrete ring buffer specifications for all ring types in the system
//! and provides type-safe constructors for creating ring instances.
//!
//! # Ring Types and Directions
//!
//! Each ring specification implements either `RingSpecToCard` or `RingSpecToHost` to indicate
//! the data flow direction:
//!
//! ## ToCard Rings (Host → Card)
//! Host is the producer, card is the consumer:
//! - `CmdReqSpec` - Management command requests
//! - `SimpleNicTxSpec` - Raw packet transmission
//! - `SendRingSpec` - RDMA send work requests
//!
//! ## ToHost Rings (Card → Host)
//! Card is the producer, host is the consumer:
//! - `CmdRespSpec` - Management command responses
//! - `SimpleNicRxSpec` - Raw packet reception
//! - `MetaReportRingSpec` - Packet metadata and completion reports
//!
//! # Type Aliases
//!
//! Convenient type aliases combine device adaptors with ring specifications:
//! ```rust,ignore
//! type SendRingCsr<Dev> = RingCsr<Dev, SendRingSpec>;
//! type MetaReportRingCsr<Dev> = RingCsr<Dev, MetaReportRingSpec>;
//! ```
//!
//! # Constructor Functions
//!
//! ## Singleton Rings
//! Functions for creating single-instance rings:
//! - `cmd_req_ring()` - Command request ring
//! - `cmd_resp_ring()` - Command response ring
//! - `simple_nic_tx_ring()` - Simple NIC TX ring
//! - `simple_nic_rx_ring()` - Simple NIC RX ring
//!
//! ## Multi-Channel Rings
//! Builder functions that create rings for all channels based on device mode:
//! - `build_send_rings()` - Creates send rings (1 for 100G, 2 for 200G, 4 for 400G)
//! - `build_meta_report_rings()` - Creates metadata report rings (same count as send rings)
//!
//! # Example
//!
//! ```rust,ignore
//! use crate::csr::{EmulatedDevice, Mode, build_send_rings, cmd_req_ring};
//!
//! let dev = EmulatedDevice::new("uverbs0")?;
//!
//! // Create singleton ring
//! let cmd_ring = cmd_req_ring(dev.clone());
//!
//! // Create multi-channel rings based on mode
//! let send_rings = build_send_rings(dev.clone(), Mode::Mode400G);
//! assert_eq!(send_rings.len(), 4);  // 4 channels in 400G mode
//! ```

use super::csr::{
    constants::{
        CMD_REQ_RING_BASE, CMD_RESP_RING_BASE, QP_RECV_RING_BASES, QP_SEND_RING_BASES,
        RING_BUF_LEN_BITS, SIMPLE_NIC_RX_RING_BASE, SIMPLE_NIC_TX_RING_BASE,
    },
    mode::Mode,
    ring_csr::RingCsr,
};

use super::descriptors::*;

use crate::ring::{
    descriptors::simple_nic::{SimpleNicRxQueueDesc, SimpleNicTxQueueDesc},
    traits::{DeviceAdaptor, RingSpec, RingSpecToCard, RingSpecToHost},
};

pub(crate) struct CmdReqSpec;
impl RingSpec for CmdReqSpec {
    const ELEMENT_NUM_EXP: u8 = RING_BUF_LEN_BITS;
    type Element = CmdQueueDesc;

    fn csr_base(&self) -> usize {
        CMD_REQ_RING_BASE
    }
}
impl RingSpecToCard for CmdReqSpec {}

pub(crate) struct CmdRespSpec;
impl RingSpec for CmdRespSpec {
    const ELEMENT_NUM_EXP: u8 = RING_BUF_LEN_BITS;
    type Element = CmdRespQueueDesc;

    fn csr_base(&self) -> usize {
        CMD_RESP_RING_BASE
    }
}
impl RingSpecToHost for CmdRespSpec {}

pub(crate) struct SimpleNicTxSpec;
impl RingSpec for SimpleNicTxSpec {
    const ELEMENT_NUM_EXP: u8 = RING_BUF_LEN_BITS;
    type Element = SimpleNicTxQueueDesc;

    fn csr_base(&self) -> usize {
        SIMPLE_NIC_TX_RING_BASE
    }
}
impl RingSpecToCard for SimpleNicTxSpec {}

pub(crate) struct SimpleNicRxSpec;
impl RingSpec for SimpleNicRxSpec {
    const ELEMENT_NUM_EXP: u8 = RING_BUF_LEN_BITS;
    type Element = SimpleNicRxQueueDesc;

    fn csr_base(&self) -> usize {
        SIMPLE_NIC_RX_RING_BASE
    }
}
impl RingSpecToHost for SimpleNicRxSpec {}

pub(crate) struct SendRingSpec(pub(crate) usize);
impl RingSpec for SendRingSpec {
    const ELEMENT_NUM_EXP: u8 = RING_BUF_LEN_BITS;
    type Element = SendQueueDesc;

    fn csr_base(&self) -> usize {
        QP_SEND_RING_BASES[self.0]
    }
}
impl RingSpecToCard for SendRingSpec {}

pub(crate) struct MetaReportRingSpec(pub(crate) usize);
impl RingSpec for MetaReportRingSpec {
    const ELEMENT_NUM_EXP: u8 = RING_BUF_LEN_BITS;
    type Element = MetaReportQueueDesc;

    fn csr_base(&self) -> usize {
        QP_RECV_RING_BASES[self.0]
    }
}
impl RingSpecToHost for MetaReportRingSpec {}

// ============================================================================
// Public type aliases
// ============================================================================

pub(crate) type CmdReqRingCsr<Dev> = RingCsr<Dev, CmdReqSpec>;
pub(crate) type CmdRespRingCsr<Dev> = RingCsr<Dev, CmdRespSpec>;
pub(crate) type SimpleNicTxRingCsr<Dev> = RingCsr<Dev, SimpleNicTxSpec>;
pub(crate) type SimpleNicRxRingCsr<Dev> = RingCsr<Dev, SimpleNicRxSpec>;
pub(crate) type SendRingCsr<Dev> = RingCsr<Dev, SendRingSpec>;
pub(crate) type MetaReportRingCsr<Dev> = RingCsr<Dev, MetaReportRingSpec>;

// ============================================================================
// Singleton ring constructors
// ============================================================================

/// Create a command request ring
#[inline]
pub(crate) fn cmd_req_ring<Dev: DeviceAdaptor>(dev: Dev) -> CmdReqRingCsr<Dev> {
    RingCsr::new(dev, CmdReqSpec)
}

/// Create a command response ring
#[inline]
pub(crate) fn cmd_resp_ring<Dev: DeviceAdaptor>(dev: Dev) -> CmdRespRingCsr<Dev> {
    RingCsr::new(dev, CmdRespSpec)
}

/// Create a Simple NIC TX ring
#[inline]
pub(crate) fn simple_nic_tx_ring<Dev: DeviceAdaptor>(dev: Dev) -> SimpleNicTxRingCsr<Dev> {
    RingCsr::new(dev, SimpleNicTxSpec)
}

/// Create a Simple NIC RX ring
#[inline]
pub(crate) fn simple_nic_rx_ring<Dev: DeviceAdaptor>(dev: Dev) -> SimpleNicRxRingCsr<Dev> {
    RingCsr::new(dev, SimpleNicRxSpec)
}

// ============================================================================
// Multi-channel ring builders
// ============================================================================

/// Build send rings for all channels based on device mode
pub(crate) fn build_send_rings<Dev: DeviceAdaptor + Clone>(
    dev: Dev,
    mode: Mode,
) -> Vec<SendRingCsr<Dev>> {
    mode.channel_ids()
        .iter()
        .map(|&id| RingCsr::new(dev.clone(), SendRingSpec(id)))
        .collect()
}

/// Build meta report rings for all channels based on device mode
pub(crate) fn build_meta_report_rings<Dev: DeviceAdaptor + Clone>(
    dev: Dev,
    mode: Mode,
) -> Vec<MetaReportRingCsr<Dev>> {
    mode.channel_ids()
        .iter()
        .map(|&id| RingCsr::new(dev.clone(), MetaReportRingSpec(id)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::ring::csr::emulated::EmulatedDevice;
    #[test]
    fn test_build_rings_100g() {
        let dev = EmulatedDevice::new("test").unwrap();

        let send_rings = build_send_rings(dev.clone(), Mode::Mode100G);
        assert_eq!(send_rings.len(), 1);

        let meta_rings = build_meta_report_rings(dev, Mode::Mode100G);
        assert_eq!(meta_rings.len(), 1);
    }

    #[test]
    fn test_build_rings_400g() {
        let dev = EmulatedDevice::new("test").unwrap();

        let send_rings = build_send_rings(dev.clone(), Mode::Mode400G);
        assert_eq!(send_rings.len(), 4);

        let meta_rings = build_meta_report_rings(dev, Mode::Mode400G);
        assert_eq!(meta_rings.len(), 4);
    }
}
