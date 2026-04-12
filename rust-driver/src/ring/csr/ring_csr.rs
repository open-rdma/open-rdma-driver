//! Device Adaptor and Ring Buffer Abstractions
//!
//! This module provides the core abstractions for accessing Control and Status Registers (CSRs)
//! through different hardware backends and managing ring buffer control structures.
//!
//! # Key Components
//!
//! ## DeviceAdaptor Trait
//! Low-level CSR read/write interface implemented by hardware backends:
//! - `SysfsPciCsrAdaptor` - Direct PCIe MMIO access via sysfs
//! - `VfioPciCsrAdaptor` - PCIe access through VFIO framework
//! - `EmulatedDevice` - UDP RPC communication with RTL simulator
//!
//! ## RingSpec Traits
//! Compile-time ring buffer specifications:
//! - `RingSpec` - Base trait providing CSR base address
//! - `RingSpecToCard` - Marker for host-to-card rings (host produces, card consumes)
//! - `RingSpecToHost` - Marker for card-to-host rings (card produces, host consumes)
//!
//! ## Ring<Dev, Spec>
//! Generic ring buffer control structure parameterized by:
//! - `Dev`: Device adaptor implementation (hardware/emulated)
//! - `Spec`: Ring specification (defines CSR base address and direction)
//!
//! Provides operations for:
//! - Reading/writing 64-bit ring buffer base physical address
//! - Direction-specific head/tail pointer operations via `WriterOps`/`ReaderOps`
//!
//! ## Direction Safety
//! The type system enforces correct operations at compile time:
//! - `WriterOps` - Only available for `RingSpecToCard` rings (write head, read tail)
//! - `ReaderOps` - Only available for `RingSpecToHost` rings (read head, write tail)
//!
//! This prevents programming errors like updating the head pointer on a receive ring.
//!
//! # Example
//!
//! ```rust,ignore
//! use crate::csr::{EmulatedDevice, SendRing, SendRingSpec, WriterOps};
//!
//! // Create device adaptor
//! let dev = EmulatedDevice::new("uverbs0")?;
//!
//! // Create send ring (ToCard direction)
//! let send_ring: SendRing<_> = Ring::new(dev, SendRingSpec(0));
//!
//! // Use writer operations (compile-time enforced)
//! send_ring.write_head(32)?;  // ✅ OK - ToCard rings implement WriterOps
//! let tail = send_ring.read_tail()?;  // ✅ OK - read consumer progress
//! // send_ring.write_tail(10)?;  // ❌ Compile error - no ReaderOps for ToCard
//! ```

use std::io;

use super::constants::{
    RING_OFFSET_BASE_HIGH, RING_OFFSET_BASE_LOW, RING_OFFSET_HEAD, RING_OFFSET_TAIL,
};

use crate::ring::traits::{
    DeviceAdaptor, FromRingBytes, RingSpec, RingSpecToCard, RingSpecToHost, ToRingBytes,
};

pub(crate) struct RingCsr<Dev, Spec>
where
    Dev: DeviceAdaptor,
    Spec: RingSpec,
{
    dev: Dev,
    spec: Spec,
}

impl<Dev: DeviceAdaptor, Spec: RingSpec> RingCsr<Dev, Spec> {
    /// Create a new ring with the given device and specification
    #[inline]
    pub(crate) fn new(dev: Dev, spec: Spec) -> Self {
        Self { dev, spec }
    }

    /// Read the base physical address of the ring buffer (64-bit)
    pub(crate) fn read_base_addr(&self) -> io::Result<u64> {
        let lo = self
            .dev
            .read_csr(self.spec.csr_base() + RING_OFFSET_BASE_LOW)?;
        let hi = self
            .dev
            .read_csr(self.spec.csr_base() + RING_OFFSET_BASE_HIGH)?;
        Ok(u64::from(lo) | (u64::from(hi) << 32))
    }

    /// Write the base physical address of the ring buffer (64-bit)
    pub(crate) fn write_base_addr(&self, phys_addr: crate::types::PhysAddr) -> io::Result<()> {
        let (lo, hi) = phys_addr.split();
        self.dev
            .write_csr(self.spec.csr_base() + RING_OFFSET_BASE_LOW, lo)?;
        self.dev
            .write_csr(self.spec.csr_base() + RING_OFFSET_BASE_HIGH, hi)?;
        Ok(())
    }
}

/// Operations for rings where the host writes (produces data to card)
pub(crate) trait WriterOps {
    /// Write the head pointer (producer index)
    fn write_head(&self, head: u32) -> io::Result<()>;
    /// Read the tail pointer (consumer index)
    fn read_tail(&self) -> io::Result<u32>;
}

/// Operations for rings where the host reads (consumes data from card)
pub(crate) trait ReaderOps {
    /// Read the head pointer (producer index)
    fn read_head(&self) -> io::Result<u32>;
    /// Write the tail pointer (consumer index)
    fn write_tail(&self, tail: u32) -> io::Result<()>;
}

/// Implement writer operations for ToCard rings (host produces, card consumes)
impl<Dev, Spec> WriterOps for RingCsr<Dev, Spec>
where
    Dev: DeviceAdaptor,
    Spec: RingSpecToCard,
    <Spec as RingSpec>::Element: ToRingBytes,
{
    #[inline]
    fn write_head(&self, head: u32) -> io::Result<()> {
        self.dev
            .write_csr(self.spec.csr_base() + RING_OFFSET_HEAD, head)
    }

    #[inline]
    fn read_tail(&self) -> io::Result<u32> {
        self.dev.read_csr(self.spec.csr_base() + RING_OFFSET_TAIL)
    }
}

/// Implement reader operations for ToHost rings (card produces, host consumes)
impl<Dev, Spec> ReaderOps for RingCsr<Dev, Spec>
where
    Dev: DeviceAdaptor,
    Spec: RingSpecToHost,
    <Spec as RingSpec>::Element: FromRingBytes,
{
    #[inline]
    fn read_head(&self) -> io::Result<u32> {
        self.dev.read_csr(self.spec.csr_base() + RING_OFFSET_HEAD)
    }

    #[inline]
    fn write_tail(&self, tail: u32) -> io::Result<()> {
        self.dev
            .write_csr(self.spec.csr_base() + RING_OFFSET_TAIL, tail)
    }
}
