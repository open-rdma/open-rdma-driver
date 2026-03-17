//! Experimental and unused code.
//!
//! This module contains code that is not currently used in production but is
//! kept for potential future use or reference:
//!
//! - `page_allocator`: The `PageAllocator<N>` trait and `ContiguousPages<N>` struct
//!   for allocating contiguous physical pages.
//! - `dmabuf`: An alternative udmabuf implementation using memfd.
//! - `host_old`: An older implementation of host page allocation.
//!
//! These modules are preserved with `#[allow(dead_code)]` to suppress warnings.

#[allow(dead_code)]
pub(crate) mod dmabuf;
#[allow(dead_code)]
pub(crate) mod host_old;
#[allow(dead_code)]
pub(crate) mod page_allocator;
