use crate::{mem::PAGE_SIZE, types::VirtAddr};

pub(crate) fn check_addr_is_anon_hugepage(addr: VirtAddr, length: usize) -> bool {
    use pagemap::PageMap;

    let pid = std::process::id() as u64;
    println!("[ADDR_DEBUG] check_addr_is_anon_hugepage: pid={pid}");
    // Instantiate a new pagemap::PageMap.
    let pm = PageMap::new(pid).unwrap();
    log::debug!(
        "[ADDR_DEBUG] check_addr_is_anon_hugepage: pid={}, addr={addr:x}, length={length}",
        pid,
    );

    // use std::fs::File;
    // let mut buf = String::new();

    // let _ = File::open("/proc/self/smaps")
    //     .unwrap()
    //     .read_to_string(&mut buf)
    //     .unwrap();

    // println!("[ADDR_DEBUG] /proc/self/smaps line: \n {buf}");

    let smaps = pm.smaps().unwrap();
    let va_region = smaps
        .iter()
        .find(|a| a.maps_entry().vma().contains(addr.as_u64()))
        .unwrap();

    if addr.as_u64() + length as u64 > va_region.maps_entry().vma().last_address() + 1 {
        panic!("mapping length exceeds region");
    }

    va_region.kernel_page_size() == PAGE_SIZE as u64
        && va_region.mmu_page_size() == PAGE_SIZE as u64
}

#[test]
fn test_check_addr_is_anon_hugepage() {
    use std::ptr;

    let len = 1024 * 1024 * 4; // 4MB

    //TODO need to move unsafe code to a separate function
    #[allow(unsafe_code)]
    let ptr = unsafe {
        libc::mmap(
            ptr::null_mut(),
            len,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_SHARED | libc::MAP_ANON | libc::MAP_HUGETLB | libc::MAP_HUGE_2MB,
            -1,
            0,
        )
    };

    if ptr == libc::MAP_FAILED {
        panic!("mmap failed");
    }
    let addr = VirtAddr::new(ptr as u64);
    assert!(check_addr_is_anon_hugepage(addr, len));
}
