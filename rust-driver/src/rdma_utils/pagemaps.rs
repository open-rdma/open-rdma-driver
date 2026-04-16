use pagemap::VirtualMemoryArea;

use crate::{
    mem::PAGE_SIZE,
    types::{PhysAddr, VirtAddr},
};

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

pub(crate) fn translate_va_to_pa(addr: VirtAddr) -> Option<PhysAddr> {
    use pagemap::PageMap;

    let pid = std::process::id() as u64;
    println!("[ADDR_DEBUG] translate_va_to_pa: pid={pid}");
    // Instantiate a new pagemap::PageMap.
    let mut pm = PageMap::new(pid).unwrap();
    log::debug!(
        "[ADDR_DEBUG] translate_va_to_pa: pid={}, addr={addr:x}",
        pid,
    );

    let smaps = pm.smaps().unwrap();
    let smap_region = smaps
        .iter()
        .find(|a| a.maps_entry().vma().contains(addr.as_u64()))?;

    let page_size = pagemap::page_size().unwrap();

    let start = addr.as_u64() & !(page_size - 1);
    let end = addr.as_u64() + page_size;
    let offset = addr.as_u64() - start;

    let result = pm
        .pagemap_vma(&VirtualMemoryArea::from((start, end)))
        .unwrap();

    let pfn = result.first().unwrap().pfn().unwrap();
    log::debug!("[ADDR_DEBUG] translate_va_to_pa: result={result:?},pfn={pfn:?},offset={offset:?},page_size={page_size:?}");
    Some(PhysAddr::new(pfn * page_size + offset))
}

#[test]
fn test_translate_va_to_pa() {
    use std::ptr;

    let len = 1024 * 1024 * 4; // 4MB
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

    #[allow(unsafe_code)]
    unsafe {
        ptr::write_bytes(ptr, 0xab, len);
    }

    let addr = VirtAddr::new(ptr as u64 + 1);

    let result = translate_va_to_pa(addr);
    print!("[ADDR_DEBUG] test_translate_va_to_pa: VA={addr:x}, PA={result:?}");
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
