#!/bin/bash

# 设置目录路径
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DRIVER_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)
PROGRAM_DIR="$SCRIPT_DIR/../build/bin"

# 设置日志目录
mkdir -p $SCRIPT_DIR/../log/sim/pcie_loopback
LOG_DIR=$(cd "$SCRIPT_DIR/../log/sim/pcie_loopback" && pwd)

# Source 共同函数库
source $SCRIPT_DIR/../../common/test_common.sh

# 设置信号处理
setup_signal_handler

# 打印测试开始信息
print_test_start "pcie_loopback"

# 初始化测试环境
init_test_environment

# 编译 Rust 驱动（同时设置 LD_LIBRARY_PATH）
build_rust_driver "sim"

# 前置编译 PCIe flow 的 BSV/Verilog
build_rtl_verilog_for_test "pcie"

# 启动 RTL 模拟器（1个实例）
start_rtl_simulators 1 "pcie_loopback"

# 编译测试程序
build_test_program "$SCRIPT_DIR/.."

# 运行 loopback 测试，参数是消息长度
MSG_LEN=${1:-2096}  # 默认 4096 字节
ROUND=${2:-10}  # 默认 10 轮
RUST_LOG=${RUST_LOG:-info}  # 默认 info 级别日志

echo "Running loopback test with MSG_LEN=$MSG_LEN"

# sudo env RUST_BACKTRACE=debug RUST_LOG=$RUST_LOG LD_LIBRARY_PATH="$LD_LIBRARY_PATH" "$PROGRAM_DIR/loopback" $MSG_LEN $ROUND > $LOG_DIR/loopback.log 2>&1 &
sudo env RUST_BACKTRACE=debug RUST_LOG=$RUST_LOG LD_LIBRARY_PATH="$LD_LIBRARY_PATH" "$PROGRAM_DIR/small_pack_loopback" $MSG_LEN $ROUND > $LOG_DIR/loopback.log 2>&1 &

LOOPBACK_PID=$!

echo "Loopback test PID: $LOOPBACK_PID"

# 只等待测试程序，不等待 RTL 进程
if ! wait_for_test_process "loopback test" "$LOOPBACK_PID" "$LOG_DIR/loopback.log"; then
    print_test_failed "loopback"
    echo "Log saved to: $LOG_DIR/loopback.log" >&2
    exit 1
fi

# 打印测试结束信息
print_test_end "loopback"
