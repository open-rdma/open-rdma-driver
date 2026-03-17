/*
 * Simplified Memory Proxy Server for Simulation Mode
 *
 * This module implements a lightweight TCP server that handles memory access requests
 * from the PCIe BFM (Bus Functional Model) proxy client in the cocotb simulator.
 *
 * Protocol: Newline-delimited JSON over TCP
 * The protocol is simplified compared to memory_proxy_protocol.rs to match the
 * Python simulator's format.
 */

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::io::BufRead;

use crate::types::PhysAddr;
use std::io::BufReader;
use std::io::Write;
use std::net::SocketAddr;
use std::net::TcpStream;
use std::sync::Arc;
use std::thread;
use std::thread::JoinHandle;
use std::time::Duration;

use crate::mem::PaVaMap;

/// Simplified memory access request from simulator
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SimpleMemRequest {
    /// Request type: "mem_read" or "mem_write"
    #[serde(rename = "type")]
    pub request_type: String,

    /// Channel ID (for routing responses)
    pub channel_id: u32,

    /// Physical address (as integer, not hex string)
    pub address: PhysAddr,

    /// Length of data in bytes
    pub length: usize,

    /// Data for write operations (byte array)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Vec<u8>>,

    /// Start byte index within data bus (for reads)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_byte_index: Option<usize>,

    /// First beat flag (for reads)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_first: Option<bool>,

    /// Last beat flag (for reads)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_last: Option<bool>,

    /// Request ID (string identifier)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
}

/// Simplified memory access response to simulator
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SimpleMemResponse {
    /// Response type: "mem_read_response" or "mem_write_response"
    #[serde(rename = "type")]
    pub response_type: String,

    /// Channel ID (same as request)
    pub channel_id: u32,

    /// Data for read operations (byte array)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Vec<u8>>,

    /// Start byte index (echoed from request)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_byte_index: Option<usize>,

    /// First beat flag (echoed from request)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_first: Option<bool>,

    /// Last beat flag (echoed from request)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_last: Option<bool>,

    /// Request ID (echoed from request for debugging)
    /// NOTE: Do NOT use skip_serializing_if to ensure this field is always present in JSON
    pub request_id: Option<String>,
}

const READ_TIMEOUT: Duration = Duration::from_secs(1);

/// Simple memory proxy client
pub(crate) struct SimpleTcpClient {
    stream: TcpStream,
    reader: BufReader<TcpStream>,
}

impl SimpleTcpClient {
    /// Connect to TCP server with retry logic for simulator startup
    ///
    /// In simulation mode, the RTL simulator may take time to start.
    /// This function implements exponential backoff retry to wait for
    /// the simulator to become available.
    ///
    /// Configuration via environment variables:
    /// - `SIM_TCP_RETRY_TIMEOUT_SECS`: Total timeout in seconds (default: 300 = 5 minutes)
    /// - `SIM_TCP_RETRY_INITIAL_MS`: Initial retry delay in milliseconds (default: 100)
    /// - `SIM_TCP_RETRY_MAX_MS`: Maximum retry delay in milliseconds (default: 2000)
    /// - `SIM_TCP_RETRY_LOG_INTERVAL_SECS`: Log interval in seconds (default: 5)
    pub(crate) fn new(addr: SocketAddr) -> Result<Self, std::io::Error> {
        use std::time::Instant;

        log::info!(
            "new tcp client with pid {}, addr is: {}",
            std::process::id(),
            addr
        );
        // Load retry configuration: env vars override hardcoded defaults
        let timeout_secs = std::env::var("SIM_TCP_RETRY_TIMEOUT_SECS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(300); // Default: 5 minutes

        let initial_delay_ms = std::env::var("SIM_TCP_RETRY_INITIAL_MS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(100); // Default: 100ms

        let max_delay_ms = std::env::var("SIM_TCP_RETRY_MAX_MS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(2000); // Default: 2000ms (2 seconds)

        let log_interval_secs = std::env::var("SIM_TCP_RETRY_LOG_INTERVAL_SECS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(5); // Default: 5 seconds

        let total_timeout = Duration::from_secs(timeout_secs);
        let log_interval = Duration::from_secs(log_interval_secs);

        let start_time = Instant::now();
        let mut last_log_time = start_time;
        let mut attempt = 0;
        let mut delay = Duration::from_millis(initial_delay_ms);

        // Retry loop with exponential backoff
        let stream = loop {
            attempt += 1;

            match TcpStream::connect(addr) {
                Ok(stream) => {
                    log::info!(
                        "Connected to simulator at {} after {} attempts ({:.2}s)",
                        addr,
                        attempt,
                        start_time.elapsed().as_secs_f64()
                    );
                    break stream;
                }
                Err(e) => {
                    let elapsed = start_time.elapsed();

                    // Check if total timeout exceeded
                    if elapsed >= total_timeout {
                        log::error!(
                            "Failed to connect to simulator at {} after {:.2}s: timeout",
                            addr,
                            elapsed.as_secs_f64()
                        );
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::TimedOut,
                            format!("Timeout connecting to simulator at {}: {}", addr, e),
                        ));
                    }

                    // Log on first attempt and periodically every N seconds
                    if attempt == 1 {
                        log::info!(
                            "Waiting for simulator at {} (will retry for {:.0}s)...",
                            addr,
                            total_timeout.as_secs_f64()
                        );
                        last_log_time = start_time;
                    } else if elapsed.saturating_sub(last_log_time.elapsed()) >= log_interval {
                        log::info!(
                            "Still waiting for simulator at {} ({:.1}s elapsed, {} attempts)...",
                            addr,
                            elapsed.as_secs_f64(),
                            attempt
                        );
                        last_log_time = Instant::now();
                    }

                    // Exponential backoff with cap
                    let remaining = total_timeout.saturating_sub(elapsed);
                    let sleep_duration = delay.min(remaining);

                    if sleep_duration.is_zero() {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::TimedOut,
                            format!("Timeout connecting to simulator at {}", addr),
                        ));
                    }

                    thread::sleep(sleep_duration);
                    delay = (delay * 2).min(Duration::from_millis(max_delay_ms));
                }
            }
        };

        stream.set_nodelay(true)?;
        // 用于超时心跳包 TODO 可能需要调整参数
        stream.set_read_timeout(Some(READ_TIMEOUT))?;

        let stream_for_reader = stream.try_clone()?;
        let reader = BufReader::new(stream_for_reader);

        Ok(Self { stream, reader })
    }

    pub(crate) fn get_request(&mut self) -> Result<SimpleMemRequest, std::io::Error> {
        let mut line = String::new();
        let bytes_read = self.reader.read_line(&mut line)?;

        // 检查是否读取到数据
        if bytes_read == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "Connection closed",
            ));
        }

        let trimmed = line.trim();

        log::trace!("Received request line: {}", trimmed);
        let request: SimpleMemRequest = serde_json::from_str(trimmed)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        Ok(request)
    }

    pub(crate) fn send_response(
        &mut self,
        response: &SimpleMemResponse,
    ) -> Result<(), std::io::Error> {
        let response_json = serde_json::to_string(response)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        self.stream.write_all(response_json.as_bytes())?;
        self.stream.write_all(b"\n")?;
        // TODO 应该有，但是没用
        self.stream.flush()?;
        log::trace!(
            "Sent response: type={}, channel={}",
            response.response_type,
            response.channel_id
        );
        Ok(())
    }
}

// TODO 与 CSR UDP内存请求整合
// TODO 还可以继续优化处理逻辑，一次性读取或者写入更多的字节，可以等到使用async 的时候再说

/// Simulation-mode memory proxy client for emulating PCIe DMA operations.
///
/// # WARNING: Behavioral Differences from Real PCIe Hardware
///
/// This proxy simulates PCIe DMA memory access via TCP/UDP in simulation mode,
/// but its behavior **fundamentally differs** from real PCIe bus mastering:
pub(crate) struct SimpleMemoryProxyClient {
    tcp_client: SimpleTcpClient,
    pa_va_map: Arc<RwLock<PaVaMap>>,
}

// TMP TODO
const SHM_START_ADDR: usize = 0x7f7e_8e60_0000;
const HEAP_START_ADDR_OFFSET: usize = 1024 * 1024 * 192;

impl SimpleMemoryProxyClient {
    pub(crate) fn new(tcp_client: SimpleTcpClient, pa_va_map: Arc<RwLock<PaVaMap>>) -> Self {
        // 启动处理线程
        Self {
            tcp_client,
            pa_va_map,
        }
    }

    pub(crate) fn start_processing(mut self) -> JoinHandle<()> {
        thread::spawn(move || loop {
            if let Err(e) = self.process_request() {
                log::error!("Error processing memory proxy request: {}", e);
                // 连接断开或错误，退出线程
                break;
            }
        })
    }

    pub(crate) fn process_request(&mut self) -> Result<(), std::io::Error> {
        let request = self.tcp_client.get_request();
        let request = match request {
            Ok(req) => req,
            Err(e) => {
                // TODO 如果是超时错误，发送心跳包，一点也不重要，需要删除
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut
                {
                    self.tcp_client.stream.write_all(b"\n")?;
                    self.tcp_client.stream.flush()?;
                    log::trace!("Heartbeat sent (read timeout)");
                    return Ok(());
                } else {
                    // 其他错误，返回错误退出线程
                    log::error!("Failed to get request: {}", e);
                    return Err(e);
                }
            }
        };

        log::trace!("Processing request: {:?}", request.request_id);

        // let request = self.tcp_client.get_request()?;

        match request.request_type.as_str() {
            "mem_read" => {
                let response = self.handle_read_request(request);
                self.tcp_client.send_response(&response)?;
            }
            "mem_write" => {
                self.handle_write_request(request);
                // No response for write operations
            }
            _ => {
                panic!("Unknown request type: {}", request.request_type);
            }
        }

        Ok(())
    }

    #[allow(unsafe_code)]
    pub(crate) fn handle_read_request(&self, req: SimpleMemRequest) -> SimpleMemResponse {
        log::trace!(
            "Received mem_read request: channel_id={}, address={:#x}, length={}, request_id={:?}",
            req.channel_id,
            req.address,
            req.length,
            req.request_id
        );

        let pa_va_map = self.pa_va_map.read();
        let (vir_addr, remain_len) = pa_va_map.lookup(req.address).unwrap();

        assert!(
            remain_len >= req.length,
            "Not enough contiguous memory for read request"
        );
        let vir_addr = vir_addr.as_ptr::<u8>();

        let mut data = Vec::with_capacity(req.length);

        // TODO 可能需要加上 fence！！！！！
        for i in 0..req.length {
            unsafe {
                let byte = vir_addr.add(i).read_volatile();
                data.push(byte);
            }
        }
        log::trace!("Read byte at va {:?}: {:?}", vir_addr, data);

        SimpleMemResponse {
            response_type: "mem_read_response".to_string(),
            channel_id: req.channel_id,
            data: Some(data),
            start_byte_index: req.start_byte_index,
            is_first: req.is_first,
            is_last: req.is_last,
            request_id: req.request_id.clone(), // Echo request_id for debugging
        }
    }

    #[allow(unsafe_code)]
    pub(crate) fn handle_write_request(&self, req: SimpleMemRequest) {
        log::trace!(
            "Received mem_write request: channel_id={}, address={:#x}, length={}",
            req.channel_id,
            req.address,
            req.length
        );

        let pa_va_map = self.pa_va_map.read();
        let (vir_addr, remain_len) = pa_va_map.lookup(req.address).unwrap();

        assert!(
            remain_len >= req.length,
            "Not enough contiguous memory for read request"
        );
        // log::debug!("Write byte at va {vir_addr} , len is {}", req.length);

        let vir_addr = vir_addr.as_mut_ptr::<u8>();

        for (i, byte) in req.data.unwrap().iter().enumerate() {
            unsafe {
                vir_addr.add(i).write_volatile(*byte);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_request_serialization() {
        let request = SimpleMemRequest {
            request_type: "mem_read".to_string(),
            channel_id: 0,
            address: PhysAddr::new(0x1000),
            length: 64,
            data: None,
            start_byte_index: Some(0),
            is_first: Some(true),
            is_last: Some(false),
            request_id: Some("req_0_123".to_string()),
        };

        let json = serde_json::to_string(&request).unwrap();
        let parsed: SimpleMemRequest = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.request_type, "mem_read");
        assert_eq!(parsed.channel_id, 0);
        assert_eq!(parsed.address, PhysAddr::new(0x1000));
        assert_eq!(parsed.length, 64);
    }
    #[test]
    fn test_request_deserialization() {
        // Test write request from Python proxy_pcie_bfm.py:185-191
        let write_json = r#"{
            "type": "mem_write",
            "channel_id": 0,
            "address": 4096,
            "data": [170, 187, 204, 221],
            "length": 4
        }"#;

        let write_req: SimpleMemRequest = serde_json::from_str(write_json).unwrap();
        assert_eq!(write_req.request_type, "mem_write");
        assert_eq!(write_req.channel_id, 0);
        assert_eq!(write_req.address.as_u64(), 4096);
        assert_eq!(write_req.length, 4);
        assert_eq!(write_req.data.unwrap(), vec![0xaa, 0xbb, 0xcc, 0xdd]);

        // Test read request from Python proxy_pcie_bfm.py:253-262
        let read_json = r#"{
            "type": "mem_read",
            "channel_id": 1,
            "address": 8192,
            "length": 32,
            "start_byte_index": 0,
            "is_first": true,
            "is_last": false,
            "request_id": "req_1_123"
        }"#;

        let read_req: SimpleMemRequest = serde_json::from_str(read_json).unwrap();
        assert_eq!(read_req.request_type, "mem_read");
        assert_eq!(read_req.channel_id, 1);
        assert_eq!(read_req.address.as_u64(), 8192);
        assert_eq!(read_req.length, 32);
        assert_eq!(read_req.start_byte_index, Some(0));
        assert_eq!(read_req.is_first, Some(true));
        assert_eq!(read_req.is_last, Some(false));
        assert_eq!(read_req.request_id, Some("req_1_123".to_string()));
    }

    #[test]
    fn test_response_serialization() {
        let response = SimpleMemResponse {
            response_type: "mem_read_response".to_string(),
            channel_id: 1,
            data: Some(vec![0xaa, 0xbb, 0xcc, 0xdd]),
            start_byte_index: Some(4),
            is_first: Some(false),
            is_last: Some(true),
            request_id: Some("req_1_123".to_string()),
        };

        let json = serde_json::to_string(&response).unwrap();
        let parsed: SimpleMemResponse = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.response_type, "mem_read_response");
        assert_eq!(parsed.channel_id, 1);
        assert_eq!(parsed.data.unwrap(), vec![0xaa, 0xbb, 0xcc, 0xdd]);
        assert_eq!(parsed.request_id.unwrap(), "req_1_123");
    }
    #[test]
    fn test_response_deserialization() {
        // Test read response that Python expects (proxy_pcie_bfm.py:328-350)
        let response_json = r#"{
            "type": "mem_read_response",
            "channel_id": 0,
            "data": [17, 34, 51, 68, 85, 102, 119, 136],
            "start_byte_index": 4,
            "is_first": false,
            "is_last": true
        }"#;

        let response: SimpleMemResponse = serde_json::from_str(response_json).unwrap();
        assert_eq!(response.response_type, "mem_read_response");
        assert_eq!(response.channel_id, 0);
        assert_eq!(
            response.data.unwrap(),
            vec![0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]
        );
        assert_eq!(response.start_byte_index, Some(4));
        assert_eq!(response.is_first, Some(false));
        assert_eq!(response.is_last, Some(true));
    }

    #[test]
    fn test_write_request_python_format() {
        // Exact format from proxy_pcie_bfm.py:185-191 with byte array
        let request = SimpleMemRequest {
            request_type: "mem_write".to_string(),
            channel_id: 0,
            address: PhysAddr::new(0x8000),
            length: 8,
            data: Some(vec![0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]),
            start_byte_index: None,
            is_first: None,
            is_last: None,
            request_id: None,
        };

        let json = serde_json::to_string(&request).unwrap();
        let parsed: SimpleMemRequest = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.request_type, "mem_write");
        assert_eq!(parsed.channel_id, 0);
        assert_eq!(parsed.address.as_u64(), 0x8000);
        assert_eq!(parsed.length, 8);
        assert_eq!(parsed.data.unwrap().len(), 8);

        // Verify optional fields are None (not serialized)
        let json_obj: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert!(json_obj.get("start_byte_index").is_none());
        assert!(json_obj.get("is_first").is_none());
        assert!(json_obj.get("is_last").is_none());
        assert!(json_obj.get("request_id").is_none());
    }

    #[test]
    fn test_read_request_python_format() {
        // Exact format from proxy_pcie_bfm.py:253-262 with all metadata
        let request = SimpleMemRequest {
            request_type: "mem_read".to_string(),
            channel_id: 2,
            address: PhysAddr::new(0x10000),
            length: 64,
            data: None,
            start_byte_index: Some(4),
            is_first: Some(false),
            is_last: Some(true),
            request_id: Some("req_2_456".to_string()),
        };

        let json = serde_json::to_string(&request).unwrap();
        let parsed: SimpleMemRequest = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.request_type, "mem_read");
        assert_eq!(parsed.channel_id, 2);
        assert_eq!(parsed.address.as_u64(), 0x10000);
        assert_eq!(parsed.length, 64);
        assert_eq!(parsed.start_byte_index, Some(4));
        assert_eq!(parsed.is_first, Some(false));
        assert_eq!(parsed.is_last, Some(true));
        assert_eq!(parsed.request_id.unwrap(), "req_2_456");

        // Verify data field is not in JSON
        let json_obj: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert!(json_obj.get("data").is_none());
    }

    #[test]
    fn test_read_response_with_data() {
        // Response matching Python's expected format (proxy_pcie_bfm.py:328-350)
        let response = SimpleMemResponse {
            response_type: "mem_read_response".to_string(),
            channel_id: 1,
            data: Some(vec![0xde, 0xad, 0xbe, 0xef]),
            start_byte_index: Some(0),
            is_first: Some(true),
            is_last: Some(true),
            request_id: Some("req_1_456".to_string()),
        };

        let json = serde_json::to_string(&response).unwrap();
        let parsed: SimpleMemResponse = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.response_type, "mem_read_response");
        assert_eq!(parsed.channel_id, 1);
        assert_eq!(parsed.data.unwrap(), vec![0xde, 0xad, 0xbe, 0xef]);
        assert_eq!(parsed.start_byte_index, Some(0));
        assert_eq!(parsed.is_first, Some(true));
        assert_eq!(parsed.is_last, Some(true));
        assert_eq!(parsed.request_id.unwrap(), "req_1_456");
    }

    #[test]
    fn test_optional_fields_omitted() {
        // Test that skip_serializing_if works correctly
        let request = SimpleMemRequest {
            request_type: "mem_write".to_string(),
            channel_id: 0,
            address: PhysAddr::new(0x1000),
            length: 4,
            data: Some(vec![0xff, 0xee, 0xdd, 0xcc]),
            start_byte_index: None,
            is_first: None,
            is_last: None,
            request_id: None,
        };

        let json = serde_json::to_string(&request).unwrap();

        // Parse as generic JSON to check field presence
        let json_obj: serde_json::Value = serde_json::from_str(&json).unwrap();

        // Required fields should be present
        assert!(json_obj.get("type").is_some());
        assert!(json_obj.get("channel_id").is_some());
        assert!(json_obj.get("address").is_some());
        assert!(json_obj.get("length").is_some());
        assert!(json_obj.get("data").is_some());

        // Optional None fields should be omitted
        assert!(json_obj.get("start_byte_index").is_none());
        assert!(json_obj.get("is_first").is_none());
        assert!(json_obj.get("is_last").is_none());
        assert!(json_obj.get("request_id").is_none());
    }

    #[test]
    fn test_newline_delimited_protocol() {
        // Test the newline-delimited JSON protocol used by Python (proxy_pcie_bfm.py:421)
        let request1 = SimpleMemRequest {
            request_type: "mem_write".to_string(),
            channel_id: 0,
            address: PhysAddr::new(0x1000),
            length: 4,
            data: Some(vec![0x11, 0x22, 0x33, 0x44]),
            start_byte_index: None,
            is_first: None,
            is_last: None,
            request_id: None,
        };

        let request2 = SimpleMemRequest {
            request_type: "mem_read".to_string(),
            channel_id: 1,
            address: PhysAddr::new(0x2000),
            length: 8,
            data: None,
            start_byte_index: Some(0),
            is_first: Some(true),
            is_last: Some(false),
            request_id: Some("req_1_0".to_string()),
        };

        // Simulate the wire protocol format
        let mut buffer = String::new();
        buffer.push_str(&serde_json::to_string(&request1).unwrap());
        buffer.push('\n');
        buffer.push_str(&serde_json::to_string(&request2).unwrap());
        buffer.push('\n');

        // Parse line by line as the Python code does (proxy_pcie_bfm.py:440)
        let lines: Vec<&str> = buffer.lines().collect();
        assert_eq!(lines.len(), 2);

        let parsed1: SimpleMemRequest = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(parsed1.request_type, "mem_write");
        assert_eq!(parsed1.channel_id, 0);
        assert_eq!(parsed1.address, PhysAddr::new(0x1000));

        let parsed2: SimpleMemRequest = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(parsed2.request_type, "mem_read");
        assert_eq!(parsed2.channel_id, 1);
        assert_eq!(parsed2.address, PhysAddr::new(0x2000));
        assert_eq!(parsed2.request_id.unwrap(), "req_1_0");
    }

    #[test]
    fn test_address_as_integer_not_hex() {
        // Verify addresses are serialized as integers, not hex strings
        // This matches Python's format (proxy_pcie_bfm.py:188 uses integer address)
        let request = SimpleMemRequest {
            request_type: "mem_write".to_string(),
            channel_id: 0,
            address: PhysAddr::new(0xdead_beef),
            length: 4,
            data: Some(vec![0x00, 0x01, 0x02, 0x03]),
            start_byte_index: None,
            is_first: None,
            is_last: None,
            request_id: None,
        };

        let json = serde_json::to_string(&request).unwrap();

        // Address should be a JSON number, not a string
        let json_obj: serde_json::Value = serde_json::from_str(&json).unwrap();
        let address = json_obj.get("address").unwrap();
        assert!(address.is_u64());
        assert_eq!(address.as_u64().unwrap(), 0xdead_beef);
        // Should not be a hex string like "0xdeadbeef"
        assert!(!address.is_string());
    }

    #[test]
    fn test_channel_id_routing() {
        // Test that channel_id is correctly preserved for response routing
        // Python uses this for requester_read_queue routing (proxy_pcie_bfm.py:444)
        for channel_id in 0..4 {
            let response = SimpleMemResponse {
                response_type: "mem_read_response".to_string(),
                channel_id,
                data: Some(vec![0xaa; 16]),
                start_byte_index: Some(0),
                is_first: Some(true),
                is_last: Some(true),
                request_id: Some(format!("req_{}_0", channel_id)),
            };

            let json = serde_json::to_string(&response).unwrap();
            let parsed: SimpleMemResponse = serde_json::from_str(&json).unwrap();

            assert_eq!(parsed.channel_id, channel_id);
            assert_eq!(parsed.request_id.unwrap(), format!("req_{}_0", channel_id));
        }
    }

    #[test]
    fn test_metadata_echo_behavior() {
        // Test that read responses correctly echo metadata fields
        // Python expects start_byte_index, is_first, is_last to be echoed (proxy_pcie_bfm.py:332-333)
        let test_cases = vec![(0, true, false), (4, false, false), (8, false, true)];

        for (idx, (start_byte_index, is_first, is_last)) in test_cases.iter().enumerate() {
            let response = SimpleMemResponse {
                response_type: "mem_read_response".to_string(),
                channel_id: 0,
                data: Some(vec![0x00; 32]),
                start_byte_index: Some(*start_byte_index),
                is_first: Some(*is_first),
                is_last: Some(*is_last),
                request_id: Some(format!("req_0_{}", idx)),
            };

            let json = serde_json::to_string(&response).unwrap();
            let parsed: SimpleMemResponse = serde_json::from_str(&json).unwrap();

            assert_eq!(parsed.start_byte_index, Some(*start_byte_index));
            assert_eq!(parsed.is_first, Some(*is_first));
            assert_eq!(parsed.is_last, Some(*is_last));
        }
    }
}
