# RCCL GID选择机制与默认IP问题分析

## 问题背景

在运行RCCL时，驱动在QP转换到RTS状态时会触发警告日志：

```rust
log::warn!("update qpn {} to RTS with default ip {}", qpn, ip);
```

位置：`rust-driver/src/verbs/ctx.rs:514`

这表明RCCL在modify_qp到RTS状态时，没有提供目标IP地址信息，导致驱动使用了本地网卡配置的默认IP。

## 一、RDMA QP状态转换标准流程

### 1.1 QP状态机

RDMA QP遵循标准的状态转换流程：

```
RESET → INIT → RTR → RTS
```

- **RESET**: 初始状态，QP资源已分配但未配置
- **INIT**: 初始化完成，可以post recv操作
- **RTR (Ready to Receive)**: 准备接收数据，配置对端信息
- **RTS (Ready to Send)**: 准备发送数据，配置发送参数

### 1.2 各阶段配置的参数

**INIT阶段**：
- `pkey_index`: 分区键索引
- `port_num`: 端口号
- `qp_access_flags`: 访问权限

**RTR阶段**（设置接收所需参数）：
- `dest_qp_num`: 目标QP号
- `ah_attr`: **地址句柄属性**（包含目标GID/LID）
- `path_mtu`: 路径MTU
- `rq_psn`: 接收队列PSN
- `max_dest_rd_atomic`: 最大目标读原子操作数
- `min_rnr_timer`: RNR重试定时器

**RTS阶段**（设置发送所需参数）：
- `timeout`: 超时时间
- `retry_cnt`: 重试次数
- `rnr_retry`: RNR重试次数
- `sq_psn`: 发送队列PSN
- `max_rd_atomic`: 最大读原子操作数

**关键点**：按照标准RDMA语义，**地址信息在RTR阶段设置一次后，RTS阶段不需要重新设置**。

## 二、IBV_QP_AV标志位详解

### 2.1 定义

```c
enum ibv_qp_attr_mask {
    IBV_QP_STATE     = 1 << 0,   // 0x0001
    IBV_QP_AV        = 1 << 7,   // 0x0080 - Address Vector
    IBV_QP_PATH_MTU  = 1 << 8,   // 0x0100
    ...
};
```

`IBV_QP_AV` = **"Address Vector"（地址向量）**

### 2.2 作用

`IBV_QP_AV` 是 `ibv_modify_qp()` 的**属性掩码标志位**，用于告诉驱动：**本次modify_qp是否包含 `ah_attr`（Address Handle Attribute）字段**。

当设置此标志位时，驱动应该读取并应用 `ibv_qp_attr.ah_attr` 中的地址信息。

### 2.3 ah_attr结构

```c
struct ibv_qp_attr {
    ...
    struct ibv_ah_attr ah_attr;  // 主路径的地址句柄
    ...
};

struct ibv_ah_attr {
    struct ibv_global_route grh;  // 全局路由信息
    uint16_t dlid;                // 目标本地标识符 (Destination LID)
    uint8_t  sl;                  // 服务等级 (Service Level)
    uint8_t  port_num;            // 端口号
    ...
};

struct ibv_global_route {
    union ibv_gid dgid;           // 目标GID (128位) ← 最关键
    uint32_t flow_label;          // 流标签
    uint8_t  sgid_index;          // 源GID索引
    uint8_t  hop_limit;           // 跳数限制
    uint8_t  traffic_class;       // 流量类别
};
```

**核心信息**：`ah_attr.grh.dgid` 包含**目标QP的GID（全局标识符）**
- InfiniBand: 128位GID（子网前缀 + GUID）
- RoCE: 128位GID（可能包含IPv4或IPv6地址）
- IPv4-mapped格式: `::ffff:192.168.1.100` → 最后4字节是IP地址

## 三、RCCL的实现方式

### 3.1 RTR阶段（设置地址信息）

位置：`rccl/src/transport/net_ib.cc:1294-1336`

```c
ncclResult_t ncclIbRtrQp(struct ibv_qp* qp,
                         struct ncclIbGidInfo* sGidInfo,  // 本地GID info
                         uint32_t dest_qp_num,
                         struct ncclIbDevInfo* info) {   // 对端信息
  struct ibv_qp_attr qpAttr;
  memset(&qpAttr, 0, sizeof(struct ibv_qp_attr));
  qpAttr.qp_state = IBV_QPS_RTR;
  qpAttr.dest_qp_num = dest_qp_num;

  if (info->link_layer == IBV_LINK_LAYER_ETHERNET) {  // RoCE
    qpAttr.ah_attr.is_global = 1;
    // 设置对端GID
    qpAttr.ah_attr.grh.dgid.global.subnet_prefix = info->gid.global.subnet_prefix;
    qpAttr.ah_attr.grh.dgid.global.interface_id = info->gid.global.interface_id;
    // 设置本地GID索引
    qpAttr.ah_attr.grh.sgid_index = sGidInfo->localGidIndex;
    qpAttr.ah_attr.grh.hop_limit = 255;
    qpAttr.ah_attr.grh.traffic_class = tc;
  }

  // ✅ 使用 IBV_QP_AV 标志位
  wrap_ibv_modify_qp(qp, &qpAttr,
                     IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | ...);
}
```

### 3.2 RTS阶段（不设置地址信息）

位置：`rccl/src/transport/net_ib.cc:1340-1350`

```c
ncclResult_t ncclIbRtsQp(struct ibv_qp* qp) {
  struct ibv_qp_attr qpAttr;
  memset(&qpAttr, 0, sizeof(struct ibv_qp_attr));
  qpAttr.qp_state = IBV_QPS_RTS;
  qpAttr.timeout = ncclParamIbTimeout();
  qpAttr.retry_cnt = ncclParamIbRetryCnt();
  qpAttr.rnr_retry = 7;
  qpAttr.sq_psn = 0;
  qpAttr.max_rd_atomic = 1;

  // ❌ 没有 IBV_QP_AV 标志位！
  wrap_ibv_modify_qp(qp, &qpAttr,
                     IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | ...);
}
```

**关键点**：RCCL遵循标准RDMA语义，RTS阶段不再设置地址信息，因此不包含 `IBV_QP_AV` 标志位。

## 四、驱动中的IP地址提取逻辑

### 4.1 从ah_attr提取IP

位置：`rust-driver/src/rdma_utils/types.rs:492-503`

```rust
impl IbvQpAttr {
    pub(crate) fn new(attr: ibv_qp_attr, attr_mask: u32) -> Self {
        // 只有当 IBV_QP_AV 被设置时，才从 ah_attr 中提取IP
        let dest_qp_ip = if attr_mask & ibv_qp_attr_mask::IBV_QP_AV.0 != 0 {
            let gid = unsafe { attr.ah_attr.grh.dgid.raw };
            info!("gid: {:x}", u128::from_be_bytes(gid));

            // 只识别 IPv4-mapped 格式的GID：::ffff:a.b.c.d
            let is_ipv4_mapped =
                gid[..10].iter().all(|&x| x == 0) && gid[10] == 0xFF && gid[11] == 0xFF;

            is_ipv4_mapped.then(|| Ipv4Addr::new(gid[12], gid[13], gid[14], gid[15]))
        } else {
            None  // ❌ 没有 IBV_QP_AV 标志位时返回 None
        };

        Self {
            dest_qp_ip,
            ...
        }
    }
}
```

### 4.2 update_qp中的fallback逻辑

位置：`rust-driver/src/verbs/ctx.rs:505-519`

```rust
fn update_qp(&mut self, qpn: u32, attr: IbvQpAttr) -> Result<()> {
    let entry = self
        .qp_attr_table
        .map_qp_mut(qpn, |current| {
            // 尝试从QP属性表获取之前保存的IP
            let current_ip = (current.dqp_ip != 0).then_some(current.dqp_ip);

            // 尝试从本次modify_qp参数中提取IP
            let attr_ip = attr.dest_qp_ip().map(Ipv4Addr::to_bits);

            // 优先级：attr_ip > current_ip > 默认IP
            let ip_addr = attr_ip.or(current_ip).unwrap_or_else(|| {
                if attr.qp_state() == Some(ibverbs_sys::ibv_qp_state::IBV_QPS_RTS) {
                    let ip: Ipv4Addr = self.net_config.ip.ip();
                    log::warn!("update qpn {} to RTS with default ip {}", qpn, ip);
                    ip.to_bits()
                } else {
                    0
                }
            });

            // 保存IP到QP属性表
            current.dqp_ip = ip_addr;
            ...
        })
}
```

## 五、问题根因分析

### 5.1 为什么会触发默认IP？

触发默认IP需要同时满足三个条件：

1. **RTS阶段没有IBV_QP_AV标志**
   - RCCL在RTS阶段不设置 `IBV_QP_AV`
   - 导致 `attr_ip = None`

2. **RTR阶段的IP没有正确保存**
   - RCCL使用的是**标准RoCE GID格式**，而不是IPv4-mapped格式
   - 驱动只识别 `::ffff:x.x.x.x` 格式的IPv4-mapped GID
   - 导致RTR阶段 `dest_qp_ip` 提取失败，保存的 `dqp_ip = 0`
   - 因此 `current_ip = None`

3. **两者都为None**
   - 触发 `unwrap_or_else` 的fallback逻辑
   - 使用 `net_config.ip` 作为默认IP

### 5.2 GID格式问题

**标准RoCE GID格式**（RCCL使用）：
- 完整的128位GID
- 可能是IPv6地址或链路本地地址
- 例如：`fe80::xxxx:xxxx:xxxx:xxxx` 或 `2001:db8::1`

**IPv4-mapped GID格式**（驱动期望）：
- 格式：`::ffff:a.b.c.d`
- 128位中：前80位为0，81-96位为 `0xFFFF`，97-128位为IPv4地址
- 例如：`::ffff:192.168.1.100`

### 5.3 为什么RCCL这样实现？

RCCL遵循**标准IB/RoCE协议**：

1. **地址信息在RTR阶段设置一次即可**
   - 目标地址在RTR时确定，RTS时不需要改变
   - 这是所有标准RDMA实现的通用做法

2. **使用标准GID格式**
   - RoCE v1/v2使用完整的128位GID
   - 支持IPv4和IPv6
   - 不局限于IPv4-mapped格式

3. **符合标准verbs语义**
   - Mellanox、Intel等厂商的驱动都这样工作
   - 应用层无需关心底层是用IP还是GID路由

## 六、RCCL的GID选择机制

### 6.1 本地GID选择流程

位置：`rccl/src/transport/net_ib.cc:434-469`

```c
static ncclResult_t ncclIbGetGidIndex(struct ibv_context *context,
                                      uint8_t portNum,
                                      struct ibv_port_attr* portAttr,
                                      int *gidIndex) {
  int gidTblLen = portAttr->gid_tbl_len;

  // 1. InfiniBand模式：选择可路由的FLID GID
  if (portAttr->link_layer == IBV_LINK_LAYER_INFINIBAND) {
    int routableGidIndex = ncclParamIbRoutableFlidIbGidIndex();
    if (routableGidIndex < gidTblLen) {
      NCCLCHECK(wrap_ibv_query_gid(context, portNum, routableGidIndex, &gid));
      if (ncclIbExtractFlid(&gid) != 0) {
        *gidIndex = routableGidIndex;
        return ncclSuccess;
      }
    }
    *gidIndex = 0;  // 默认使用GID索引0
    return ncclSuccess;
  }

  // 2. RoCE模式：优先级选择策略

  // 优先级1: 环境变量指定（最高优先级）
  *gidIndex = ncclParamIbGidIndex();  // NCCL_IB_GID_INDEX
  if (*gidIndex >= 0) {
    return ncclSuccess;
  }

  // 优先级2: 根据网络配置自动选择
  sa_family_t userAddrFamily = envIbAddrFamily();  // NCCL_IB_ADDR_FAMILY
  int userRoceVersion = ncclParamIbRoceVersionNum();
  void *prefix = envIbAddrRange(userAddrFamily, &prefixlen);  // NCCL_IB_ADDR_RANGE

  // 从GID表中遍历，选择最匹配的GID
  *gidIndex = 0;
  for (int gidIndexNext = 1; gidIndexNext < gidTblLen; ++gidIndexNext) {
    NCCLCHECK(ncclUpdateGidIndex(context, portNum, userAddrFamily, prefix,
                                  prefixlen, userRoceVersion, gidIndexNext, gidIndex));
  }

  return ncclSuccess;
}
```

### 6.2 GID选择优先级规则

位置：`rccl/src/transport/net_ib.cc:392-432`

自动选择GID时的匹配规则：

1. **地址族类型匹配** + **子网匹配**
   - 优先选择与用户指定地址族（IPv4/IPv6）相同的GID
   - 且必须在用户指定的子网范围内

2. **有效GID** + **子网匹配** + **RoCE版本匹配**
   - GID必须是已配置的有效地址（非 `0.0.0.0` 或 `fe80::0`）
   - 不能是链路本地地址（link-local）
   - 优先选择匹配的RoCE版本（v1或v2）

```c
static ncclResult_t ncclUpdateGidIndex(...) {
  union ibv_gid gid, gidCandidate;

  sa_family_t gidFam = getGidAddrFamily(&gid);
  sa_family_t gidCandidateFam = getGidAddrFamily(&gidCandidate);
  bool gidCandidateMatchSubnet = matchGidAddrPrefix(usrFam, prefix, prefixlen, &gidCandidate);

  // 优先级1: 地址族类型匹配 + 子网匹配
  if (gidCandidateFam != gidFam && gidCandidateFam == usrFam && gidCandidateMatchSubnet) {
    *gidIndex = gidIndexCandidate;
    return;
  }

  // 优先级2: 有效GID + 子网匹配 + RoCE版本匹配
  if (validGid(&gidCandidate) && gidCandidateMatchSubnet) {
    if (gidRoceVerNumCandidate == usrRoceVer) {
      *gidIndex = gidIndexCandidate;
    }
  }
}
```

### 6.3 对端GID获取流程

RCCL通过**TCP Socket交换连接元数据**来获取对端GID：

**步骤1：本地准备阶段** (1494-1497行)
```c
// 获取本地GID索引
ncclIbGetGidIndex(ibDev->context, ibDev->portNum, &ibDev->portAttr,
                  &commDev->base.gidInfo.localGidIndex);

// 查询本地GID
wrap_ibv_query_gid(ibDev->context, ibDev->portNum,
                   commDev->base.gidInfo.localGidIndex,
                   &commDev->base.gidInfo.localGid);

// 打包到metadata中
devInfo->gid.global.subnet_prefix = commDev->base.gidInfo.localGid.global.subnet_prefix;
devInfo->gid.global.interface_id = commDev->base.gidInfo.localGid.global.interface_id;
```

**步骤2：通过Socket交换metadata** (1536-1539行)
```c
stage->state = ncclIbCommStateSend;
memcpy(stage->buffer, &meta, sizeof(meta));  // meta包含本地GID
// 通过TCP socket发送给对端
```

**步骤3：接收对端metadata** (1573-1576行)
```c
for (int i = 0; i < remMeta.ndevs; i++) {
  comm->base.remDevs[i] = remMeta.devs[i];
  // 保存对端的GID
  comm->base.remDevs[i].remoteGid.global.interface_id = remMeta.devs[i].gid.global.interface_id;
  comm->base.remDevs[i].remoteGid.global.subnet_prefix = remMeta.devs[i].gid.global.subnet_prefix;
}
```

**步骤4：使用对端GID设置QP** (1608行)
```c
// RTR阶段使用对端GID
ncclIbRtrQp(qp, &commDev->base.gidInfo,  // 本地GID info
            remQpInfo->qpn,              // 对端QP号
            remDevInfo,                  // 对端GID在这里
            false, remMeta.tc, remMeta.sl);
```

### 6.4 环境变量控制

用户可以通过以下环境变量控制GID选择：

**1. NCCL_IB_GID_INDEX**（最高优先级）
```bash
export NCCL_IB_GID_INDEX=3  # 直接指定使用GID表中索引3的GID
```

**2. NCCL_IB_ADDR_FAMILY**（指定地址族）
```bash
export NCCL_IB_ADDR_FAMILY=AF_INET   # 优先选择IPv4-mapped GID
export NCCL_IB_ADDR_FAMILY=AF_INET6  # 优先选择IPv6 GID
```

**3. NCCL_IB_ADDR_RANGE**（指定子网范围）
```bash
export NCCL_IB_ADDR_RANGE=192.168.1.0/24  # 只选择该子网内的GID
export NCCL_IB_ADDR_RANGE=2001:db8::/32   # IPv6子网
```

## 七、解决方案建议

### 7.1 短期方案：增强GID到IP的转换

在RTR阶段正确提取并保存IP地址：

```rust
// 位置：rust-driver/src/rdma_utils/types.rs
let dest_qp_ip = if attr_mask & ibv_qp_attr_mask::IBV_QP_AV.0 != 0 {
    let gid = unsafe { attr.ah_attr.grh.dgid.raw };

    // 1. IPv4-mapped格式：::ffff:a.b.c.d
    let is_ipv4_mapped = gid[..10].iter().all(|&x| x == 0)
                        && gid[10] == 0xFF && gid[11] == 0xFF;
    if is_ipv4_mapped {
        return Some(Ipv4Addr::new(gid[12], gid[13], gid[14], gid[15]));
    }

    // 2. 标准RoCE v2格式：提取IPv4地址（如果GID的最后4字节是有效IP）
    // TODO: 需要根据实际GID格式实现

    // 3. IPv6格式：如果硬件支持IPv6路由，直接使用GID
    // TODO: 考虑硬件是否支持IPv6

    None
} else {
    None
};
```

### 7.2 长期方案：直接使用GID

如果硬件支持，在硬件层面直接使用128位GID进行路由，而不是转换为IP：

1. 修改硬件CSR接口，支持完整的128位GID
2. 修改驱动，直接传递GID给硬件
3. 无需从GID中提取IP地址

### 7.3 临时workaround：配置IPv4-mapped GID

如果可以控制网络配置，让系统使用IPv4-mapped格式的GID：

```bash
# 查看当前GID表
show_gids

# 配置RoCE v2使用IPv4-mapped GID
# 这通常需要网络管理员配置交换机和网卡
```

## 八、总结

1. **问题本质**：驱动期望IPv4-mapped GID格式，但RCCL使用标准RoCE GID格式

2. **RCCL的做法是标准的**：
   - RTR阶段通过`IBV_QP_AV`设置地址信息
   - RTS阶段不重新设置地址
   - 使用完整的128位GID

3. **驱动的问题**：
   - 只支持IPv4-mapped GID格式
   - RTR阶段GID提取失败时没有正确处理
   - 依赖RTS阶段的fallback逻辑

4. **根本原因**：驱动与标准RDMA语义的适配问题，而非RCCL的问题

5. **RCCL的GID选择**：
   - 支持环境变量手动指定
   - 自动选择时考虑地址族、子网、RoCE版本
   - 通过TCP Socket交换GID信息

---

**调研日期**: 2026-02-02
**相关代码**:
- `rust-driver/src/verbs/ctx.rs:505-539`
- `rust-driver/src/rdma_utils/types.rs:492-503`
- `rccl/src/transport/net_ib.cc:434-469, 1294-1336`