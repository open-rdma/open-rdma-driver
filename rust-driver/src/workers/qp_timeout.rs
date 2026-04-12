use std::time::{Duration, Instant};

use log::{trace, warn};
use serde::{Deserialize, Serialize};

use crate::{
    constants::{
        DEFAULT_INIT_RETRY_COUNT, DEFAULT_LOCAL_ACK_TIMEOUT, DEFAULT_TIMEOUT_CHECK_DURATION,
        QPN_KEY_PART_WIDTH,
    },
    rdma_utils::qp::QpTable,
    workers::{
        retransmit::PacketRetransmitTask,
        spawner::{SingleThreadTaskWorker, TaskTx},
    },
};

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub(crate) struct AckTimeoutConfig {
    // 4.096 uS * 2^(CHECK DURATION)
    pub(crate) check_duration_exp: u8,
    // 4.096 uS * 2^(Local ACK Timeout)
    pub(crate) local_ack_timeout_exp: u8,
    pub(crate) init_retry_count: usize,
}

impl Default for AckTimeoutConfig {
    fn default() -> Self {
        Self {
            check_duration_exp: DEFAULT_TIMEOUT_CHECK_DURATION,
            local_ack_timeout_exp: DEFAULT_LOCAL_ACK_TIMEOUT,
            init_retry_count: DEFAULT_INIT_RETRY_COUNT,
        }
    }
}

impl AckTimeoutConfig {
    pub(crate) fn new(check_duration: u8, local_ack_timeout: u8, init_retry_count: usize) -> Self {
        Self {
            check_duration_exp: check_duration,
            local_ack_timeout_exp: local_ack_timeout,
            init_retry_count,
        }
    }
}

#[allow(variant_size_differences)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum AckTimeoutTask {
    // A new message with the AckReq bit set
    NewAckReq {
        qpn: u32,
    },
    // A new meta is received
    RecvMeta {
        qpn: u32,
    },
    /// The previous message is successfully acknowledged
    Ack {
        qpn: u32,
    },
}

impl AckTimeoutTask {
    pub(crate) fn new_ack_req(qpn: u32) -> Self {
        Self::NewAckReq { qpn }
    }

    pub(crate) fn recv_meta(qpn: u32) -> Self {
        Self::RecvMeta { qpn }
    }

    pub(crate) fn ack(qpn: u32) -> Self {
        Self::Ack { qpn }
    }

    pub(crate) fn qpn(self) -> u32 {
        match self {
            AckTimeoutTask::NewAckReq { qpn }
            | AckTimeoutTask::RecvMeta { qpn }
            | AckTimeoutTask::Ack { qpn } => qpn,
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct TransportTimer {
    timeout_interval: Option<Duration>,
    last_start: Option<Instant>,
    init_retry_counter: usize,
    current_retry_counter: usize,
}

impl TransportTimer {
    pub(crate) fn new(local_ack_timeout: u8, init_retry_counter: usize) -> Self {
        let timeout_nanos = if local_ack_timeout == 0 {
            // disabled
            None
        } else {
            // 4.096 uS * 2^(Local ACK Timeout)
            Some(4096u64 << local_ack_timeout)
        };

        Self {
            timeout_interval: timeout_nanos.map(Duration::from_nanos),
            last_start: None,
            init_retry_counter,
            current_retry_counter: init_retry_counter,
        }
    }

    /// Returns `Ok(true)` if timeout
    pub(crate) fn check_timeout(&mut self) -> TimerResult {
        let Some(timeout_interval) = self.timeout_interval else {
            return TimerResult::Ok;
        };
        let Some(start_time) = self.last_start else {
            return TimerResult::Ok;
        };
        let elapsed = start_time.elapsed();
        if elapsed < timeout_interval {
            return TimerResult::Ok;
        }
        if self.current_retry_counter == 0 {
            return TimerResult::RetryLimitExceeded;
        }
        self.current_retry_counter -= 1;
        self.reset();
        TimerResult::Timeout
    }

    fn is_running(&self) -> bool {
        self.last_start.is_some()
    }

    fn stop(&mut self) {
        self.last_start = None;
    }

    fn restart(&mut self) {
        self.current_retry_counter = self.init_retry_counter;
        self.last_start = Some(Instant::now());
    }

    fn reset(&mut self) {
        self.last_start = Some(Instant::now());
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum TimerResult {
    Ok,
    Timeout,
    RetryLimitExceeded,
}

pub(crate) struct QpAckTimeoutWorker {
    packet_retransmit_tx: TaskTx<PacketRetransmitTask>,
    timer_table: QpTable<TransportTimer>,
    // TODO: maintain this value as atomic variable
    outstanding_ack_req_cnt: QpTable<usize>,
    config: AckTimeoutConfig,
}

impl SingleThreadTaskWorker for QpAckTimeoutWorker {
    type Task = AckTimeoutTask;

    fn process(&mut self, task: Self::Task) {
        let qpn = task.qpn();
        match task {
            AckTimeoutTask::NewAckReq { qpn } => {
                trace!("new ack req, qpn: {qpn}");
                let _ignore = self.outstanding_ack_req_cnt.map_qp_mut(qpn, |x| *x += 1);
                let _ignore = self.timer_table.map_qp_mut(qpn, TransportTimer::restart);
            }
            AckTimeoutTask::RecvMeta { qpn } => {
                trace!("recv meta, qpn: {qpn}");
                let _ignore = self.timer_table.map_qp_mut(qpn, TransportTimer::restart);
            }
            AckTimeoutTask::Ack { qpn } => {
                if self
                    .outstanding_ack_req_cnt
                    .map_qp_mut(qpn, |x| {
                        *x -= 1;
                        trace!("ack, qpn: {qpn}, outstanding: {x}");
                        *x == 0
                    })
                    .unwrap_or(false)
                {
                    let _ignore = self.timer_table.map_qp_mut(qpn, TransportTimer::stop);
                }
            }
        }
    }

    fn maintainance(&mut self) {
        for (index, timer) in self.timer_table.iter_mut().enumerate() {
            match timer.check_timeout() {
                TimerResult::Ok => {}
                TimerResult::Timeout => {
                    warn!("timeout, qp index: {index}");
                    // no need for exact qpn, as it will be later converted to index anyway
                    let qpn = (index << QPN_KEY_PART_WIDTH) as u32;
                    self.packet_retransmit_tx
                        .send(PacketRetransmitTask::RetransmitAll { qpn });
                }
                TimerResult::RetryLimitExceeded => todo!("handle retry failures"),
            }
        }
    }
}

impl QpAckTimeoutWorker {
    pub(crate) fn new(
        packet_retransmit_tx: TaskTx<PacketRetransmitTask>,
        config: AckTimeoutConfig,
    ) -> Self {
        let timer_table = QpTable::new_with(|| {
            TransportTimer::new(config.local_ack_timeout_exp, config.init_retry_count)
        });
        Self {
            packet_retransmit_tx,
            timer_table,
            config,
            outstanding_ack_req_cnt: QpTable::new(),
        }
    }
}

#[allow(clippy::unchecked_duration_subtraction)]
#[cfg(test)]
mod tests {
    use super::*;
    use crate::workers::spawner::task_channel;
    use std::time::Duration;

    #[test]
    fn test_transport_timer_disabled() {
        let mut timer = TransportTimer::new(0, 3); // timeout disabled
        assert!(!timer.is_running());

        timer.restart();
        assert!(timer.is_running());

        // Should always return Ok when disabled
        assert!(matches!(timer.check_timeout(), TimerResult::Ok));
    }

    #[test]
    fn test_transport_timer_not_started() {
        let mut timer = TransportTimer::new(1, 3);
        assert!(!timer.is_running());

        // Should return Ok when not started
        assert!(matches!(timer.check_timeout(), TimerResult::Ok));
    }

    #[test]
    fn test_transport_timer_basic_operations() {
        let mut timer = TransportTimer::new(1, 3);

        // Start timer
        timer.restart();
        assert!(timer.is_running());

        // Should not timeout immediately
        assert!(matches!(timer.check_timeout(), TimerResult::Ok));

        // Stop timer
        timer.stop();
        assert!(!timer.is_running());

        // Should return Ok when stopped
        assert!(matches!(timer.check_timeout(), TimerResult::Ok));
    }

    #[test]
    fn test_transport_timer_timeout_calculation() {
        // Test timeout calculation: 4.096 uS * 2^1 = 8.192 uS
        let timer = TransportTimer::new(1, 3);
        let expected_nanos = 4096u64 << 1;
        assert_eq!(
            timer.timeout_interval,
            Some(Duration::from_nanos(expected_nanos))
        );

        // Test with larger exponent
        let timer = TransportTimer::new(5, 3);
        let expected_nanos = 4096u64 << 5;
        assert_eq!(
            timer.timeout_interval,
            Some(Duration::from_nanos(expected_nanos))
        );
    }

    #[test]
    fn test_transport_timer_retry_logic() {
        let mut timer = TransportTimer::new(1, 2); // 2 retries
        timer.restart();

        // Simulate timeout by setting start time in the past
        timer.last_start = Some(Instant::now() - Duration::from_millis(100));

        // First timeout should return Timeout and decrement counter
        assert!(matches!(timer.check_timeout(), TimerResult::Timeout));

        // Second timeout should return Timeout and decrement counter
        timer.last_start = Some(Instant::now() - Duration::from_millis(100));
        assert!(matches!(timer.check_timeout(), TimerResult::Timeout));

        // Third timeout should return RetryLimitExceeded
        timer.last_start = Some(Instant::now() - Duration::from_millis(100));
        assert!(matches!(
            timer.check_timeout(),
            TimerResult::RetryLimitExceeded
        ));
    }

    #[test]
    fn test_qp_ack_timeout_worker_new_ack_req() {
        let (tx, rx) = task_channel();
        let config = AckTimeoutConfig::default();
        let mut worker = QpAckTimeoutWorker::new(tx, config);

        let qpn = 42;
        let task = AckTimeoutTask::new_ack_req(qpn);

        // Process new ack req task
        worker.process(task);

        // Verify outstanding count increased
        let count = worker
            .outstanding_ack_req_cnt
            .map_qp_mut(qpn, |x| *x)
            .unwrap_or(0);
        assert_eq!(count, 1);

        // Verify timer is running
        let is_running = worker
            .timer_table
            .map_qp_mut(qpn, |timer| timer.is_running())
            .unwrap_or(false);
        assert!(is_running);
    }

    #[test]
    fn test_qp_ack_timeout_worker_recv_meta() {
        let (tx, rx) = task_channel();
        let config = AckTimeoutConfig::default();
        let mut worker = QpAckTimeoutWorker::new(tx, config);

        let qpn = 42;
        let task = AckTimeoutTask::recv_meta(qpn);

        // Process recv meta task
        worker.process(task);

        // Verify timer is running (restarted)
        let is_running = worker
            .timer_table
            .map_qp_mut(qpn, |timer| timer.is_running())
            .unwrap_or(false);
        assert!(is_running);
    }

    #[test]
    fn test_qp_ack_timeout_worker_ack_single() {
        let (tx, rx) = task_channel();
        let config = AckTimeoutConfig::default();
        let mut worker = QpAckTimeoutWorker::new(tx, config);

        let qpn = 42;

        // First add an ack req
        worker.process(AckTimeoutTask::new_ack_req(qpn));

        // Verify timer is running and count is 1
        let count = worker
            .outstanding_ack_req_cnt
            .map_qp_mut(qpn, |x| *x)
            .unwrap_or(0);
        assert_eq!(count, 1);
        let is_running = worker
            .timer_table
            .map_qp_mut(qpn, |timer| timer.is_running())
            .unwrap_or(false);
        assert!(is_running);

        // Process ack task
        worker.process(AckTimeoutTask::ack(qpn));

        // Verify count decreased and timer stopped
        let count = worker
            .outstanding_ack_req_cnt
            .map_qp_mut(qpn, |x| *x)
            .unwrap_or(0);
        assert_eq!(count, 0);
        let is_running = worker
            .timer_table
            .map_qp_mut(qpn, |timer| timer.is_running())
            .unwrap_or(false);
        assert!(!is_running);
    }

    #[test]
    fn test_qp_ack_timeout_worker_ack_multiple() {
        let (tx, rx) = task_channel();
        let config = AckTimeoutConfig::default();
        let mut worker = QpAckTimeoutWorker::new(tx, config);

        let qpn = 42;

        // Add multiple ack reqs
        worker.process(AckTimeoutTask::new_ack_req(qpn));
        worker.process(AckTimeoutTask::new_ack_req(qpn));

        // Verify count is 2 and timer is running
        let count = worker
            .outstanding_ack_req_cnt
            .map_qp_mut(qpn, |x| *x)
            .unwrap_or(0);
        assert_eq!(count, 2);
        let is_running = worker
            .timer_table
            .map_qp_mut(qpn, |timer| timer.is_running())
            .unwrap_or(false);
        assert!(is_running);

        // Process one ack
        worker.process(AckTimeoutTask::ack(qpn));

        // Verify count decreased but timer still running
        let count = worker
            .outstanding_ack_req_cnt
            .map_qp_mut(qpn, |x| *x)
            .unwrap_or(0);
        assert_eq!(count, 1);
        let is_running = worker
            .timer_table
            .map_qp_mut(qpn, |timer| timer.is_running())
            .unwrap_or(false);
        assert!(is_running);

        // Process second ack
        worker.process(AckTimeoutTask::ack(qpn));

        // Verify count is 0 and timer stopped
        let count = worker
            .outstanding_ack_req_cnt
            .map_qp_mut(qpn, |x| *x)
            .unwrap_or(0);
        assert_eq!(count, 0);
        let is_running = worker
            .timer_table
            .map_qp_mut(qpn, |timer| timer.is_running())
            .unwrap_or(false);
        assert!(!is_running);
    }

    #[test]
    fn test_transport_timer_restart_resets_retry_counter() {
        let mut timer = TransportTimer::new(1, 3);
        timer.restart();

        // Simulate timeout to decrement retry counter
        timer.last_start = Some(Instant::now() - Duration::from_millis(100));
        timer.check_timeout(); // This should decrement counter and restart

        // Restart should reset the counter
        timer.restart();

        // Verify we can timeout the full number of times again
        for _ in 0..3 {
            timer.last_start = Some(Instant::now() - Duration::from_millis(100));
            let result = timer.check_timeout();
            assert!(matches!(result, TimerResult::Timeout));
        }

        // Next timeout should exceed retry limit
        timer.last_start = Some(Instant::now() - Duration::from_millis(100));
        assert!(matches!(
            timer.check_timeout(),
            TimerResult::RetryLimitExceeded
        ));
    }
}
