# 死锁bug 复现

[细节参考](./payload-con-rq-deadlock-fix.md)

## 仿真复现

由于仿真速度较慢，为了加速复现过程，需要减小ringbuf的长度。（最小应该只能是16，更小的话硬件对齐就出问题了）

### 1. Driver 端修改 ringbuf 长度

`rust-driver/src/ring/buffer/mod.rs:17`

```rust
const RING_BUF_LEN_BITS: u8 = 4;  // 4位 = 长度16
```

### 2. RTL 端修改 ringbuf 长度

`open-rdma-rtl/src/Ringbuf.bsv:94`

```bluespec
typedef 16  USER_LOGIC_RING_BUF_4096_DEEP;  // 与driver保持一致
```

### 3. Consumer 添加延迟

`rust-driver/src/ring/buffer/consumer.rs` 在 `try_pop` 方法开头添加：

```rust
std::thread::sleep(std::time::Duration::from_millis(100));
```

### 4. RTL FullyPipelineChecker 修改

`open-rdma-rtl/src/FullyPipelineChecker.bsv`

注释掉 `mkFIFOFWithFullAssert`、`mkLFIFOFWithFullAssert`、`mkSizedFIFOFWithFullAssert` 中的 `assertFull` rule。

### 5. 运行仿真测试

```bash
./tests/base_test/scripts/test_loopback_sim.sh 1 200
```

## 硬件复现

### 1. 添加轮询速度限制

`rust-driver/src/workers/spawner.rs:73` 在 worker 循环中添加：

```rust
std::thread::sleep(Duration::from_millis(2));
```

### 2. 运行硬件测试

```bash
./tests/base_test/scripts/test_loopback_hw.sh 1 4000
```

- 第一个参数：写多少 byte 的大小
- 第二个参数：并行发送多少请求（太少复现不了，太多会把 PCIE 控制器冲爆）

## Ps

- Producer 是 lazy 的，只有没有位置了才会去读 CSR 寄存器更新指针
- Consumer 轮询的是标志位而不是 CSR 寄存器，想要看到 consumer 中还有几个空位需要手动加 log
