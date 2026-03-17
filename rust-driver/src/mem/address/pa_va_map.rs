/*
 * Physical Address to Virtual Address Bidirectional Mapping
 *
 * This module provides a global mapping table for simulation mode that tracks
 * the relationship between physical addresses (PA) and virtual addresses (VA).
 * This is necessary because the simulator sends memory access requests using
 * physical addresses, but the driver must perform operations on virtual addresses.
 *
 * The mapping table is only compiled and used in simulation mode (feature = "sim").
 */

use core::panic;
use std::collections::BTreeMap;

use crate::types::{PhysAddr, VirtAddr};

/// A range of memory addresses with its corresponding mapping
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct AddressRange {
    /// Starting physical address
    pa_start: PhysAddr,
    /// Ending physical address (exclusive)
    pa_end: PhysAddr,
    /// Starting virtual address
    va_start: VirtAddr,
}

impl AddressRange {
    /// Check if a physical address falls within this range
    fn contains(&self, pa: PhysAddr) -> bool {
        pa >= self.pa_start && pa < self.pa_end
    }

    /// Convert a physical address to virtual address within this range
    fn pa_to_va(&self, pa: PhysAddr) -> Option<VirtAddr> {
        if self.contains(pa) {
            let offset = pa.as_u64() - self.pa_start.as_u64();
            Some(VirtAddr::new(self.va_start.as_u64() + offset))
        } else {
            None
        }
    }

    /// Convert a physical address to virtual address and return remaining length
    ///
    /// # Returns
    ///
    /// `Some((va, remaining_len))` where:
    /// - `va`: Virtual address corresponding to the PA
    /// - `remaining_len`: Bytes remaining from this VA to the end of the range
    fn pa_to_va_with_len(&self, pa: PhysAddr) -> Option<(VirtAddr, usize)> {
        if self.contains(pa) {
            let offset = pa.as_u64() - self.pa_start.as_u64();
            let va = VirtAddr::new(self.va_start.as_u64() + offset);
            let remaining_len = (self.pa_end.as_u64() - pa.as_u64()) as usize;
            Some((va, remaining_len))
        } else {
            None
        }
    }
}

/// Global bidirectional mapping table for PA ↔ VA translation
///
/// Thread-safety must be managed by the caller (typically through Arc<RwLock<PaVaMap>>)
pub(crate) struct PaVaMap {
    /// Mapping from PA ranges to VA ranges
    /// Key: PA start address, Value: AddressRange
    ranges: BTreeMap<PhysAddr, AddressRange>,
    /// Reverse mapping from VA to PA for fast VA→PA lookups
    /// Key: VA start address, Value: PA start address
    va_to_pa: BTreeMap<VirtAddr, PhysAddr>,
}

impl PaVaMap {
    /// Create a new empty mapping table
    pub(crate) fn new() -> Self {
        Self {
            ranges: BTreeMap::new(),
            va_to_pa: BTreeMap::new(),
        }
    }

    /// Insert a new PA ↔ VA mapping
    ///
    /// # Arguments
    ///
    /// * `pa` - Physical address start
    /// * `va` - Virtual address start
    /// * `size` - Size of the region in bytes
    ///
    /// # Panics
    ///
    /// Panics if the region overlaps with an existing mapping
    pub(crate) fn insert(&mut self, pa: PhysAddr, va: VirtAddr, size: usize) {
        let range = AddressRange {
            pa_start: pa,
            pa_end: PhysAddr::new(pa.as_u64() + size as u64),
            va_start: va,
        };

        // Check for overlaps with existing ranges
        for existing_range in self.ranges.values() {
            if range.pa_start < existing_range.pa_end && range.pa_end > existing_range.pa_start {
                panic!(
                    "PA range overlap detected: new [{:#x}, {:#x}) conflicts with existing [{:#x}, {:#x})",
                    range.pa_start.as_u64(), range.pa_end.as_u64(), existing_range.pa_start.as_u64(), existing_range.pa_end.as_u64()
                );
            }
        }

        let _ = self.ranges.insert(pa, range);

        // Insert reverse mapping VA → PA
        let _ = self.va_to_pa.insert(va, pa);

        log::debug!(
            "PA_VA_MAP: Inserted mapping PA [{:#x}, {:#x}) -> VA [{:#x}, {:#x})",
            pa.as_u64(),
            pa.as_u64() + size as u64,
            va.as_u64(),
            va.as_u64() + size as u64
        );
    }

    /// Lookup the virtual address corresponding to a physical address
    ///
    /// # Arguments
    ///
    /// * `pa` - Physical address to look up
    ///
    /// # Returns
    ///
    /// A tuple of `(va, remaining_len)` where:
    /// - `va`: The corresponding virtual address
    /// - `remaining_len`: Bytes remaining from this VA to the end of the mapped range
    ///
    /// Returns `None` if the PA is not found in any mapped range
    pub(crate) fn lookup(&self, pa: PhysAddr) -> Option<(VirtAddr, usize)> {
        // Use BTreeMap's range query to efficiently find the range
        // We look for the largest key that is <= pa
        for (_, range) in self.ranges.range(..=pa).rev() {
            if let Some(result) = range.pa_to_va_with_len(pa) {
                return Some(result);
            }
        }

        log::warn!("PA_VA_MAP: Failed to lookup PA {:#x}", pa.as_u64());
        None
    }

    /// Lookup the physical address corresponding to a virtual address
    ///
    /// # Arguments
    ///
    /// * `va` - Virtual address start to look up (must be exact match)
    ///
    /// # Returns
    ///
    /// The corresponding physical address start, or `None` if not found
    pub(crate) fn lookup_by_va(&self, va: VirtAddr) -> Option<PhysAddr> {
        match self.va_to_pa.get(&va) {
            Some(&pa) => Some(pa),
            None => {
                log::warn!("PA_VA_MAP: Failed to lookup VA {:#x}", va.as_u64());
                None
            }
        }
    }

    /// Remove a PA ↔ VA mapping by physical address
    ///
    /// # Arguments
    ///
    /// * `pa` - Physical address start of the region to remove
    pub(crate) fn remove(&mut self, pa: PhysAddr) {
        if let Some(range) = self.ranges.remove(&pa) {
            let va = range.va_start;

            // Remove reverse mapping VA → PA
            let _ = self.va_to_pa.remove(&va);

            log::debug!(
                "PA_VA_MAP: Removed mapping PA [{:#x}, {:#x}) -> VA [{:#x}, {:#x})",
                range.pa_start.as_u64(),
                range.pa_end.as_u64(),
                range.va_start.as_u64(),
                range.va_start.as_u64() + (range.pa_end.as_u64() - range.pa_start.as_u64())
            );
        } else {
            panic!(
                "PA_VA_MAP: Attempted to remove non-existent mapping at PA {:#x}",
                pa.as_u64()
            );
        }
    }

    /// Remove a PA ↔ VA mapping by virtual address
    ///
    /// # Arguments
    ///
    /// * `va` - Virtual address start of the region to remove
    ///
    /// This is useful for unpinning operations where the VA is known but PA needs to be looked up.
    pub(crate) fn remove_by_va(&mut self, va: VirtAddr) {
        // First lookup PA from VA
        if let Some(pa) = self.va_to_pa.remove(&va) {
            // Remove from main ranges map
            if let Some(range) = self.ranges.remove(&pa) {
                log::debug!(
                    "PA_VA_MAP: Removed mapping VA [{:#x}, {:#x}) -> PA [{:#x}, {:#x})",
                    range.va_start.as_u64(),
                    range.va_start.as_u64() + (range.pa_end.as_u64() - range.pa_start.as_u64()),
                    range.pa_start.as_u64(),
                    range.pa_end.as_u64()
                );
            } else {
                panic!("PA_VA_MAP: Inconsistent state - VA {:#x} mapped to PA {:#x} but PA mapping not found",
                    va.as_u64(), pa.as_u64());
            }
        } else {
            panic!(
                "PA_VA_MAP: Attempted to remove non-existent mapping at VA {:#x}",
                va.as_u64()
            );
        }
    }

    /// Get the number of registered regions
    pub(crate) fn len(&self) -> usize {
        self.ranges.len()
    }

    /// Check if the mapping table is empty
    pub(crate) fn is_empty(&self) -> bool {
        self.ranges.is_empty()
    }

    /// Clear all mappings (primarily for testing)
    #[cfg(test)]
    pub(crate) fn clear(&mut self) {
        self.ranges.clear();
        self.va_to_pa.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_insert_and_lookup() {
        let mut map = PaVaMap::new();

        // Insert a mapping: PA [0x1000, 0x2000) -> VA [0x7000, 0x8000)
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);

        // Lookup addresses within the range
        // At PA 0x1000: VA 0x7000, remaining = 0x2000 - 0x1000 = 0x1000 (4096 bytes)
        assert_eq!(
            map.lookup(PhysAddr::new(0x1000)),
            Some((VirtAddr::new(0x7000), 0x1000))
        );
        // At PA 0x1500: VA 0x7500, remaining = 0x2000 - 0x1500 = 0xb00 (2816 bytes)
        assert_eq!(
            map.lookup(PhysAddr::new(0x1500)),
            Some((VirtAddr::new(0x7500), 0xb00))
        );
        // At PA 0x1fff: VA 0x7fff, remaining = 0x2000 - 0x1fff = 1 byte
        assert_eq!(
            map.lookup(PhysAddr::new(0x1fff)),
            Some((VirtAddr::new(0x7fff), 1))
        );

        // Lookup addresses outside the range
        assert_eq!(map.lookup(PhysAddr::new(0x0fff)), None);
        assert_eq!(map.lookup(PhysAddr::new(0x2000)), None);
    }

    #[test]
    fn test_multiple_ranges() {
        let mut map = PaVaMap::new();

        // Insert multiple non-overlapping ranges
        // Range 1: PA [0x1000, 0x2000) -> VA [0x7000, 0x8000)
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);
        // Range 2: PA [0x3000, 0x5000) -> VA [0x8000, 0xa000)
        map.insert(PhysAddr::new(0x3000), VirtAddr::new(0x8000), 0x2000);
        // Range 3: PA [0x6000, 0x7000) -> VA [0xa000, 0xb000)
        map.insert(PhysAddr::new(0x6000), VirtAddr::new(0xa000), 0x1000);

        // Lookup in first range
        // At PA 0x1500: remaining = 0x2000 - 0x1500 = 0xb00
        assert_eq!(
            map.lookup(PhysAddr::new(0x1500)),
            Some((VirtAddr::new(0x7500), 0xb00))
        );

        // Lookup in second range
        // At PA 0x3500: remaining = 0x5000 - 0x3500 = 0x1b00
        assert_eq!(
            map.lookup(PhysAddr::new(0x3500)),
            Some((VirtAddr::new(0x8500), 0x1b00))
        );
        // At PA 0x4fff: remaining = 0x5000 - 0x4fff = 1
        assert_eq!(
            map.lookup(PhysAddr::new(0x4fff)),
            Some((VirtAddr::new(0x9fff), 1))
        );

        // Lookup in third range
        // At PA 0x6500: remaining = 0x7000 - 0x6500 = 0xb00
        assert_eq!(
            map.lookup(PhysAddr::new(0x6500)),
            Some((VirtAddr::new(0xa500), 0xb00))
        );

        // Lookup in gaps
        assert_eq!(map.lookup(PhysAddr::new(0x2000)), None);
        assert_eq!(map.lookup(PhysAddr::new(0x5000)), None);
        assert_eq!(map.lookup(PhysAddr::new(0x7000)), None);
    }

    #[test]
    fn test_remove() {
        let mut map = PaVaMap::new();

        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);
        assert_eq!(
            map.lookup(PhysAddr::new(0x1500)),
            Some((VirtAddr::new(0x7500), 0xb00))
        );

        map.remove(PhysAddr::new(0x1000));
        assert_eq!(map.lookup(PhysAddr::new(0x1500)), None);
    }

    #[test]
    #[should_panic(expected = "PA range overlap detected")]
    fn test_overlap_detection() {
        let mut map = PaVaMap::new();

        // Insert first range
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x2000);

        // Try to insert overlapping range (should panic)
        map.insert(PhysAddr::new(0x1500), VirtAddr::new(0x8000), 0x1000);
    }

    #[test]
    fn test_adjacent_ranges() {
        let mut map = PaVaMap::new();

        // Insert adjacent (non-overlapping) ranges
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);
        map.insert(PhysAddr::new(0x2000), VirtAddr::new(0x8000), 0x1000);

        // Both ranges should be accessible
        // At PA 0x1fff: last byte of first range, remaining = 1
        assert_eq!(
            map.lookup(PhysAddr::new(0x1fff)),
            Some((VirtAddr::new(0x7fff), 1))
        );
        // At PA 0x2000: first byte of second range, remaining = 0x1000
        assert_eq!(
            map.lookup(PhysAddr::new(0x2000)),
            Some((VirtAddr::new(0x8000), 0x1000))
        );
    }

    #[test]
    fn test_len_and_is_empty() {
        let mut map = PaVaMap::new();

        assert!(map.is_empty());
        assert_eq!(map.len(), 0);

        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);
        assert!(!map.is_empty());
        assert_eq!(map.len(), 1);

        map.insert(PhysAddr::new(0x2000), VirtAddr::new(0x8000), 0x1000);
        assert_eq!(map.len(), 2);

        map.remove(PhysAddr::new(0x1000));
        assert_eq!(map.len(), 1);

        map.remove(PhysAddr::new(0x2000));
        assert!(map.is_empty());
    }

    #[test]
    fn test_lookup_by_va() {
        let mut map = PaVaMap::new();

        // Insert mappings
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);
        map.insert(PhysAddr::new(0x3000), VirtAddr::new(0x8000), 0x2000);

        // Test VA → PA lookups (exact matches only)
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x7000)),
            Some(PhysAddr::new(0x1000))
        );
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x8000)),
            Some(PhysAddr::new(0x3000))
        );

        // Non-existent VA
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x9000)), None);

        // Offset addresses should not match (exact match only)
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x7500)), None);
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x8500)), None);
    }

    #[test]
    fn test_bidirectional_lookup() {
        let mut map = PaVaMap::new();

        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);

        // Test PA → VA (with remaining length)
        assert_eq!(
            map.lookup(PhysAddr::new(0x1000)),
            Some((VirtAddr::new(0x7000), 0x1000))
        );
        assert_eq!(
            map.lookup(PhysAddr::new(0x1500)),
            Some((VirtAddr::new(0x7500), 0xb00))
        );

        // Test VA → PA
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x7000)),
            Some(PhysAddr::new(0x1000))
        );
    }

    #[test]
    fn test_remove_by_va() {
        let mut map = PaVaMap::new();

        // Insert mapping
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);

        // Verify it exists
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x7000)),
            Some(PhysAddr::new(0x1000))
        );
        assert_eq!(
            map.lookup(PhysAddr::new(0x1000)),
            Some((VirtAddr::new(0x7000), 0x1000))
        );

        // Remove by VA
        map.remove_by_va(VirtAddr::new(0x7000));

        // Verify both directions are removed
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x7000)), None);
        assert_eq!(map.lookup(PhysAddr::new(0x1000)), None);
        assert!(map.is_empty());
    }

    #[test]
    fn test_remove_maintains_both_indices() {
        let mut map = PaVaMap::new();

        // Insert mapping
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);

        // Verify it exists
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x7000)),
            Some(PhysAddr::new(0x1000))
        );
        assert_eq!(
            map.lookup(PhysAddr::new(0x1000)),
            Some((VirtAddr::new(0x7000), 0x1000))
        );

        // Remove by PA (original method)
        map.remove(PhysAddr::new(0x1000));

        // Verify both directions are removed
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x7000)), None);
        assert_eq!(map.lookup(PhysAddr::new(0x1000)), None);
        assert!(map.is_empty());
    }

    #[test]
    fn test_bidirectional_with_multiple_ranges() {
        let mut map = PaVaMap::new();

        // Insert multiple ranges
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);
        map.insert(PhysAddr::new(0x3000), VirtAddr::new(0x8000), 0x2000);
        map.insert(PhysAddr::new(0x6000), VirtAddr::new(0xa000), 0x1000);

        // Test all VA → PA lookups
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x7000)),
            Some(PhysAddr::new(0x1000))
        );
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x8000)),
            Some(PhysAddr::new(0x3000))
        );
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0xa000)),
            Some(PhysAddr::new(0x6000))
        );

        // Test all PA → VA lookups (with remaining length)
        assert_eq!(
            map.lookup(PhysAddr::new(0x1000)),
            Some((VirtAddr::new(0x7000), 0x1000))
        );
        assert_eq!(
            map.lookup(PhysAddr::new(0x3000)),
            Some((VirtAddr::new(0x8000), 0x2000))
        );
        assert_eq!(
            map.lookup(PhysAddr::new(0x6000)),
            Some((VirtAddr::new(0xa000), 0x1000))
        );

        // Remove middle range by VA
        map.remove_by_va(VirtAddr::new(0x8000));

        // Verify only that range is removed
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0x7000)),
            Some(PhysAddr::new(0x1000))
        );
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x8000)), None);
        assert_eq!(
            map.lookup_by_va(VirtAddr::new(0xa000)),
            Some(PhysAddr::new(0x6000))
        );
        assert_eq!(map.len(), 2);
    }

    #[test]
    fn test_clear_removes_both_indices() {
        let mut map = PaVaMap::new();

        // Insert multiple mappings
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);
        map.insert(PhysAddr::new(0x2000), VirtAddr::new(0x8000), 0x1000);

        assert_eq!(map.len(), 2);

        // Clear all
        map.clear();

        // Verify both indices are empty
        assert!(map.is_empty());
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x7000)), None);
        assert_eq!(map.lookup_by_va(VirtAddr::new(0x8000)), None);
        assert_eq!(map.lookup(PhysAddr::new(0x1000)), None);
        assert_eq!(map.lookup(PhysAddr::new(0x2000)), None);
    }

    #[test]
    fn test_remaining_length_calculation() {
        let mut map = PaVaMap::new();

        // Insert a 4KB range: PA [0x1000, 0x2000) -> VA [0x7000, 0x8000)
        map.insert(PhysAddr::new(0x1000), VirtAddr::new(0x7000), 0x1000);

        // Test remaining length at different offsets
        assert_eq!(
            map.lookup(PhysAddr::new(0x1000)),
            Some((VirtAddr::new(0x7000), 0x1000))
        ); // 4096 bytes remaining
        assert_eq!(
            map.lookup(PhysAddr::new(0x1001)),
            Some((VirtAddr::new(0x7001), 0xfff))
        ); // 4095 bytes remaining
        assert_eq!(
            map.lookup(PhysAddr::new(0x1800)),
            Some((VirtAddr::new(0x7800), 0x800))
        ); // 2048 bytes remaining
        assert_eq!(
            map.lookup(PhysAddr::new(0x1fff)),
            Some((VirtAddr::new(0x7fff), 1))
        ); // 1 byte remaining

        // Insert a larger range to test: PA [0x10000, 0x20000) -> VA [0x50000, 0x60000)
        map.insert(PhysAddr::new(0x10000), VirtAddr::new(0x50000), 0x10000);

        // Test various positions in the 64KB range
        assert_eq!(
            map.lookup(PhysAddr::new(0x10000)),
            Some((VirtAddr::new(0x50000), 0x10000))
        ); // 65536 bytes
        assert_eq!(
            map.lookup(PhysAddr::new(0x18000)),
            Some((VirtAddr::new(0x58000), 0x8000))
        ); // 32768 bytes
        assert_eq!(
            map.lookup(PhysAddr::new(0x1ffff)),
            Some((VirtAddr::new(0x5ffff), 1))
        ); // 1 byte
    }
}
