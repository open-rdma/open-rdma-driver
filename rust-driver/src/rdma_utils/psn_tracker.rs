use bitvec::vec::BitVec;
use log::debug;

use crate::rdma_utils::psn::Psn;

#[derive(Debug, Default)]
pub(crate) struct LocalAckTracker {
    psn_tracker: PsnTracker,
    psn_pre: Psn,
}

impl LocalAckTracker {
    pub(crate) fn ack_one(&mut self, psn: Psn) -> Option<Psn> {
        debug!(
            "LocalAckTracker::ack_one enter: psn={:?} tracker={:?}",
            psn, self
        );
        self.psn_tracker.ack_one(psn)
    }

    pub(crate) fn ack_bitmap(&mut self, base_psn: Psn, bitmap: u128) -> Option<Psn> {
        debug!(
            "LocalAckTracker::ack_bitmap enter: base_psn={:?} bitmap={:#034x} tracker={:?}",
            base_psn, bitmap, self
        );
        let x = self.psn_tracker.ack_range(self.psn_pre, base_psn);
        let y = self.psn_tracker.ack_bitmap(base_psn, bitmap);
        if self.psn_pre < base_psn {
            self.psn_pre = base_psn;
        }
        let result = y.or(x);
        debug!(
            "LocalAckTracker::ack_bitmap exit: result={:?} tracker={:?}",
            result, self
        );
        result
    }

    pub(crate) fn nak_bitmap(
        &mut self,
        psn_pre: Psn,
        pre_bitmap: u128,
        psn_now: Psn,
        now_bitmap: u128,
    ) -> Option<Psn> {
        debug!(
            "LocalAckTracker::nak_bitmap enter: psn_pre={:?} pre_bitmap={:#034x} psn_now={:?} now_bitmap={:#034x} tracker={:?}",
            psn_pre, pre_bitmap, psn_now, now_bitmap, self
        );
        let x = self.psn_tracker.ack_range(self.psn_pre, psn_pre);
        let y = self.psn_tracker.ack_bitmap(psn_pre, pre_bitmap);
        let z = self.psn_tracker.ack_bitmap(psn_now, now_bitmap);
        if self.psn_pre < psn_now {
            self.psn_pre = psn_now;
        }
        let result = z.or(y).or(x);
        debug!(
            "LocalAckTracker::nak_bitmap exit: result={:?} tracker={:?}",
            result, self
        );
        result
    }

    pub(crate) fn base_psn(&self) -> Psn {
        self.psn_tracker.base_psn()
    }
}

#[derive(Debug, Default)]
pub(crate) struct RemoteAckTracker {
    psn_tracker: PsnTracker,
    msn_pre: u16,
    psn_pre: Psn,
}

impl RemoteAckTracker {
    pub(crate) fn ack_before(&mut self, psn: Psn) -> Option<Psn> {
        self.psn_tracker.ack_before(psn)
    }

    pub(crate) fn nak_bitmap(
        &mut self,
        msn: u16,
        psn_pre: Psn,
        pre_bitmap: u128,
        psn_now: Psn,
        now_bitmap: u128,
    ) -> Option<Psn> {
        let x = (msn == self.msn_pre.wrapping_add(1))
            .then(|| self.psn_tracker.ack_range(self.psn_pre, psn_pre))
            .flatten();
        let y = self.psn_tracker.ack_bitmap(psn_pre, pre_bitmap);
        let z = self.psn_tracker.ack_bitmap(psn_now, now_bitmap);
        if self.psn_pre < psn_now {
            self.psn_pre = psn_now;
            self.msn_pre = msn;
        }
        z.or(y).or(x)
    }
}

#[derive(Default, Debug, Clone)]
pub(crate) struct PsnTracker {
    base_psn: Psn,
    inner: BitVec,
}

#[allow(clippy::cast_possible_wrap, clippy::cast_sign_loss)] // won't wrap since we only use 24bits of the Psn
impl PsnTracker {
    #[allow(clippy::as_conversions)] // Psn to usize
    /// Acknowledges a range of PSNs starting from `base_psn` using a bitmap.
    ///
    /// # Returns
    ///
    /// Returns `Some(PSN)` if the left edge of the PSN window is advanced, where the
    /// returned `PSN` is the new base PSN value after the advance.
    pub(crate) fn ack_bitmap(&mut self, now_psn: Psn, bitmap: u128) -> Option<Psn> {
        let rstart = self.rstart(now_psn);
        let rend = rstart + 128;
        if let Ok(x) = usize::try_from(rend) {
            if x > self.inner.len() {
                self.inner.resize(x, false);
            }
        }
        for i in rstart.max(0)..rend {
            let x = (i - rstart) as usize;
            if bitmap.wrapping_shr(x as u32) & 1 == 1 {
                self.inner.set(i as usize, true);
            }
        }

        self.try_advance()
    }

    /// Acknowledges a range of PSNs from `psn_low` to `psn_high` (exclusive).
    ///
    /// # Returns
    /// * `Some(PSN)` - If the acknowledgment causes the base PSN to advance, returns the new base PSN
    /// * `None` - If the base PSN doesn't change
    pub(crate) fn ack_range(&mut self, psn_low: Psn, psn_high: Psn) -> Option<Psn> {
        if psn_low <= self.base_psn {
            return self.ack_before(psn_high);
        }
        let rstart: usize = usize::try_from(self.rstart(psn_low)).ok()?;
        let rend: usize = usize::try_from(self.rstart(psn_high)).ok()?;
        if rend >= self.inner.len() {
            self.inner.resize(rend + 1, false);
        }
        for i in rstart..rend {
            self.inner.set(i, true);
        }
        None
    }

    /// Acknowledges a single PSN.
    ///
    /// # Returns
    ///
    /// Returns `Some(PSN)` if the left edge of the PSN window is advanced, where the
    /// returned `PSN` is the new base PSN value after the advance.
    pub(crate) fn ack_one(&mut self, psn: Psn) -> Option<Psn> {
        let rstart: usize = usize::try_from(self.rstart(psn)).ok()?;
        if rstart >= self.inner.len() {
            self.inner.resize(rstart + 1, false);
        }
        self.inner.set(rstart, true);
        self.try_advance()
    }

    /// Acknowledges all PSNs before the given PSN.
    ///
    /// # Returns
    ///
    /// Returns `Some(PSN)` if the left edge of the PSN window is advanced, where the
    /// returned `PSN` is the new base PSN value after the advance.
    pub(crate) fn ack_before(&mut self, psn: Psn) -> Option<Psn> {
        let rstart: usize = usize::try_from(self.rstart(psn)).ok()?;
        self.base_psn = psn;
        if rstart >= self.inner.len() {
            self.inner.fill(false);
        } else {
            self.inner.shift_left(rstart);
        }
        Some(psn)
    }

    pub(crate) fn base_psn(&self) -> Psn {
        self.base_psn
    }

    // 是不是不应该硬编码
    fn rstart(&self, psn: Psn) -> i32 {
        let x = psn.into_inner().wrapping_sub(self.base_psn.into_inner());
        if ((x >> 23) & 1) != 0 {
            (x | 0xFF00_0000) as i32
        } else {
            x as i32
        }
    }

    /// Try to advance the base PSN to the next unacknowledged PSN.
    ///
    /// # Returns
    ///
    /// Returns `Some(PSN)` if `base_psn` was advanced, where the returned `PSN` is the new
    /// base PSN value after the advance.
    fn try_advance(&mut self) -> Option<Psn> {
        let pos = self.inner.first_zero().unwrap_or(self.inner.len());
        if pos == 0 {
            return None;
        }
        self.inner.shift_left(pos);
        let psn = self.base_psn;
        self.base_psn += pos as u32;
        Some(self.base_psn)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::constants::PSN_MASK;

    #[test]
    fn test_ack_one() {
        let mut tracker = PsnTracker::default();
        tracker.ack_one(5.into());
        assert!(!tracker.inner[0..5].iter().any(|b| *b));
        assert!(tracker.inner[5]);
    }

    #[test]
    fn test_ack_range() {
        let mut tracker = PsnTracker::default();
        tracker.ack_bitmap(0.into(), 0b11); // PSN 0 and 1
        assert_eq!(tracker.base_psn, 2.into());
        assert!(tracker.inner.not_all());

        let mut tracker = PsnTracker {
            base_psn: 5.into(),
            ..Default::default()
        };
        tracker.ack_bitmap(5.into(), 0b11);
        assert_eq!(tracker.base_psn, 7.into());
        assert!(tracker.inner.not_all());

        let mut tracker = PsnTracker {
            base_psn: 10.into(),
            ..Default::default()
        };
        tracker.ack_bitmap(5.into(), 0b11);
        assert_eq!(tracker.base_psn, 10.into());
        assert!(tracker.inner.not_all());
        tracker.ack_bitmap(20.into(), 0b11);
        assert_eq!(tracker.base_psn, 10.into());
        assert!(tracker.inner[10]);
        assert!(tracker.inner[11]);
    }

    #[test]
    fn test_wrapping_ack() {
        let mut tracker = PsnTracker {
            base_psn: (PSN_MASK - 1).into(),
            ..Default::default()
        };
        tracker.ack_bitmap(0.into(), 0b11);
    }
}
