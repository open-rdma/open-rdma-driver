//! Emulated CSR Access Implementation
//!
//! This module provides CSR access for hardware simulation and RTL testing through
//! UDP-based RPC communication with a simulator backend.
//!
//! # Architecture
//!
//! ```text
//! Rust Driver (EmulatedDevice)
//!         ↓ UDP RPC
//!    127.0.0.1:7701 (uverbs0)
//!    127.0.0.1:7702 (uverbs1)
//!         ↓
//! RTL Simulator (Cocotb/Verilator)
//!         ↓
//! Hardware Model (Verilog/VHDL)
//! ```
//!
//! # Use Cases
//!
//! - RTL verification before hardware tapeout
//! - Driver development without physical hardware
//! - Regression testing in CI/CD environments
//! - Protocol debugging with full hardware state visibility
//!
//! # Communication Protocol
//!
//! The RPC protocol uses newline-delimited JSON messages over TCP:
//!
//! **Read Request**:
//! ```json
//! {"is_write": false, "addr": 0x1000, "value": 0}
//! ```
//!
//! **Write Request**:
//! ```json
//! {"is_write": true, "addr": 0x2000, "value": 0x42}
//! ```
//!
//! **Response** (for reads):
//! ```json
//! {"is_write": false, "addr": 0x1000, "value": 0x12345678}
//! ```
//!
//! # Port Mapping
//!
//! Device names map to TCP ports:
//! - `uverbs0` → 127.0.0.1:7701
//! - `uverbs1` → 127.0.0.1:7702
//! - `uverbs2` → 127.0.0.1:7703
//! - etc.
//!
//! # Example
//!
//! ```rust,ignore
//! use crate::csr::emulated::EmulatedDevice;
//!
//! // Connect to simulator
//! let dev = EmulatedDevice::new("uverbs0")?;  // Connects to 127.0.0.1:7701
//!
//! // CSR operations are forwarded to simulator
//! dev.write_csr(0x1000, 0x42)?;  // → TCP message to simulator
//! let value = dev.read_csr(0x1000)?;  // ← Response from simulator
//! ```

use std::{
    io::{self, BufRead, BufReader, Bytes, Write},
    net::{SocketAddr, TcpStream},
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};

use log::debug;
use serde::{Deserialize, Serialize};

use crate::ring::traits::DeviceAdaptor;

#[derive(Debug, Serialize, Deserialize)]
struct CsrAccessRpcMessage {
    is_write: bool,
    addr: usize,
    value: u32,
}

struct RpcClientInner {
    stream: TcpStream,
    reader: BufReader<TcpStream>,
}

impl std::fmt::Debug for RpcClientInner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RpcClientInner").finish()
    }
}

#[derive(Debug, Clone)]
pub(super) struct RpcClient(Arc<Mutex<RpcClientInner>>);

impl RpcClient {
    pub(super) fn new(server_addr: SocketAddr) -> io::Result<Self> {
        debug!("connect to: {server_addr}");

        let timeout_secs = std::env::var("SIM_TCP_RETRY_TIMEOUT_SECS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(300);
        let initial_delay_ms = std::env::var("SIM_TCP_RETRY_INITIAL_MS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(100);
        let max_delay_ms = std::env::var("SIM_TCP_RETRY_MAX_MS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(2000);

        let total_timeout = Duration::from_secs(timeout_secs);
        let start_time = std::time::Instant::now();
        let mut attempt = 0u64;
        let mut delay = Duration::from_millis(initial_delay_ms);

        let stream = loop {
            attempt += 1;
            match TcpStream::connect(server_addr) {
                Ok(s) => {
                    log::info!(
                        "Connected to simulator at {} after {} attempts ({:.2}s)",
                        server_addr,
                        attempt,
                        start_time.elapsed().as_secs_f64()
                    );
                    break s;
                }
                Err(e) => {
                    let elapsed = start_time.elapsed();
                    if elapsed >= total_timeout {
                        return Err(io::Error::new(
                            io::ErrorKind::TimedOut,
                            format!("Timeout connecting to simulator at {server_addr}: {e}"),
                        ));
                    }
                    if attempt == 1 {
                        log::info!(
                            "Waiting for simulator at {} (will retry for {:.0}s)...",
                            server_addr,
                            total_timeout.as_secs_f64()
                        );
                    }
                    let remaining = total_timeout.saturating_sub(elapsed);
                    let sleep_duration = delay.min(remaining);
                    if sleep_duration.is_zero() {
                        return Err(io::Error::new(
                            io::ErrorKind::TimedOut,
                            format!("Timeout connecting to simulator at {server_addr}"),
                        ));
                    }
                    thread::sleep(sleep_duration);
                    delay = (delay * 2).min(Duration::from_millis(max_delay_ms));
                }
            }
        };

        stream.set_nodelay(true)?;
        let stream_for_reader = stream.try_clone()?;
        let reader = BufReader::new(stream_for_reader);

        Ok(Self(Arc::new(Mutex::new(RpcClientInner {
            stream,
            reader,
        }))))
    }

    pub(super) fn read_csr(&self, addr: usize) -> io::Result<u32> {
        let msg = CsrAccessRpcMessage {
            is_write: false,
            addr,
            value: 0,
        };
        debug!("send msg: {msg:?}");

        #[allow(clippy::expect_used)]
        let mut inner = self.0.lock().expect("RpcClient mutex poisoned");
        let json = serde_json::to_string(&msg)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        inner.stream.write_all(json.as_bytes())?;
        inner.stream.write_all(b"\n")?;
        inner.stream.flush()?;

        let mut line = String::new();
        let bytes_read = inner.reader.read_line(&mut line)?;

        // 检查是否读取到数据
        if bytes_read == 0 {
            panic!("Simulator closed the connection unexpectedly");
        }
        let response = serde_json::from_str::<CsrAccessRpcMessage>(line.trim())
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

        Ok(response.value)
    }

    pub(super) fn write_csr(&self, addr: usize, data: u32) -> io::Result<()> {
        let msg = CsrAccessRpcMessage {
            is_write: true,
            addr,
            value: data,
        };
        debug!("send msg write: {msg:?}");

        #[allow(clippy::expect_used)]
        let mut inner = self.0.lock().expect("RpcClient mutex poisoned");
        let json = serde_json::to_string(&msg)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        inner.stream.write_all(json.as_bytes())?;
        inner.stream.write_all(b"\n")?;
        inner.stream.flush()?;
        Ok(())
    }
}

#[non_exhaustive]
#[derive(Clone, Debug)]
pub(crate) struct EmulatedDevice(RpcClient);

impl EmulatedDevice {
    /// Create a new emulated device with a default address based on device name
    pub(crate) fn new(device_name: &str) -> io::Result<Self> {
        // Parse device name like "uverbs0" or "test" to determine port
        // For testing, use a default port
        let port = if device_name.starts_with("uverbs") {
            let idx: usize = device_name
                .trim_start_matches("uverbs")
                .parse()
                .unwrap_or(0);
            7701 + idx
        } else {
            7701 // default port for testing
        };

        let addr = format!("127.0.0.1:{port}").parse().map_err(|e| {
            io::Error::new(io::ErrorKind::InvalidInput, format!("invalid address: {e}"))
        })?;

        Ok(EmulatedDevice(RpcClient::new(addr)?))
    }

    #[allow(clippy::expect_used)]
    pub(crate) fn new_with_addr(addr: &str) -> Self {
        EmulatedDevice(
            RpcClient::new(addr.parse().expect("invalid socket addr"))
                .expect("failed to connect to emulator"),
        )
    }
}

impl DeviceAdaptor for EmulatedDevice {
    fn read_csr(&self, addr: usize) -> io::Result<u32> {
        self.0.read_csr(addr)
    }

    fn write_csr(&self, addr: usize, data: u32) -> io::Result<()> {
        self.0.write_csr(addr, data)
    }
}
