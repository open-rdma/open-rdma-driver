# M1 DMA Engine

本目录是自持的 M1 交付单元，包含：

- `src/DmaEngine.bsv`：DMA Engine production 源码；
- `src/CommonTypes.bsv`、`src/DmaTypes.bsv`：独立编译所需类型与 Pipe 接口；
- `tb/TestDmaEngine.bsv`：仅用于 Cocotb 的透明接口 wrapper；
- `cocotb/`：BFM、reference model、short/long/expected-fatal 测试；
- `tools/`：production RTL 依赖收集与 Quartus bundle 导出工具。

M1 不依赖 `mvp/` 或 `open-rdma-rtl/` 中的源码，可以单独复制后构建。

## 环境

需要以下工具：

- BSC 2025.01.1 或兼容版本；
- Python 3 和 Cocotb 2；
- Verilator（默认 simulator）；
- Icarus Verilog（双 simulator 回归和 expected-fatal 测试）；
- Quartus（仅宿主工程集成时需要）。

可显式指定工具路径：

```bash
make test \
  BSC=/path/to/bsc \
  PYTHON=/path/to/python3 \
  SIM=verilator
```

## 编译与 Cocotb 仿真

```bash
# BSV 编译检查
make compile TARGET=dma

# 生成 Cocotb wrapper 层次并运行 13 个 short tests
make test TARGET=dma SIM=verilator
make test TARGET=dma SIM=icarus

# 512-message 随机长测试
make test-long TARGET=dma SIM=verilator

# 16 个协议断言 expected-fatal case
make test-negative TARGET=dma SIM=icarus

# M1 payload compactor smoke regression
make test TARGET=compactor SIM=verilator
```

Cocotb 的 HDL top 是 `mkTestDmaEngine`。该 wrapper 只重命名 Pipe 接口，不改变
`mkDmaEngine` 的数据通路。

## 导出 Quartus RTL bundle

```bash
# 直接以 mkDmaEngine 为 production top 生成可移动 bundle
make quartus-rtl

# 额外用 Icarus 和 Verilator 检查 bundle 的语法与层次闭包
make check-quartus-rtl

# 可选：真正执行 Quartus Analysis & Synthesis
make quartus-synth
make quartus-synth \
  QUARTUS_FAMILY="<目标器件 family>" \
  QUARTUS_DEVICE=<目标器件订货号>
```

输出位于：

```text
build/quartus/
├── rtl/                 # mkDmaEngine 层次及实际使用的 BSC primitives
├── dma_engine.f         # 从 bundle 根目录解释的相对路径 filelist
└── dma_engine.qip       # 可直接加入 Quartus 工程的相对路径 QIP
```

导出过程每次从空的 `build/production/bsc` 目录重新 elaboration，并通过 Bluetcl
递归收集真实层次依赖。bundle 会拒绝 `mkTest*.v`、缺失模块和同名源文件覆盖，避免
把 testbench 或陈旧 RTL 交给 Quartus。

在 Quartus GUI 中可以把 `dma_engine.qip` 加入工程；也可以在宿主 QSF 中加入：

```tcl
set_global_assignment -name QIP_FILE /absolute/path/to/build/quartus/dma_engine.qip
```

QIP 内部只使用相对于自身的路径，所以整个 `build/quartus` 目录可以移动。不要同时
手工添加 `rtl/*.v`，否则会重复定义模块。

`quartus-synth` 默认沿用参考 F-Tile 工程的 Agilex 7 器件
`AGIB027R31B1E2VR0`，并只运行 Analysis & Synthesis；集成其他板卡时应通过
`QUARTUS_FAMILY` 和 `QUARTUS_DEVICE` 覆盖。

## Production 顶层接口

Production top 是 `mkDmaEngine`，使用 `CLK` 和低有效 `RST_N`。其公开接口为 BSC
method handshake，而不是 AXI 或 Avalon：

| BSV 接口 | 方向 | packed 宽度 |
|---|---|---:|
| `chunkIn` / `DmaChunkReq` | 输入 | 130 bit |
| `dmaReadReqOut` / `DmaReadRequest` | 输出 | 157 bit |
| `dmaCompletionIn` / `DmaCompletionBeat` | 输入 | 1233 bit |
| `payloadOut` / `DmaPayloadBeat` | 输出 | 1202 bit |
| `messageDoneOut` / `DmaMessageDone` | 输出 | 65 bit |

调用 Action method 时必须同时满足对应 `EN_*` 与 `RDY_*`；读取 `*_first` 前必须检查
它的 `RDY_*`，不能把 `EN_*` 当成可永久拉高的 AXI `valid`。

## Quartus 交付边界

这里生成的是 **Quartus 可导入的 RTL 组件 bundle**，可以作为宿主 FPGA 工程的子模块，
也可以用 `mkDmaEngine` 做独立 Analysis & Synthesis。它不是特定板卡的 `.sof`。

生成可烧录 `.sof` 还需要宿主板卡顶层、FPGA family/device、pin assignment、SDC、时钟与
复位网络，以及 PCIe/DMA/内存硬 IP 到上述 Pipe 接口的适配；这些信息不属于独立
`DmaEngine` 模块本身。
