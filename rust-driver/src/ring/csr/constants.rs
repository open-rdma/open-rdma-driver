//! CSR Memory Map Constants
//!
//! This module defines the memory-mapped I/O address layout for all Control and Status
//! Registers (CSRs) in the RDMA device.
//!
//! # Address Space Layout
//!
//! The CSR space is organized into logical blocks:
//!
//! ```text
//! Block         | Base Offset | Purpose
//! --------------|-------------|----------------------------------
//! QP (0x00)     | 0x000       | Queue Pair send/recv rings (4x)
//! CmdQ (0x40)   | 0x100       | Command request/response rings
//! SimpleNic (0x50)| 0x140     | Raw packet TX/RX rings
//! ```
//!
//! # Ring Register Layout
//!
//! Each ring occupies a 16-byte aligned region with standard offsets:
//!
//! ```text
//! Offset | Register     | Access | Description
//! -------|--------------|--------|----------------------------
//! +0x00  | BASE_LOW     | RW     | Ring buffer physical address [31:0]
//! +0x04  | BASE_HIGH    | RW     | Ring buffer physical address [63:32]
//! +0x08  | HEAD         | RW     | Producer pointer (write index)
//! +0x0C  | TAIL         | RW     | Consumer pointer (read index)
//! ```
//!
//! # Direction Encoding
//!
//! Within each block, rings are organized by direction:
//! - **ToCard (Host → Card)**: Even queue indices (offset +0x00)
//! - **ToHost (Card → Host)**: Odd queue indices (offset +0x10)
//!
//! For example, in the QP block:
//! - QP0 Send Ring: 0x000 (ToCard)
//! - QP0 Recv Ring: 0x010 (ToHost)
//! - QP1 Send Ring: 0x040 (ToCard)
//! - QP1 Recv Ring: 0x050 (ToHost)
//!
//! # Address Calculation
//!
//! All addresses are calculated using `generate_csr_addr_base()`:
//! ```text
//! addr = (block_base + queue_idx * 0x10 + direction_offset) << 2
//! ```
//!
//! The `<< 2` shifts converts from 32-bit word addresses to byte addresses.
//!
//! # Constants
//!
//! ## Ring Base Addresses
//! - `CMD_REQ_RING_BASE` / `CMD_RESP_RING_BASE` - Command rings
//! - `SIMPLE_NIC_TX_RING_BASE` / `SIMPLE_NIC_RX_RING_BASE` - Raw packet rings
//! - `QP_SEND_RING_BASES[4]` / `QP_RECV_RING_BASES[4]` - RDMA send/recv rings
//!
//! ## Register Offsets
//! - `RING_OFFSET_BASE_LOW` / `RING_OFFSET_BASE_HIGH` - Physical address registers
//! - `RING_OFFSET_HEAD` / `RING_OFFSET_TAIL` - Producer/consumer pointers

#[derive(Clone, Copy)]
enum BlockStart {
    Qp = 0x00,
    CmdQ = 0x40,
    SimpleNic = 0x50,
}

#[allow(clippy::arithmetic_side_effects)]
const fn generate_csr_addr_base(start: BlockStart, queue_index: usize, is_to_host: bool) -> usize {
    let base = start as usize + queue_index * 0x10 + if is_to_host { 4 } else { 0 };
    base << 2
}

macro_rules! generate_qp_array_start {
    ($is_to_host:expr) => {
        [
            generate_csr_addr_base(BlockStart::Qp, 0, $is_to_host),
            generate_csr_addr_base(BlockStart::Qp, 1, $is_to_host),
            generate_csr_addr_base(BlockStart::Qp, 2, $is_to_host),
            generate_csr_addr_base(BlockStart::Qp, 3, $is_to_host),
        ]
    };
}

pub(crate) const NUM_QPS: usize = 4;

// Offsets within a ring (byte address)
pub(crate) const RING_OFFSET_BASE_LOW: usize = 0x00;
pub(crate) const RING_OFFSET_BASE_HIGH: usize = 0x04;
pub(crate) const RING_OFFSET_HEAD: usize = 0x08;
pub(crate) const RING_OFFSET_TAIL: usize = 0x0C;

// Ring base addresses
pub(crate) const CMD_REQ_RING_BASE: usize = generate_csr_addr_base(BlockStart::CmdQ, 0, false);
pub(crate) const CMD_RESP_RING_BASE: usize = generate_csr_addr_base(BlockStart::CmdQ, 0, true);
pub(crate) const SIMPLE_NIC_TX_RING_BASE: usize =
    generate_csr_addr_base(BlockStart::SimpleNic, 0, false);
pub(crate) const SIMPLE_NIC_RX_RING_BASE: usize =
    generate_csr_addr_base(BlockStart::SimpleNic, 0, true);

pub(crate) const QP_SEND_RING_BASES: [usize; NUM_QPS] = generate_qp_array_start!(false);
pub(crate) const QP_RECV_RING_BASES: [usize; NUM_QPS] = generate_qp_array_start!(true);

pub(super) const CSR_DEVICE_MODE_ADDR: usize = 0;

/// Number of bits used to represent the length of the ring buffer.
pub(crate) const RING_BUF_LEN_BITS: u8 = 12;
/// Highest bit of the ring buffer
pub(crate) const RING_BUF_LEN: usize = 1 << RING_BUF_LEN_BITS;
