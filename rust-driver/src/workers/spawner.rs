use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use log::{error, info};

pub(crate) trait SingleThreadPollingWorker {
    type Task;

    fn poll(&mut self) -> Option<Self::Task>;

    fn process(&mut self, task: Self::Task);

    fn spawn(mut self, name: &str, abort: AbortSignal)
    where
        Self: Sized + Send + 'static,
        Self::Task: Send + 'static,
    {
        let name = name.to_owned();
        let abort = AbortSignal::new();
        let abort_c = abort.clone();
        let _handle = std::thread::Builder::new()
            .name(name.clone())
            .spawn(move || {
                info!("worker {name} running");
                loop {
                    if abort.should_abort() {
                        break;
                    }
                    if let Some(task) = self.poll() {
                        self.process(task);
                    }
                    // std::thread::sleep(Duration::from_nanos(10000));
                }
                info!("worker {name} exited");
            })
            .expect("failed to spawn worker");
    }
}

pub(crate) trait SingleThreadTaskWorker {
    type Task;

    fn process(&mut self, task: Self::Task);

    fn maintainance(&mut self);

    fn spawn(mut self, rx: TaskRx<Self::Task>, name: &str, abort: AbortSignal)
    where
        Self: Sized + Send + 'static,
        Self::Task: Send + 'static,
    {
        let name = name.to_owned();
        let abort = AbortSignal::new();
        let abort_c = abort.clone();
        let _handle = std::thread::Builder::new()
            .name(name.clone())
            .spawn(move || {
                info!("worker {name} running");
                loop {
                    if abort.should_abort() {
                        break;
                    }
                    if let Some(task) = rx.recv() {
                        self.process(task);
                    } else {
                        error!("failed to recv task from channel");
                        break;
                    }
                }
                info!("worker {name} exited");
            })
            .expect("failed to spawn worker");
    }

    fn spawn_polling(
        mut self,
        rx: TaskRx<Self::Task>,
        name: &str,
        abort: AbortSignal,
        interval: Duration,
    ) where
        Self: Sized + Send + 'static,
        Self::Task: Send + 'static,
    {
        let name = name.to_owned();
        let abort = AbortSignal::new();
        let abort_c = abort.clone();
        let _handle = std::thread::Builder::new()
            .name(name.clone())
            .spawn(move || {
                info!("worker {name} running");
                loop {
                    spin_sleep::sleep(interval);
                    if abort.should_abort() {
                        break;
                    }
                    for task in rx.try_iter() {
                        self.process(task);
                    }
                    self.maintainance();
                }
                info!("worker {name} exited");
            })
            .expect("failed to spawn worker");
    }
}

pub(crate) struct TaskTx<T> {
    inner: flume::Sender<T>,
}

impl<T> TaskTx<T> {
    pub(crate) fn send(&self, task: T) {
        self.inner
            .send(task)
            .expect("failed to send task to channel");
    }
}

impl<T> Clone for TaskTx<T> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

pub(crate) struct TaskRx<T> {
    inner: flume::Receiver<T>,
}

impl<T> TaskRx<T> {
    pub(crate) fn recv(&self) -> Option<T> {
        self.inner.recv().ok()
    }

    pub(crate) fn try_recv(&self) -> Option<T> {
        self.inner.try_recv().ok()
    }

    fn try_iter(&self) -> flume::TryIter<'_, T> {
        self.inner.try_iter()
    }
}

pub(crate) fn task_channel<T>() -> (TaskTx<T>, TaskRx<T>) {
    let (tx, rx) = flume::unbounded();
    (TaskTx { inner: tx }, TaskRx { inner: rx })
}

#[derive(Debug, Clone)]
pub(crate) struct AbortSignal {
    inner: Arc<AtomicBool>,
}

impl AbortSignal {
    pub(crate) fn new() -> Self {
        Self {
            inner: Arc::new(AtomicBool::new(false)),
        }
    }

    fn should_abort(&self) -> bool {
        self.inner.load(Ordering::Relaxed)
    }

    pub(crate) fn abort(&self) {
        self.inner.store(true, Ordering::Relaxed);
    }
}
