//! Address translation and mapping utilities.

mod pa_va_map;
mod resolver;

pub(crate) use pa_va_map::PaVaMap;
pub(crate) use resolver::{AddressResolver, PhysAddrResolverLinuxX86};
