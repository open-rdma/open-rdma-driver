# PSN重映射与Bitmap机制详解

## 1. PSN重映射机制

### 为什么需要重映射？

硬件和软件对PSN（Packet Sequence Number）的处理方式不同：

- **硬件视角**：bitmap窗口从第16个stride开始（而非从0开始）
- **软件视角**：期望从PSN 0开始处理

### 重映射公式

```rust
fn remap_psn(psn: Psn) -> Psn {
    const OFFSET: u32 = 112;  // 128 - 16
    psn - OFFSET
}
```

- **128**：bitmap窗口大小（位宽）
- **16**：硬件起始stride偏移
- **112**：需要补偿的偏移量

### 实际例子

如果硬件报告PSN = 112：
- 重映射后：112 - 112 = 0
- 表示这是窗口中的第一个包

如果硬件报告PSN = 0：
- 重映射后：0 - 112 = 65424（wrap-around）
- 通过模运算回到有效范围

## 2. Bitmap窗口机制

### 硬件实现（RTL/BSV）

```bsv
// 关键参数
Bit#(32) ACK_BITMAP_WIDTH = 128;    // bitmap宽度
Bit#(32) ACK_WINDOW_STRIDE = 16;    // stride大小

// Bitmap窗口存储
BitmapWindowStorageEntry {
    Bit#(128) data;      // 128位bitmap
    Bit#(24)  leftBound; // 窗口左边界（PSN）
    Bit#(8)   qpnKeyPart; // QP编号
}
```

### 工作流程

1. **包到达**：硬件根据PSN计算bitmap位置
   ```
   bitmap位置 = (PSN - leftBound) % 128
   ```

2. **Bitmap更新**：执行OR操作合并确认信息
   ```
   newEntry.data = newEntry.data | oneHotBitmap
   ```

3. **窗口滑动**：当PSN超出当前窗口时
   - 检测条件：PSN > leftBound + 128
   - 操作：更新leftBound，清空bitmap

### 并发保护

硬件实现了三层保护：
1. **Round-robin仲裁器**：序列化多通道访问
2. **BRAM互斥**：`bitmapUpdateBusyReg`防止竞态
3. **流水线处理**：三级流水线确保原子操作

## 3. 硬件-软件接口

### Meta报告描述符

```bsv
typedef struct {
    RingbufDescCommonHead commonHeader;  // 16位
    // ... 控制字段 ...
    PSN psnBeforeSlide;                  // 24位
    PSN psnNow;                          // 24位
    QPN qpn;                             // 24位
    MSN msn;                             // 16位
    AckBitmap nowBitmap;                 // 128位
} MetaReportQueueAckDesc;
```

### 数据流向

```
网络包 → 硬件通道 → AutoAckGenerator → BitmapWindowStorage → Meta报告队列 → 驱动
```

## 4. 双向跟踪机制

### 接收方向（LocalAckTracker）
- 跟踪本地硬件接收的包
- 使用硬件ACK描述符中的bitmap
- 更新完成队列和重传工作线程

### 发送方向（RemoteAckTracker）
- 基于远程ACK/NAK跟踪已发送包
- 确定哪些包需要重传
- 使用MSN进行关联

## 5. 关键设计洞察

1. **性能优化**：硬件处理高速bitmap操作，软件处理高层协调
2. **存储效率**：128位bitmap可跟踪128个连续PSN，存储开销小
3. **容错性**：滑动窗口机制自动处理丢包检测和窗口推进
4. **并发安全**：即使4个并行硬件通道，每QP的bitmap更新也是序列化的

## 6. 实际案例分析

从日志中的ACK描述符：
```
'h80110800000000000000000001f300000003ffffffffffffffffffffffffffff
```

解析结果：
- **psnNow**: 0（重映射后的值）
- **qpn**: 499
- **bitmap**: 0x0003ffffffffffffffffffffffffffff
  - 包0-1：显式确认（0x0003）
  - 包2-127：全部确认（全1）

这表明：
1. 硬件实际PSN可能是112（112-112=0）
2. 所有包都已确认接收
3. QPN 499的接收队列状态正常

## 总结

PSN重映射和bitmap机制是RDMA可靠性的核心：
- **重映射**解决了硬件窗口偏移问题
- **Bitmap**提供了高效的数据包确认机制
- **硬件加速**确保高性能，**软件协调**确保正确性
- 整体设计体现了软硬件协同优化的思想