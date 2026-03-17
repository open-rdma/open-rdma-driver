//! User memory handler implementations.
//!
//! Provides two implementations:
//! - `HostUmemHandler`: Real hardware environment
//! - `EmulatedUmemHandler`: Simulation environment

mod emulated;
mod host;

pub(crate) use emulated::EmulatedUmemHandler;
pub(crate) use host::HostUmemHandler;

use std::io;

use crate::types::VirtAddr;

use super::address::AddressResolver;

/// Trait for pinning/unpinning memory pages.
pub(crate) trait MemoryPinner {
    /// Pins pages in memory to prevent swapping.
    ///
    /// # Errors
    ///
    /// Returns an error if the pages could not be locked in memory.
    fn pin_pages(&self, addr: VirtAddr, length: usize) -> io::Result<()>;

    /// Unpins pages.
    ///
    /// # Errors
    ///
    /// Returns an error if the pages could not be unlocked.
    fn unpin_pages(&self, addr: VirtAddr, length: usize) -> io::Result<()>;
}

/// Combined trait for user memory handling.
///
/// Combines address resolution and memory pinning capabilities.
pub(crate) trait UmemHandler: AddressResolver + MemoryPinner {}
