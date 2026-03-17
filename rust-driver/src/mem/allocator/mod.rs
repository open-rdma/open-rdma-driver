//! Memory allocators for DMA buffers.

mod emulated;
pub(crate) mod udmabuf;

pub(crate) use emulated::EmulatedPageAllocator;
pub(crate) use udmabuf::UDmaBufAllocator;
