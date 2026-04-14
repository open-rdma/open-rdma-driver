# cocotb dev 环境中 `GPI_USERS` 与 Verilator 兼容性说明

## 背景

在将 RTL 仿真环境从 `cocotb 1.9.2` 切换到 `cocotb` dev 版本后，可能会遇到两类典型问题：

1. Verilator 版本过低，编译 `cocotb` 自带的 `verilator.cpp` 失败
2. 仿真器启动时缺少 `GPI_USERS`，导致 `cocotb` 尚未进入测试模块就直接退出

这两个问题都不是 RTL 逻辑本身的错误，而是仿真基础设施与 `cocotb` dev 启动协议不匹配导致的。

## 问题一：Verilator 版本过低

### 典型错误

```log
error: ‘clearEvalNeeded’ is not a member of ‘VerilatedVpi’
error: ‘doInertialPuts’ is not a member of ‘VerilatedVpi’
error: ‘evalNeeded’ is not a member of ‘VerilatedVpi’
```

### 原因

`cocotb` dev 版本的 Verilator 适配代码会调用较新的 `VerilatedVpi` 接口：

- `VerilatedVpi::clearEvalNeeded()`
- `VerilatedVpi::doInertialPuts()`
- `VerilatedVpi::evalNeeded()`

这些接口在较老版本的 Verilator 中不存在。例如 `Verilator 5.020` 就会在编译阶段失败。

### 结论

建议使用：

```text
Verilator >= 5.026
```

更稳妥的做法是直接使用更新的 5.x 版本，而不是停在最低兼容版本边缘。

## 问题二：`No GPI_USERS specified, exiting...`

### 典型错误

```log
No GPI_USERS specified, exiting...
```

随后常见的连带错误还有：

```log
xml.etree.ElementTree.ParseError: no element found: line 1, column 0
```

后一个错误通常只是因为仿真器过早退出，没有生成结果 XML。

### `GPI_USERS` 的作用

`GPI_USERS` 是 `cocotb 2.x/dev` 启动链中的底层环境变量，用来告诉 GPI 层在仿真器启动时需要加载哪些入口。

对于当前使用的 Python 仿真流程，`GPI_USERS` 通常至少包含两部分：

```text
<libpython 路径>;<pygpi entry point>
```

其中：

- `libpython`：用于装载 Python 运行时
- `pygpi entry point`：用于进入 `cocotb` 的 Python 启动逻辑

如果没有这个环境变量，仿真器虽然能启动，但 GPI 层不知道要装载什么，因此会在进入测试模块之前直接退出。

## 启动链说明

当前 RTL 仿真的启动链可简化为：

```text
make run_system_test_server_loopback
    -> tb_top_for_system_test.py
    -> cocotb_test.simulator.run(...)
    -> Verilator 仿真可执行文件
    -> VPI 层
    -> GPI 层
    -> PyGPI / cocotb regression
    -> 导入 COCOTB_TEST_MODULES 指定的测试模块
    -> 执行 @cocotb.test
```

几个关键环境变量的层级如下：

- `GPI_USERS`
  作用层级：GPI 层
  作用：告诉底层先加载哪些入口
- `LIBPYTHON_LOC`
  作用层级：GPI / Python bridge
  作用：定位 `libpython`
- `PYGPI_PYTHON_BIN`
  作用层级：PyGPI
  作用：指定 Python 可执行文件
- `COCOTB_TEST_MODULES`
  作用层级：cocotb regression
  作用：指定需要导入并执行的测试模块

因此：

- `GPI_USERS` 解决的是“cocotb 能不能先启动起来”
- `COCOTB_TEST_MODULES` 解决的是“启动起来之后跑哪个测试模块”

## 为什么需要手动补 `GPI_USERS`

当前工程使用的是：

- `cocotb` dev 版本
- `cocotb_test 0.2.6`

这两者之间存在一个启动协议差异：

- `cocotb` dev 已经要求 `GPI_USERS`
- 但 `cocotb_test 0.2.6` 不会自动把 `GPI_USERS` 组装完整

因此，虽然 `cocotb_test` 仍会设置：

- `COCOTB_TEST_MODULES`
- `LIBPYTHON_LOC`
- `PYGPI_PYTHON_BIN`

但如果不额外补 `GPI_USERS`，仿真器还是会在 GPI 初始化阶段退出。

## 当前工程中的处理方式

在 `open-rdma-rtl/test/cocotb/test_framework/common.py` 中增加了共享 helper：

```python
def cocotb_extra_env():
    ...
```

该 helper 会自动注入：

- `PYGPI_PYTHON_BIN`
- `LIBPYTHON_LOC`
- `GPI_USERS`

随后在以下入口统一传给 `cocotb_test.simulator.run(..., extra_env=...)`：

- `compile_verilator.py`
- `tb_top_for_system_test.py`
- `tb_top_for_system_test_two_card.py`
- `tb_top_for_system_test_multi_node.py`

这样可以在继续使用现有 `cocotb_test` 代码结构的前提下，兼容 `cocotb` dev 的启动要求。

## 建议

如果要继续使用 `cocotb` dev，建议同时满足以下条件：

1. 使用 `Verilator >= 5.026`
2. 自动设置 `GPI_USERS`
3. 保持 `cocotbext-pcie`、`cocotbext-axi`、`cocotb-bus` 与当前环境一致

如果只升级其中一部分，而其余组件仍停留在旧启动流程或旧工具版本上，就很容易出现“安装成功但仿真启动失败”的情况。
