//! Memory utility functions.

use crate::mem::PAGE_SIZE;

/// Calculates the number of pages spanned by a memory region.
///
/// # Arguments
///
/// * `addr` - Starting address of the memory region
/// * `length` - Length of the memory region in bytes
///
/// # Returns
///
/// The number of pages that the memory region spans, accounting for
/// page boundary crossings.
#[allow(clippy::arithmetic_side_effects)]
pub(crate) fn get_num_page(addr: u64, length: usize) -> usize {
    if length == 0 {
        return 0;
    }
    let last = addr.saturating_add(length as u64).saturating_sub(1);
    let start_page = addr / PAGE_SIZE as u64;
    let end_page = last / PAGE_SIZE as u64;
    (end_page.saturating_sub(start_page) + 1) as usize
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_get_num_page_zero_length() {
        assert_eq!(get_num_page(0, 0), 0);
        assert_eq!(get_num_page(100, 0), 0);
    }

    #[test]
    fn test_get_num_page_single_page_aligned() {
        assert_eq!(get_num_page(0, PAGE_SIZE), 1);
        assert_eq!(get_num_page(PAGE_SIZE as u64, PAGE_SIZE), 1);
    }

    #[test]
    fn test_get_num_page_single_page_unaligned() {
        assert_eq!(get_num_page(1, 1), 1);
        assert_eq!(get_num_page(1, PAGE_SIZE - 1), 1);
        assert_eq!(get_num_page(1, PAGE_SIZE), 2);
        assert_eq!(get_num_page(PAGE_SIZE as u64 - 1, 1), 1);
        assert_eq!(get_num_page(PAGE_SIZE as u64 - 1, 2), 2);
    }

    #[test]
    fn test_get_num_page_multiple_pages_aligned() {
        assert_eq!(get_num_page(0, 2 * PAGE_SIZE), 2);
        assert_eq!(get_num_page(PAGE_SIZE as u64, 3 * PAGE_SIZE), 3);
    }

    #[test]
    fn test_get_num_page_multiple_pages_unaligned() {
        assert_eq!(get_num_page(1, 2 * PAGE_SIZE - 1), 2);
        assert_eq!(get_num_page(1, 2 * PAGE_SIZE), 3);
        assert_eq!(get_num_page(PAGE_SIZE as u64 + 1, PAGE_SIZE + 1), 2);
    }

    #[test]
    fn test_get_num_page_large_values() {
        let large_addr = 100 * PAGE_SIZE as u64;
        let large_length = 50 * PAGE_SIZE;
        assert_eq!(get_num_page(large_addr, large_length), 50);

        let large_length_unaligned = 50 * PAGE_SIZE + 1;
        assert_eq!(get_num_page(large_addr, large_length_unaligned), 51);

        let large_addr_unaligned = 100 * PAGE_SIZE as u64 + 1;
        assert_eq!(get_num_page(large_addr_unaligned, large_length), 51);
    }

    #[test]
    fn test_get_num_page_max_values() {
        let max_usize_len = usize::MAX;
        let max_u64_addr = u64::MAX - max_usize_len as u64;

        // Case 1: Length causes saturation, but still within a few pages
        let addr_near_max = u64::MAX - (PAGE_SIZE as u64 * 2);
        let len_causes_saturation = PAGE_SIZE * 3;
        assert_eq!(get_num_page(addr_near_max, len_causes_saturation), 3);

        // Case 2: Length is very large, causing saturation
        let addr_small = 0;
        let len_max = usize::MAX;
        assert_eq!(
            get_num_page(addr_small, len_max),
            (u64::MAX / PAGE_SIZE as u64 + 1) as usize
        );

        // Case 3: addr is very large, length is small
        let addr_max = u64::MAX - 10;
        let len_small = 20;
        let expected_start_page = (u64::MAX - 10) / PAGE_SIZE as u64;
        let expected_end_page = u64::MAX / PAGE_SIZE as u64;
        assert_eq!(
            get_num_page(addr_max, len_small),
            (expected_end_page.saturating_sub(expected_start_page) + 1) as usize
        );
    }
}
