use std::{iter, mem, sync::Arc};

use bitvec::vec::BitVec;
use parking_lot::Mutex;
use rand::Rng;

use crate::{
    constants::{MAX_PSN_WINDOW, MAX_QP_CNT, MAX_SEND_WR, QPN_KEY_PART_WIDTH},
    rdma_utils::psn::Psn,
};

/// Manages QPs
#[derive(Debug)]
pub(crate) struct QpManager {
    /// Bitmap tracking allocated QPNs
    bitmap: BitVec,
}

#[allow(clippy::as_conversions, clippy::indexing_slicing)]
impl QpManager {
    /// Creates a new `QpManager`
    pub(crate) fn new() -> Self {
        let mut bitmap = BitVec::with_capacity(MAX_QP_CNT);
        bitmap.resize(MAX_QP_CNT, false);
        bitmap.set(0, true);
        Self { bitmap }
    }

    /// Allocates a new QP and returns its QPN
    #[allow(clippy::cast_possible_truncation)] // no larger than u32
    pub(crate) fn create_qp(&mut self) -> Option<u32> {
        let index = self.bitmap.first_zero()? as u32;
        let key = rand::thread_rng().gen_range(0..1 << QPN_KEY_PART_WIDTH);
        self.bitmap.set(index as usize, true);
        let qpn = (index << QPN_KEY_PART_WIDTH) | key;
        Some(qpn)
    }

    /// Removes and returns the QP associated with the given QPN
    pub(crate) fn destroy_qp(&mut self, qpn: u32) -> bool {
        let index = qpn_to_index(qpn);
        let ret = self.bitmap.get(index).is_some_and(|x| *x);
        self.bitmap.set(index, false);

        ret
    }
}

#[derive(Debug)]
pub(crate) struct SendQueueContext {
    pub(crate) msn: u16,
    pub(crate) psn: Psn,
    pub(crate) psn_acked: Psn,
    pub(crate) msn_acked: u16,
}

impl Default for SendQueueContext {
    fn default() -> Self {
        Self {
            msn: 0,      // 自定义值
            psn: Psn(0), // 自定义值
            psn_acked: Psn::default(),
            msn_acked: 0,
        }
    }
}

impl SendQueueContext {
    #[allow(clippy::similar_names)]
    pub(crate) fn next_wr(&mut self, num_psn: u32) -> Option<(u16, Psn)> {
        let outstanding_num_psn = self.psn - self.psn_acked;
        let outstanding_num_msn = self.msn.wrapping_sub(self.msn_acked);
        if (outstanding_num_psn + num_psn).into_inner() as usize > MAX_PSN_WINDOW
            || outstanding_num_msn as usize >= MAX_SEND_WR
        {
            return None;
        }
        let current_psn = self.psn;
        let current_msn = self.msn;
        let next_msn = self.msn.wrapping_add(1);
        let next_psn = self.psn + num_psn;
        self.msn = next_msn;
        self.psn = next_psn;

        Some((current_msn, current_psn))
    }

    pub(crate) fn update_psn_acked(&mut self, psn: Psn) {
        self.psn_acked = psn;
    }

    pub(crate) fn update_msn_acked(&mut self, msn: u16) {
        self.msn_acked = msn;
    }
}

#[allow(clippy::as_conversions)] // u32 to usize
pub(crate) fn qpn_to_index(qpn: u32) -> usize {
    (qpn >> QPN_KEY_PART_WIDTH) as usize
}

/// Calculate the number of psn required for this WR
pub(crate) fn num_psn(pmtu: u8, addr: u64, length: u32) -> Option<u32> {
    let pmtu = convert_ibv_mtu_to_u16(pmtu)?;
    let pmtu_mask = pmtu
        .checked_sub(1)
        .unwrap_or_else(|| unreachable!("pmtu should be greater than 1"));
    let next_align_addr = addr.saturating_add(u64::from(pmtu_mask)) & !u64::from(pmtu_mask);
    let gap = next_align_addr.saturating_sub(addr);
    let length_u64 = u64::from(length);
    length_u64
        .checked_sub(gap)
        .unwrap_or(length_u64)
        .div_ceil(u64::from(pmtu))
        .max(1) // 0 length still needs at least 1 psn
        .try_into()
        .ok()
}

pub(crate) fn convert_ibv_mtu_to_u16(ibv_mtu: u8) -> Option<u16> {
    let pmtu = match u32::from(ibv_mtu) {
        ibverbs_sys::IBV_MTU_256 => 256,
        ibverbs_sys::IBV_MTU_512 => 512,
        ibverbs_sys::IBV_MTU_1024 => 1024,
        ibverbs_sys::IBV_MTU_2048 => 2048,
        ibverbs_sys::IBV_MTU_4096 => 4096,
        _ => return None,
    };
    Some(pmtu)
}

// TODO 需要改成 QpTable<T, const N: usize> 性能更高
#[derive(Debug)]
pub(crate) struct QpTable<T> {
    inner: Box<[T]>,
}

impl<T> QpTable<T> {
    pub(crate) fn new_with<F: FnMut() -> T>(f: F) -> Self {
        Self {
            inner: iter::repeat_with(f).take(MAX_QP_CNT).collect(),
        }
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = &T> {
        self.inner.iter()
    }

    pub(crate) fn iter_mut(&mut self) -> impl Iterator<Item = &mut T> {
        self.inner.iter_mut()
    }

    pub(crate) fn get_qp(&self, qpn: u32) -> Option<&T> {
        self.inner.get(qpn_to_index(qpn))
    }

    pub(crate) fn get_qp_mut(&mut self, qpn: u32) -> Option<&mut T> {
        self.inner.get_mut(qpn_to_index(qpn))
    }

    pub(crate) fn map_qp<R, F>(&self, qpn: u32, f: F) -> Option<R>
    where
        F: FnMut(&T) -> R,
    {
        self.inner.get(qpn_to_index(qpn)).map(f)
    }

    pub(crate) fn map_qp_mut<R, F>(&mut self, qpn: u32, f: F) -> Option<R>
    where
        F: FnOnce(&mut T) -> R,
    {
        self.inner.get_mut(qpn_to_index(qpn)).map(f)
    }

    pub(crate) fn replace(&mut self, qpn: u32, mut t: T) -> Option<T> {
        if let Some(x) = self.inner.get_mut(qpn_to_index(qpn)) {
            mem::swap(x, &mut t);
            Some(t)
        } else {
            None
        }
    }
}

impl<T: Default> QpTable<T> {
    pub(crate) fn new() -> Self {
        Self::default()
    }
}

impl<T: Default> Default for QpTable<T> {
    fn default() -> Self {
        Self::new_with(T::default)
    }
}

impl<T: Clone> Clone for QpTable<T> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct QpTableShared<T> {
    inner: Arc<[Mutex<T>]>,
}

impl<T> QpTableShared<T> {
    pub(crate) fn new_with<F: FnMut() -> T>(f: F) -> Self {
        Self {
            inner: iter::repeat_with(f)
                .take(MAX_QP_CNT)
                .map(Mutex::new)
                .collect(),
        }
    }

    pub(crate) fn for_each<F: FnMut(&T)>(&self, mut f: F) {
        self.inner.iter().for_each(|x| f(&x.lock()));
    }

    pub(crate) fn for_each_mut<F: FnMut(&mut T)>(&self, mut f: F) {
        self.inner.iter().for_each(|x| f(&mut x.lock()));
    }

    pub(crate) fn get_qp(&self, qpn: u32) -> Option<T>
    where
        T: Copy,
    {
        self.inner.get(qpn_to_index(qpn)).map(|x| *x.lock())
    }

    pub(crate) fn map_qp<R, F>(&self, qpn: u32, mut f: F) -> Option<R>
    where
        F: FnMut(&T) -> R,
    {
        self.inner.get(qpn_to_index(qpn)).map(|x| f(&x.lock()))
    }

    pub(crate) fn map_qp_mut<R, F>(&self, qpn: u32, f: F) -> Option<R>
    where
        F: FnOnce(&mut T) -> R,
    {
        self.inner.get(qpn_to_index(qpn)).map(|x| f(&mut x.lock()))
    }

    pub(crate) fn replace(&self, qpn: u32, mut t: T) -> Option<T> {
        if let Some(x) = self.inner.get(qpn_to_index(qpn)) {
            mem::swap(&mut *x.lock(), &mut t);
            Some(t)
        } else {
            None
        }
    }

    pub(crate) fn query<F>(&self, mut f: F) -> Option<T>
    where
        F: FnMut(&T) -> bool,
        T: Clone,
    {
        self.inner.iter().find_map(|x| {
            let guard = x.lock();
            f(&guard).then(|| guard.clone())
        })
    }
}

impl<T: Default> QpTableShared<T> {
    pub(crate) fn new() -> Self {
        Self::default()
    }
}

impl<T: Default> Default for QpTableShared<T> {
    fn default() -> Self {
        Self::new_with(T::default)
    }
}
