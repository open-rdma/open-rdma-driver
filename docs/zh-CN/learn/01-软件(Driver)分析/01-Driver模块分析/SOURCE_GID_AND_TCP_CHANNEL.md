# 源 GID/IP 来源与 TCP 通道分析

## 概述

本文档分析 open-rdma-driver 中 **源 GID/IP（本地 IP）** 的来源，以及如何使用本地 IP 和远程 IP 建立额外的 TCP 通道用于传输 Post Recv 信息。

---

## 1. 源 IP 的获取流程

### 1.1 从网络接口读取配置

**文件位置**：`rust-driver/src/net/reader.rs:22-47`

```rust
pub(crate) fn read() -> NetworkConfig {
    // 1. 查找名为 "blue0" 的网络接口
    let interface = default_net::get_interfaces()
        .into_iter()
        .find(|x| x.name == BLUE_RDMA_NETDEV_INTERFACE_NAME)  // "blue0"
        .expect("blue-rdma netdev not present");

    // 2. 提取接口的 IPv4 地址
    let ip = interface
        .ipv4
        .into_iter()
        .next()
        .expect("no ipv4 address configured");
    let ip = Ipv4Network::new(ip.addr, ip.prefix_len)
        .expect("invalid address format");

    // 3. 提取网关和 MAC 地址
    let gateway = interface.gateway.map(|x| match x.ip_addr {
        IpAddr::V4(ip) => ip,
        IpAddr::V6(ip) => unreachable!(),
    });
    let mac = interface.mac_addr.expect("no mac address configured");
    let mac = MacAddress(mac.octets());

    // 4. 返回网络配置
    NetworkConfig {
        ip,           // 本地 IP（Ipv4Network 类型）
        peer_ip: 0.into(),
        gateway,
        mac,
    }
}
```

**关键常量**：`rust-driver/src/constants.rs:45`
```rust
pub(crate) const BLUE_RDMA_NETDEV_INTERFACE_NAME: &str = "blue0";
```

### 1.2 初始化 QP 表时设置本地 IP

**文件位置**：`rust-driver/src/verbs/ctx.rs:108-134`

```rust
pub(crate) fn initialize(device: H, config: DeviceConfig) -> Result<Self> {
    // 1. 读取网络配置
    let net_config = NetConfigReader::read();

    // ... (其他初始化代码)

    // 2. 创建 QP 表，所有 QP 默认使用相同的本地 IP
    let qp_attr_table = QpTableShared::new_with(|| {
        QpAttr::new_with_ip(net_config.ip.ip().to_bits())  // 将 Ipv4Addr 转为 u32
    });

    // ...
}
```

**QpAttr 构造函数**：`rust-driver/src/rdma_utils/types.rs:402-408`

```rust
impl QpAttr {
    pub(crate) fn new_with_ip(ip: u32) -> Self {
        Self {
            ip,  // 存储为 u32 格式（大端序）
            ..Default::default()
        }
    }
}
```

### 1.3 配置网络接口的方法

有两种方式配置 `blue0` 接口的 IP 地址：

#### 方法 1：手动配置（推荐用于测试）
```bash
# 创建 blue0 接口（如果不存在）
sudo ip link add blue0 type dummy

# 设置 IP 地址
sudo ip addr add 17.34.51.10/24 dev blue0

# 启动接口
sudo ip link set blue0 up

# 验证配置
ip addr show blue0
```

#### 方法 2：通过 sysfs 读取 GID（备用）

**文件位置**：`rust-driver/src/net/reader.rs:67-90`

```rust
pub(crate) fn read_ip_sysfs() -> io::Result<Option<u32>> {
    // 从 /sys/class/infiniband/blue-rdma/ports/1/gids/0 读取 GID
    let gids = Self::read_attribute_sysfs("gids")?;

    for gid in gids.lines() {
        let bytes = gid
            .split(':')
            .map(|s| u16::from_str_radix(s, 16))
            .collect::<Result<Vec<_>, _>>()
            .expect("invalid gid format");

        assert_eq!(bytes.len(), 8, "invalid gid format");

        // 检查是否是 IPv4-mapped IPv6 格式
        if bytes[0] == 0 && bytes[1] == 0 && bytes[2] == 0
            && bytes[3] == 0 && bytes[4] == 0 && bytes[5] == 0xffff {
            // 提取最后 4 字节作为 IPv4
            let result = (u32::from(bytes[6]) << 16) | u32::from(bytes[7]);
            return Ok(Some(result));
        }
    }

    Ok(None)
}
```

---

## 2. TCP 通道的建立与使用

### 2.1 为什么需要额外的 TCP 通道？

在 RDMA 操作中，除了主数据路径（RoCE 包通过硬件/UDP 传输），还需要一个 **控制路径** 来传输 **Post Recv 队列信息**。这个控制路径使用 TCP 连接实现。

**原因**：
1. Post Recv 操作需要通知对端接收缓冲区已准备好
2. TCP 提供可靠的控制消息传输
3. 与数据路径分离，避免相互干扰

### 2.2 TCP 通道的建立时机

**文件位置**：`rust-driver/src/verbs/ctx.rs:421-429`

```rust
// 在 QP 修改时，当同时满足以下条件时建立 TCP 通道：
// 1. dqpn != 0（已设置目标 QP 号）
// 2. dqp_ip != 0（已设置目标 IP）
// 3. TCP 通道尚未建立

if qp.dqpn != 0 && qp.dqp_ip != 0 && self.post_recv_tx_table.get_qp_mut(qpn).is_none() {
    // 将 u32 转回 Ipv4Addr
    let dqp_ip = Ipv4Addr::from_bits(qp.dqp_ip);
    debug!("update_qp get dqp_ip={dqp_ip:?}");
    log::info!("qp local ip is {},remote ip is {}", qp.ip, qp.dqp_ip);

    // 建立 TCP 通道：本地 IP -> 远程 IP
    let (tx, rx) = post_recv_channel::<TcpChannel>(
        qp.ip.into(),      // 源 IP（从 blue0 接口获取）
        qp.dqp_ip.into(),  // 目标 IP（从 DGID 提取）
        qpn,               // 本地 QPN
        qp.dqpn            // 远程 QPN
    )?;

    // 保存发送端用于后续 Post Recv 操作
    self.post_recv_tx_table.insert(qpn, tx);
    // ...
}
```

### 2.3 TCP 通道的数据流

```
┌──────────────────────────────────────────────────────────┐
│                      QP 修改流程                          │
└──────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────┐
│  1. 从 DGID 提取目标 IP (dqp_ip)                         │
│     types.rs:488-496                                     │
└──────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────┐
│  2. 检查是否满足建立 TCP 通道条件                         │
│     ctx.rs:421                                           │
│     - dqpn != 0                                          │
│     - dqp_ip != 0                                        │
│     - TCP 通道未建立                                      │
└──────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────┐
│  3. 建立 TCP 连接                                         │
│     post_recv_channel<TcpChannel>(                       │
│         qp.ip,        ← 源 IP (从 blue0 获取)            │
│         qp.dqp_ip,    ← 目标 IP (从 DGID 提取)           │
│         qpn,                                             │
│         qp.dqpn                                          │
│     )                                                    │
└──────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────┐
│  4. TCP 通道用途                                          │
│     - 发送 Post Recv 队列信息                            │
│     - 通知对端接收缓冲区状态                              │
│     - 控制路径消息传输                                    │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 完整的 IP 地址流转

```
┌─────────────────────────────────────────────────────────────┐
│              1. 系统启动 - 读取本地 IP                       │
│                                                             │
│  Linux 网络接口 "blue0"                                     │
│  ├─ IP: 17.34.51.10/24                                     │
│  ├─ MAC: aa:bb:cc:dd:ee:ff                                 │
│  └─ Gateway: 17.34.51.1                                    │
│                         │                                   │
│                         ▼                                   │
│  NetConfigReader::read()                                   │
│  (reader.rs:22-47)                                         │
│                         │                                   │
│                         ▼                                   │
│  net_config.ip = Ipv4Network(17.34.51.10/24)              │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│          2. 初始化 QP 表 - 设置所有 QP 的本地 IP             │
│                                                             │
│  QpTableShared::new_with(|| {                              │
│      QpAttr::new_with_ip(                                  │
│          net_config.ip.ip().to_bits()  // 0x1122330A       │
│      )                                                     │
│  })                                                        │
│  (ctx.rs:134)                                              │
│                                                             │
│  所有 QP 的 qp.ip = 0x1122330A                             │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│         3. 用户设置 QP - 从 DGID 提取目标 IP                 │
│                                                             │
│  ibv_modify_qp(attr.ah_attr.grh.dgid = ::ffff:18.52.86.20) │
│                         │                                   │
│                         ▼                                   │
│  IbvQpAttr::new()                                          │
│  (types.rs:488-496)                                        │
│                         │                                   │
│                         ▼                                   │
│  dest_qp_ip = Ipv4Addr(18, 52, 86, 20)                    │
│                         │                                   │
│                         ▼                                   │
│  qp.dqp_ip = 0x12345414                                   │
│  (ctx.rs:408)                                              │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│            4. 建立 TCP 通道 - 连接本地与远程 IP              │
│                                                             │
│  post_recv_channel::<TcpChannel>(                          │
│      qp.ip      = 0x1122330A  (17.34.51.10) ← 源 IP        │
│      qp.dqp_ip  = 0x12345414  (18.52.86.20) ← 目标 IP      │
│      qpn,                                                  │
│      qp.dqpn                                               │
│  )                                                         │
│  (ctx.rs:427)                                              │
│                                                             │
│  建立 TCP 连接: 17.34.51.10 → 18.52.86.20                  │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│           5. 使用 TCP 通道 - 传输 Post Recv 信息             │
│                                                             │
│  ibv_post_recv() 被调用时                                   │
│      ↓                                                      │
│  通过 TCP 通道发送 RecvWr 信息到对端                         │
│      ↓                                                      │
│  对端接收并处理 Post Recv 请求                               │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 源 IP 与目标 IP 对比总结

| 属性 | 源 IP (qp.ip) | 目标 IP (qp.dqp_ip) |
|------|--------------|-------------------|
| **来源** | 从 `blue0` 网络接口读取 | 从 `ibv_qp_attr.ah_attr.grh.dgid` 提取 |
| **设置时机** | QP 表初始化时 | `ibv_modify_qp()` 调用时 |
| **存储位置** | `QpAttr.ip` (u32) | `QpAttr.dqp_ip` (u32) |
| **用途** | - TCP 通道源地址<br>- ACK 包源 IP<br>- 发送描述符中的源 IP | - TCP 通道目标地址<br>- ACK 包目标 IP<br>- 发送描述符中的目标 IP |
| **配置方法** | `sudo ip addr add 17.34.51.10/24 dev blue0` | 在 `ibv_modify_qp()` 中设置 DGID |
| **格式** | IPv4-mapped IPv6<br>`::ffff:17.34.51.10` | IPv4-mapped IPv6<br>`::ffff:18.52.86.20` |
| **示例值** | `0x1122330A` (17.34.51.10) | `0x12345414` (18.52.86.20) |

---

## 5. 关键代码位置总结

| 功能 | 文件路径 | 行号 | 说明 |
|------|---------|------|------|
| 读取网络接口配置 | `rust-driver/src/net/reader.rs` | 22-47 | 从 blue0 接口读取本地 IP |
| 初始化 QP 本地 IP | `rust-driver/src/verbs/ctx.rs` | 134 | 设置所有 QP 的默认本地 IP |
| 提取目标 IP | `rust-driver/src/rdma_utils/types.rs` | 488-496 | 从 DGID 提取远程 IP |
| 建立 TCP 通道 | `rust-driver/src/verbs/ctx.rs` | 421-429 | 使用本地/远程 IP 建立控制通道 |
| TCP 通道定义 | `rust-driver/src/net/recv_chan.rs` | - | TCP 通道实现 |

---

## 6. 实际使用示例

### 6.1 配置环境

```bash
# 服务器端（17.34.51.10）
sudo ip link add blue0 type dummy
sudo ip addr add 17.34.51.10/24 dev blue0
sudo ip link set blue0 up

# 客户端（18.52.86.20）
sudo ip link add blue0 type dummy
sudo ip addr add 18.52.86.20/24 dev blue0
sudo ip link set blue0 up
```

### 6.2 运行测试程序

```c
// 服务器端设置 DGID 指向客户端
uint32_t client_ip = 0x12345414;  // 18.52.86.20
attr.ah_attr.grh.dgid.raw[10] = 0xFF;
attr.ah_attr.grh.dgid.raw[11] = 0xFF;
attr.ah_attr.grh.dgid.raw[12] = (client_ip >> 24) & 0xFF;
attr.ah_attr.grh.dgid.raw[13] = (client_ip >> 16) & 0xFF;
attr.ah_attr.grh.dgid.raw[14] = (client_ip >> 8) & 0xFF;
attr.ah_attr.grh.dgid.raw[15] = client_ip & 0xFF;

ibv_modify_qp(qp, &attr, IBV_QP_AV | IBV_QP_STATE);
```

### 6.3 查看日志验证

```bash
RUST_LOG=info ./my_rdma_app

# 输出示例：
# [INFO] qp local ip is 285212682, remote ip is 305419284
#        (0x1122330A = 17.34.51.10)   (0x12345414 = 18.52.86.20)
```

---

## 7. 注意事项

1. **blue0 接口必须配置**：驱动启动时会查找 `blue0` 接口，如果不存在会 panic
2. **IP 地址一致性**：`blue0` 的 IP 应该与测试程序中使用的本地 IP 一致
3. **网络可达性**：本地 IP 和远程 IP 必须网络可达（至少在同一子网或有路由）
4. **TCP 端口**：TCP 通道使用动态端口，需确保防火墙允许
5. **大端序存储**：所有 IP 地址在驱动中以 u32 大端序格式存储

---

## 8. 故障排查

### 问题：驱动启动失败，提示 "blue-rdma netdev not present"

**解决方法**：
```bash
sudo ip link add blue0 type dummy
sudo ip addr add <your_ip>/24 dev blue0
sudo ip link set blue0 up
```

### 问题：TCP 通道建立失败

**排查步骤**：
1. 检查 `blue0` 接口 IP：`ip addr show blue0`
2. 检查目标 IP 可达性：`ping <remote_ip>`
3. 查看驱动日志：`RUST_LOG=debug ./app`
4. 确认 DGID 设置正确

### 问题：Post Recv 信息未传输

**排查步骤**：
1. 确认 `dqpn != 0` 和 `dqp_ip != 0`
2. 检查是否调用了 `ibv_modify_qp()` 设置 DGID
3. 查看 TCP 连接状态：`netstat -tnp | grep <qpn>`

---

## 9. 相关文档

- [GID_TO_IPV4_ANALYSIS.md](./GID_TO_IPV4_ANALYSIS.md) - DGID 到目标 IP 的转换
- `rust-driver/src/net/recv_chan.rs` - TCP 通道实现细节
- `rust-driver/src/net/reader.rs` - 网络配置读取实现
