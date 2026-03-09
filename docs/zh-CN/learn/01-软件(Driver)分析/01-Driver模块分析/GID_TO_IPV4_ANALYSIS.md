# GID/DGID 到 IPv4 地址转换分析

## 概述

本文档分析 open-rdma-driver 中如何处理 RDMA QP 的 GID (Global Identifier) 和 DGID (Destination GID)，以及如何将其转换为 IPv4 地址用于网络通信。

## 1. GID 格式和 IPv4 映射

### 1.1 IPv4-mapped IPv6 地址格式

在 RoCE (RDMA over Converged Ethernet) 中，GID 是一个 128 位（16 字节）的标识符，基于 IPv6 格式。对于 IPv4 网络，使用 **IPv4-mapped IPv6 地址** 格式：

```
格式：::ffff:a.b.c.d

二进制表示（16 字节）：
[0-9]:  0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00  (前10字节为0)
[10-11]: 0xFF 0xFF                                           (第11-12字节为0xFF)
[12-15]: a    b    c    d                                    (最后4字节为IPv4地址)
```

### 1.2 测试程序中的设置

在测试程序（如 `write_imm.c`）中，设置 DGID 的代码：

```c
// write_imm.c:180-188
uint32_t ipv4_addr = 0x1122330A;  // 17.34.51.10
attr.ah_attr.grh.dgid.raw[10] = 0xFF;
attr.ah_attr.grh.dgid.raw[11] = 0xFF;
attr.ah_attr.grh.dgid.raw[12] = (ipv4_addr >> 24) & 0xFF;  // 0x11 = 17
attr.ah_attr.grh.dgid.raw[13] = (ipv4_addr >> 16) & 0xFF;  // 0x22 = 34
attr.ah_attr.grh.dgid.raw[14] = (ipv4_addr >> 8) & 0xFF;   // 0x33 = 51
attr.ah_attr.grh.dgid.raw[15] = ipv4_addr & 0xFF;          // 0x0A = 10
```

## 2. 驱动中的 GID 提取和转换

### 2.1 从 QP 属性中提取 DGID

**文件位置**：`rust-driver/src/rdma_utils/types.rs:488-496`

```rust
let dest_qp_ip = if attr_mask & ibv_qp_attr_mask::IBV_QP_AV.0 != 0 {
    // 从 ah_attr.grh.dgid 中提取 16 字节的 GID
    let gid = unsafe { attr.ah_attr.grh.dgid.raw };
    info!("gid: {:x}", u128::from_be_bytes(gid));

    // 检查是否是 IPv4-mapped IPv6 格式
    // 前10字节为0，第11-12字节为0xFF
    let is_ipv4_mapped =
        gid[..10].iter().all(|&x| x == 0) && gid[10] == 0xFF && gid[11] == 0xFF;

    // 如果是 IPv4 映射格式，提取最后4字节作为 IPv4 地址
    is_ipv4_mapped.then(|| Ipv4Addr::new(gid[12], gid[13], gid[14], gid[15]))
} else {
    None
};
```

**关键逻辑**：
1. 检查 `IBV_QP_AV` 标志位，确认用户设置了 Address Vector
2. 从 `attr.ah_attr.grh.dgid.raw` 提取 16 字节 GID
3. 验证是否符合 IPv4-mapped 格式
4. 提取最后 4 字节构造 `Ipv4Addr`

### 2.2 存储到 QP 属性

**文件位置**：`rust-driver/src/verbs/ctx.rs:390-408`

```rust
// 获取当前 QP 的 IP 和新属性中的 IP
let current_ip = (current.dqp_ip != 0).then_some(current.dqp_ip);
let attr_ip = attr.dest_qp_ip().map(Ipv4Addr::to_bits);

// 优先使用新设置的 IP，否则保留当前 IP
let ip_addr = attr_ip.or(current_ip).unwrap_or(0);

// 更新 QP 表中的目标 IP
current.dqp_ip = ip_addr;
```

**QpAttr 结构**：`rust-driver/src/rdma_utils/types.rs:388-409`

```rust
pub(crate) struct QpAttr {
    pub(crate) qp_type: u8,
    pub(crate) qpn: u32,
    pub(crate) dqpn: u32,
    pub(crate) ip: u32,        // 本地 IP（u32 大端格式）
    pub(crate) dqp_ip: u32,    // 目标 IP（u32 大端格式）
    pub(crate) mac_addr: u64,
    pub(crate) pmtu: u8,
    pub(crate) access_flags: u8,
    pub(crate) send_cq: Option<u32>,
    pub(crate) recv_cq: Option<u32>,
}
```

## 3. IP 地址的使用场景

### 3.1 建立 TCP 通道（Post Recv）

**文件位置**：`rust-driver/src/verbs/ctx.rs:421-427`

```rust
if qp.dqpn != 0 && qp.dqp_ip != 0 && self.post_recv_tx_table.get_qp_mut(qpn).is_none() {
    let dqp_ip = Ipv4Addr::from_bits(qp.dqp_ip);
    debug!("update_qp get dqp_ip={dqp_ip:?}");
    log::info!("qp local ip is {},remote ip is {}", qp.ip, qp.dqp_ip);

    // 使用本地 IP 和远程 IP 建立 TCP 通道
    let (tx, rx) = post_recv_channel::<TcpChannel>(
        qp.ip.into(),      // 本地 IP
        qp.dqp_ip.into(),  // 远程 IP
        qpn,
        qp.dqpn
    )?;
    self.post_recv_tx_table.insert(qpn, tx);
}
```

### 3.2 发送描述符中的目标 IP

**文件位置**：`rust-driver/src/descriptors/send.rs:28-33`

```rust
#[bitsize(64)]
struct SendQueueReqDescSeg0Chunk2 {
    pub dqp_ip: u32,  // 目标 QP 的 IPv4 地址
    pub rkey: u32,
}
```

发送描述符会携带目标 IP，硬件使用此 IP 构造 RoCE 数据包。

### 3.3 构建网络数据包（ACK/NAK）

**文件位置**：`rust-driver/src/workers/ack_responder.rs:52-54`

```rust
let qp_attr = self.qp_table.get_qp(task.qpn()).expect("invalid qpn");
// 使用 QP 的本地 IP 作为源 IP，目标 IP 作为目标 IP
let frame_builder = AckFrameBuilder::new(qp_attr.ip, qp_attr.dqp_ip, qp_attr.dqpn);
```

**文件位置**：`rust-driver/src/workers/ack_responder.rs:140-177`

```rust
fn build_ethernet_frame(
    src_ip: u32,
    dst_ip: u32,
    src_mac: MacAddr,
    dst_mac: MacAddr,
    payload: &[u8],
) -> Vec<u8> {
    // ...

    // 设置 IPv4 包头
    ipv4_packet.set_source(Ipv4Addr::from_bits(src_ip));      // 使用 QP 的本地 IP
    ipv4_packet.set_destination(Ipv4Addr::from_bits(dst_ip)); // 使用 QP 的目标 IP

    // ...
}
```

## 4. 完整的数据流

```
用户空间应用
    │
    │ ibv_modify_qp(attr.ah_attr.grh.dgid = ::ffff:17.34.51.10)
    │
    ▼
Rust 驱动: types.rs
    │
    │ 1. 提取 dgid.raw[0..16]
    │ 2. 检查 IPv4-mapped 格式
    │ 3. 提取 gid[12..16] 作为 IPv4
    │
    ▼
Rust 驱动: ctx.rs
    │
    │ 4. 存储到 QpAttr.dqp_ip (u32)
    │ 5. 建立 TCP 通道 (qp.ip → qp.dqp_ip)
    │
    ▼
硬件发送路径
    │
    │ 6a. 发送描述符携带 dqp_ip
    │     → 硬件构造 RoCE 包
    │
    ▼
软件 ACK 路径
    │
    │ 6b. AckFrameBuilder 使用 src_ip/dst_ip
    │     → 构造 Ethernet/IPv4/UDP 包
    │
    ▼
网络传输 (UDP port 4791)
```

## 5. 关键代码位置总结

| 功能 | 文件路径 | 行号 | 说明 |
|------|---------|------|------|
| GID 到 IPv4 转换 | `rust-driver/src/rdma_utils/types.rs` | 488-496 | 从 DGID 提取 IPv4 地址 |
| 存储目标 IP | `rust-driver/src/verbs/ctx.rs` | 390-408 | 更新 QpAttr.dqp_ip |
| 建立通道 | `rust-driver/src/verbs/ctx.rs` | 421-427 | 使用 IP 建立 TCP 通道 |
| 发送描述符 | `rust-driver/src/descriptors/send.rs` | 28-33 | 描述符中的 dqp_ip 字段 |
| ACK 包构建 | `rust-driver/src/workers/ack_responder.rs` | 140-177 | 构造带源/目标 IP 的以太网帧 |

## 6. 测试验证

可以通过以下方式验证 IP 提取：

```bash
# 运行驱动时查看日志
RUST_LOG=info cargo run

# 日志输出示例：
# [INFO] gid: 0xffffffffffff1122330a
# [INFO] qp local ip is 285212682, remote ip is 285212682
```

其中 `285212682 (decimal) = 0x1122330A (hex) = 17.34.51.10 (dotted)`

## 7. 注意事项

1. **大端序**：IP 地址以大端序（网络字节序）存储在 u32 中
2. **IPv6 支持**：当前驱动仅支持 IPv4-mapped 格式，纯 IPv6 GID 会被忽略
3. **验证**：驱动会验证 GID 的前 10 字节和第 11-12 字节，确保符合 IPv4-mapped 格式
4. **默认值**：如果 GID 不是 IPv4-mapped 格式，`dest_qp_ip` 为 `None`，dqp_ip 保持为 0

## 8. 相关 RDMA 规范

- **IBA (InfiniBand Architecture)**：定义 GID 为 128 位全局标识符
- **RoCE v2**：使用 IPv4/IPv6 作为网络层，GID 映射到 IP 地址
- **RFC 4291**：定义 IPv4-mapped IPv6 地址格式 `::ffff:a.b.c.d`
