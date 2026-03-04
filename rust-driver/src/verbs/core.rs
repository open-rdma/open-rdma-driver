use crate::rdma_utils::types::ibv_qp_attr::{IbvQpAttr, IbvQpInitAttr};
use crate::rdma_utils::types::{RecvWr, SendWr};
use crate::RdmaCtxOps;
use crate::{
    config::{ConfigLoader, DeviceConfig},
    workers::{completion::Completion, qp_timeout::AckTimeoutConfig},
};
use log::{debug, error};
use std::ptr;
use std::sync::OnceLock;

use super::dev::{EmulatedHwDevice, PciHwDevice};
use super::ffi::get_device;
use super::{
    ctx::{HwDeviceCtx, VerbsOps},
    mock::MockDeviceCtx,
};

use crate::error::{RdmaError, Result};

macro_rules! deref_or_ret {
    ($ptr:expr, $ret:expr) => {
        match unsafe { $ptr.as_mut() } {
            Some(val) => *val,
            None => return $ret,
        }
    };
}

#[allow(
    missing_debug_implementations,
    missing_copy_implementations,
    clippy::exhaustive_structs
)]
pub struct BlueRdmaCore;

impl BlueRdmaCore {
    fn check_logger_inited() {
        assert!(env_logger::try_init().is_err(), "global logger init failed");
    }

    #[allow(clippy::unwrap_used, clippy::unwrap_in_result)]
    pub(super) fn new_hw(sysfs_name: &str) -> Result<HwDeviceCtx<PciHwDevice>> {
        Self::check_logger_inited();
        debug!("before load default");
        let config = ConfigLoader::load_default()?;
        debug!("before open default");
        let device = PciHwDevice::open_default()?;

        debug!("before reset device");
        device.reset()?;

        #[cfg(feature = "debug_csrs")]
        device.set_custom()?;

        debug!("before initialize HwDeviceCtx");
        let ctx = HwDeviceCtx::initialize(device, config, sysfs_name.to_string())?;
        Ok(ctx)
    }

    #[allow(clippy::unwrap_used, clippy::unwrap_in_result)]
    pub(super) fn new_emulated(sysfs_name: &str) -> Result<HwDeviceCtx<EmulatedHwDevice>> {
        log::info!("initializing emulated device with sysfs name: {sysfs_name}");
        let rank_offset = sysfs_name
            .chars()
            .last()
            .and_then(|c| c.to_digit(10))
            .ok_or_else(|| {
                RdmaError::InvalidInput(format!(
                    "sysfs_name must end with a digit, got: {sysfs_name}"
                ))
            })? as u16;

        let csr_addr = format!("127.0.0.1:{}", 7701u16 + rank_offset);
        let post_recv_addr = format!("127.0.0.1:{}", 7003u16 + rank_offset);
        let device = EmulatedHwDevice::new(csr_addr.into(), post_recv_addr.into());

        let ack = AckTimeoutConfig::new(16, 40, 2);
        let config = DeviceConfig { ack };
        // (check_duration, local_ack_timeout) : (256ms, 1s) because emulator is slow
        HwDeviceCtx::initialize(device, config, sysfs_name.to_string())
    }

    #[allow(clippy::unnecessary_wraps)]
    pub(super) fn new_mock(sysfs_name: &str) -> Result<MockDeviceCtx> {
        Ok(MockDeviceCtx::default())
    }
}

#[allow(unsafe_code)]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
#[allow(clippy::as_conversions, clippy::cast_possible_truncation)]
unsafe impl RdmaCtxOps for BlueRdmaCore {
    #[inline]
    fn init() {}

    #[inline]
    fn new(sysfs_name: *const std::ffi::c_char) -> *mut std::ffi::c_void {
        let name = unsafe {
            std::ffi::CStr::from_ptr(sysfs_name)
                .to_string_lossy()
                .into_owned()
        };
        debug!("before create once_lock ctx for device: {}", name);

        #[cfg(feature = "hw")]
        {
            let once_ctx: OnceLock<parking_lot::Mutex<HwDeviceCtx<PciHwDevice>>> = OnceLock::new();
            let ptr = Box::into_raw(Box::new(once_ctx)).cast();
            log::info!("create hw ptr is:{:?},at pid: {}", ptr, std::process::id());
            ptr
        }

        #[cfg(feature = "sim")]
        {
            let once_ctx: OnceLock<parking_lot::Mutex<HwDeviceCtx<EmulatedHwDevice>>> =
                OnceLock::new();
            let ptr = Box::into_raw(Box::new(once_ctx)).cast();
            log::info!("create sim ptr is:{:?},at pid: {}", ptr, std::process::id());
            ptr
        }

        #[cfg(feature = "mock")]
        {
            let once_ctx: OnceLock<parking_lot::Mutex<MockDeviceCtx>> = OnceLock::new();
            let ptr = Box::into_raw(Box::new(once_ctx)).cast();
            log::info!(
                "create mock ptr is:{:?},at pid: {}",
                ptr,
                std::process::id()
            );
            ptr
        }
    }

    #[inline]
    fn free(driver_data: *const std::ffi::c_void) {
        if driver_data.is_null() {
            error!("Failed to free driver data");
        } else {
            unsafe {
                drop(Box::from_raw(
                    driver_data as *mut HwDeviceCtx<EmulatedHwDevice>,
                ));
            }
        }
    }

    #[inline]
    fn alloc_pd(blue_context: *mut ibverbs_sys::ibv_context) -> *mut ibverbs_sys::ibv_pd {
        let mut bluerdma = get_device(blue_context);

        match bluerdma.alloc_pd() {
            Ok(handle) => Box::into_raw(Box::new(ibverbs_sys::ibv_pd {
                context: blue_context,
                handle,
            })),
            Err(err) => {
                error!("Failed to alloc PD: {err}");
                ptr::null_mut()
            }
        }
    }

    #[inline]
    fn dealloc_pd(pd: *mut ibverbs_sys::ibv_pd) -> ::std::os::raw::c_int {
        let pd = deref_or_ret!(pd, libc::EINVAL);
        let mut bluerdma = get_device(pd.context);

        match bluerdma.dealloc_pd(pd.handle) {
            Ok(()) => 0,
            Err(err) => {
                error!("failed to dealloc PD");
                err.to_errno()
            }
        }
    }

    #[inline]
    fn query_device_ex(
        _blue_context: *mut ibverbs_sys::ibv_context,
        _input: *const ibverbs_sys::ibv_query_device_ex_input,
        device_attr: *mut ibverbs_sys::ibv_device_attr,
        _attr_size: usize,
    ) -> ::std::os::raw::c_int {
        unsafe {
            (*device_attr) = ibverbs_sys::ibv_device_attr {
                max_qp: 256,
                max_qp_wr: 64,
                max_sge: 1,
                max_sge_rd: 1,
                max_cq: 256,
                max_cqe: 4096,
                max_mr: 256,
                max_pd: 256,
                phys_port_cnt: 1,
                ..Default::default()
            };
        }
        0
    }

    #[inline]
    fn query_port(
        _blue_context: *mut ibverbs_sys::ibv_context,
        _port_num: u8,
        port_attr: *mut ibverbs_sys::ibv_port_attr,
    ) -> ::std::os::raw::c_int {
        unsafe {
            (*port_attr) = ibverbs_sys::ibv_port_attr {
                state: ibverbs_sys::ibv_port_state::IBV_PORT_ACTIVE,
                max_mtu: ibverbs_sys::IBV_MTU_4096,
                active_mtu: ibverbs_sys::IBV_MTU_4096,
                gid_tbl_len: 256,
                port_cap_flags: 0x0000_2c00,
                max_msg_sz: 1 << 31,
                lid: 1,
                link_layer: ibverbs_sys::IBV_LINK_LAYER_ETHERNET as u8,
                ..Default::default()
            };
        }
        0
    }

    #[inline]
    fn create_cq(
        blue_context: *mut ibverbs_sys::ibv_context,
        cqe: core::ffi::c_int,
        channel: *mut ibverbs_sys::ibv_comp_channel,
        comp_vector: core::ffi::c_int,
    ) -> *mut ibverbs_sys::ibv_cq {
        let mut bluerdma = get_device(blue_context);
        match bluerdma.create_cq() {
            Ok(handle) => {
                let cq = ibverbs_sys::ibv_cq {
                    context: blue_context,
                    channel,
                    cq_context: ptr::null_mut(),
                    handle,
                    cqe,
                    mutex: ibverbs_sys::pthread_mutex_t::default(),
                    cond: ibverbs_sys::pthread_cond_t::default(),
                    comp_events_completed: 0,
                    async_events_completed: 0,
                };
                Box::into_raw(Box::new(cq))
            }
            Err(err) => {
                error!("Failed to create cq");
                ptr::null_mut()
            }
        }
    }

    #[inline]
    fn destroy_cq(cq: *mut ibverbs_sys::ibv_cq) -> ::std::os::raw::c_int {
        let cq = deref_or_ret!(cq, libc::EINVAL);
        let mut bluerdma = get_device(cq.context);

        match bluerdma.destroy_cq(cq.handle) {
            Ok(()) => 0,
            Err(err) => {
                error!("Failed to destroy CQ: {}", cq.handle);
                err.to_errno()
            }
        }
    }

    #[inline]
    fn create_qp(
        pd: *mut ibverbs_sys::ibv_pd,
        init_attr: *mut ibverbs_sys::ibv_qp_init_attr,
    ) -> *mut ibverbs_sys::ibv_qp {
        let context = deref_or_ret!(pd, ptr::null_mut()).context;
        let mut bluerdma = get_device(context);
        let init_attr = deref_or_ret!(init_attr, ptr::null_mut());
        match bluerdma.create_qp(IbvQpInitAttr::new(init_attr)) {
            Ok(qpn) => Box::into_raw(Box::new(ibverbs_sys::ibv_qp {
                context,
                qp_context: ptr::null_mut(),
                pd,
                send_cq: ptr::null_mut(),
                recv_cq: ptr::null_mut(),
                srq: ptr::null_mut(),
                handle: 0,
                qp_num: qpn,
                state: ibverbs_sys::ibv_qp_state::IBV_QPS_INIT,
                qp_type: init_attr.qp_type,
                mutex: ibverbs_sys::pthread_mutex_t::default(),
                cond: ibverbs_sys::pthread_cond_t::default(),
                events_completed: 0,
            })),
            Err(err) => {
                error!("Failed to create qp: {err}");
                ptr::null_mut()
            }
        }
    }

    #[inline]
    fn destroy_qp(qp: *mut ibverbs_sys::ibv_qp) -> ::std::os::raw::c_int {
        let qp = deref_or_ret!(qp, libc::EINVAL);
        let context = qp.context;
        let mut bluerdma = get_device(context);
        let qpn = qp.qp_num;
        match bluerdma.destroy_qp(qpn) {
            Ok(()) => 0,
            Err(err) => {
                error!("Failed to destroy QP: {qpn}");
                err.to_errno()
            }
        }
    }

    #[allow(clippy::cast_sign_loss)]
    #[inline]
    fn modify_qp(
        qp: *mut ibverbs_sys::ibv_qp,
        attr: *mut ibverbs_sys::ibv_qp_attr,
        attr_mask: core::ffi::c_int,
    ) -> ::std::os::raw::c_int {
        let qp = deref_or_ret!(qp, libc::EINVAL);
        let attr = deref_or_ret!(attr, libc::EINVAL);
        let context = qp.context;
        let mut bluerdma = get_device(context);
        let mask = attr_mask as u32;
        match bluerdma.update_qp(qp.qp_num, IbvQpAttr::new(attr, attr_mask as u32)) {
            Ok(()) => 0,
            Err(err) => {
                error!("Failed to modify QP: qpn=0x{:x}, err={:?}", qp.qp_num, err);
                err.to_errno()
            }
        }
    }

    #[inline]
    fn query_qp(
        qp: *mut ibverbs_sys::ibv_qp,
        attr: *mut ibverbs_sys::ibv_qp_attr,
        attr_mask: core::ffi::c_int,
        init_attr: *mut ibverbs_sys::ibv_qp_init_attr,
    ) -> ::std::os::raw::c_int {
        let qp = deref_or_ret!(qp, libc::EINVAL);
        let context = qp.context;
        let bluerdma = get_device(context);

        0
    }

    #[allow(clippy::cast_sign_loss)]
    #[inline]
    fn reg_mr(
        pd: *mut ibverbs_sys::ibv_pd,
        addr: *mut ::std::os::raw::c_void,
        length: usize,
        _hca_va: u64,
        access: core::ffi::c_int,
    ) -> *mut ibverbs_sys::ibv_mr {
        let pd_deref = deref_or_ret!(pd, ptr::null_mut());
        let context = pd_deref.context;
        let pd_handle = pd_deref.handle;
        let mut bluerdma = get_device(pd_deref.context);
        match bluerdma.reg_mr(addr as u64, length, pd_handle, access as u8) {
            Ok(mr_key) => {
                let ibv_mr = Box::new(ibverbs_sys::ibv_mr {
                    context,
                    pd,
                    addr,
                    length,
                    handle: mr_key, // the `mr_key` is used for identify the memory region
                    lkey: mr_key,
                    rkey: mr_key,
                });
                Box::into_raw(ibv_mr)
            }
            Err(err) => {
                error!("Failed to register MR, {err}");
                ptr::null_mut()
            }
        }
    }

    #[inline]
    fn dereg_mr(mr: *mut ibverbs_sys::ibv_mr) -> ::std::os::raw::c_int {
        let mr = deref_or_ret!(mr, libc::EINVAL);
        let pd = deref_or_ret!(mr.pd, libc::EINVAL);
        let mut bluerdma = get_device(mr.context);
        match bluerdma.dereg_mr(mr.handle) {
            Ok(()) => 0,
            Err(err) => {
                error!("Failed to deregister MR: {err}");
                err.to_errno()
            }
        }
    }

    #[inline]
    fn post_send(
        qp: *mut ibverbs_sys::ibv_qp,
        wr: *mut ibverbs_sys::ibv_send_wr,
        bad_wr: *mut *mut ibverbs_sys::ibv_send_wr,
    ) -> ::std::os::raw::c_int {
        let qp = deref_or_ret!(qp, libc::EINVAL);
        let context = qp.context;
        let qp_num = qp.qp_num;
        let mut bluerdma = get_device(context);
        let mut count: usize = 0;
        // Traverse the entire WR chain
        let mut current_wr_ptr = wr;
        while !current_wr_ptr.is_null() {
            count += 1;
            // SAFETY: We've checked that current_wr_ptr is not null
            let current_wr = unsafe { &*current_wr_ptr };

            // Convert current WR to internal representation
            let send_wr = match SendWr::new(*current_wr) {
                Ok(wr) => wr,
                Err(err) => {
                    error!("Invalid send WR: {err}");
                    unsafe { *bad_wr = current_wr_ptr };
                    return libc::EINVAL;
                }
            };

            // Post current WR
            if let Err(err) = bluerdma.post_send(qp_num, send_wr) {
                error!("Failed to post send WR: {err}");
                unsafe { *bad_wr = current_wr_ptr };
                return err.to_errno();
            }

            // Move to next WR in the chain
            current_wr_ptr = current_wr.next;
        }

        log::debug!("[CORE] send WR count: {count}");
        // All WRs posted successfully
        unsafe { *bad_wr = ptr::null_mut() };
        0
    }

    #[inline]
    fn post_recv(
        qp: *mut ibverbs_sys::ibv_qp,
        wr: *mut ibverbs_sys::ibv_recv_wr,
        bad_wr: *mut *mut ibverbs_sys::ibv_recv_wr,
    ) -> ::std::os::raw::c_int {
        let qp = deref_or_ret!(qp, libc::EINVAL);
        let context = qp.context;
        let qp_num = qp.qp_num;
        let mut bluerdma = get_device(context);
        let mut count: usize = 0;

        // Traverse the entire WR chain
        let mut current_wr_ptr = wr;
        while !current_wr_ptr.is_null() {
            count += 1;
            // SAFETY: We've checked that current_wr_ptr is not null
            let current_wr = unsafe { &*current_wr_ptr };

            // Convert current WR to internal representation
            let Some(recv_wr) = RecvWr::new(*current_wr) else {
                error!("Invalid receive WR: only 0 or 1 SGE is supported (num_sge must be 0 or 1)");
                unsafe { *bad_wr = current_wr_ptr };
                return libc::EINVAL;
            };

            // Post current WR
            if let Err(err) = bluerdma.post_recv(qp_num, recv_wr) {
                error!("Failed to post recv WR: {err}");
                unsafe { *bad_wr = current_wr_ptr };
                return err.to_errno();
            }

            // Move to next WR in the chain
            current_wr_ptr = current_wr.next;
        }

        log::debug!("[CORE] recv WR count: {count}");
        // All WRs posted successfully
        unsafe { *bad_wr = ptr::null_mut() };
        0
    }

    #[allow(
        clippy::as_conversions,
        clippy::cast_sign_loss,
        clippy::cast_possible_wrap
    )]
    #[inline]
    fn poll_cq(
        cq: *mut ibverbs_sys::ibv_cq,
        num_entries: i32,
        wc: *mut ibverbs_sys::ibv_wc,
    ) -> i32 {
        let cq = deref_or_ret!(cq, 0);
        let mut bluerdma = get_device(cq.context);
        let completions = bluerdma.poll_cq(cq.handle, num_entries as usize);
        let num = completions.len() as i32;
        for (i, c) in completions.into_iter().enumerate() {
            if let Some(wc) = unsafe { wc.add(i).as_mut() } {
                match c {
                    Completion::Send { wr_id }
                    | Completion::RdmaWrite { wr_id }
                    | Completion::RdmaRead { wr_id } => {
                        wc.wr_id = wr_id;
                        wc.wc_flags = 0;
                    }
                    Completion::Recv { wr_id, imm } => {
                        wc.wr_id = wr_id;
                        if let Some(imm) = imm {
                            wc.__bindgen_anon_1.imm_data = imm;
                            wc.wc_flags = ibverbs_sys::ibv_wc_flags::IBV_WC_WITH_IMM.0;
                        } else {
                            wc.wc_flags = 0;
                        }
                    }
                    Completion::RecvRdmaWithImm { wr_id, imm } => {
                        wc.wr_id = wr_id;
                        wc.__bindgen_anon_1.imm_data = imm;
                        wc.wc_flags = ibverbs_sys::ibv_wc_flags::IBV_WC_WITH_IMM.0;
                    }
                }
                wc.opcode = c.opcode();
                wc.status = ibverbs_sys::ibv_wc_status::IBV_WC_SUCCESS;
            }
        }

        num
    }
}
