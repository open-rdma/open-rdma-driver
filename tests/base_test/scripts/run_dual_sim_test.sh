#!/bin/bash

# 通用的双端 sim 测试脚本
# 用法: ./run_dual_sim_test.sh <test_program> [args...]
# 例如: ./run_dual_sim_test.sh send_recv 4096
#       ./run_dual_sim_test.sh write_imm

# 检查参数
if [ $# -lt 1 ]; then
    echo "Usage: $0 <test_program> [args...]"
    echo "Example: $0 send_recv 4096"
    echo "         $0 write_imm"
    exit 1
fi

# 获取测试程序名称
TEST_PROGRAM=$1
shift  # 移除第一个参数，剩余参数将传递给测试程序



# 设置目录路径
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DRIVER_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)
PROGRAM_DIR="$SCRIPT_DIR/../build/bin"



# 设置日志目录（为每个测试程序创建独立的日志目录）
mkdir -p $SCRIPT_DIR/../log/sim/$TEST_PROGRAM
LOG_DIR=$(cd "$SCRIPT_DIR/../log/sim/$TEST_PROGRAM" && pwd)

echo "Log directory: $LOG_DIR"

# Source 共同函数库
source $SCRIPT_DIR/../../common/test_common.sh

# 设置信号处理
setup_signal_handler

# 打印测试开始信息
print_test_start "$TEST_PROGRAM"

# 初始化测试环境
init_test_environment

# 编译 Rust 驱动（同时设置 LD_LIBRARY_PATH）
build_rust_driver "sim"

# 前置编译普通 SIM flow 的 BSV/Verilog
build_rtl_verilog_for_test "default"

# 启动 RTL 模拟器（2个实例）
start_rtl_simulators 2 "$TEST_PROGRAM"

# 编译测试程序
build_test_program "$SCRIPT_DIR/.."

# 检查测试程序是否存在
if [ ! -f "$PROGRAM_DIR/$TEST_PROGRAM" ]; then
    echo "Error: Test program $PROGRAM_DIR/$TEST_PROGRAM not found"
    exit 1
fi

echo "Running $TEST_PROGRAM test with args: $@"

cd $SCRIPT_DIR/..

# 设置默认的 RUST_LOG 级别（如果环境变量未设置）
RUST_LOG=${RUST_LOG:-info}

# 先启动 server
echo "Starting server..."
echo "  RUST_LOG=$RUST_LOG"
if [ -n "$RDMA_BUFFER_SIZE" ]; then
    echo "  RDMA_BUFFER_SIZE=$RDMA_BUFFER_SIZE"
    sudo env RUST_LOG="$RUST_LOG" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" RDMA_BUFFER_SIZE="$RDMA_BUFFER_SIZE" $PROGRAM_DIR/$TEST_PROGRAM "$@" &> $LOG_DIR/server.log &
else
    sudo env RUST_LOG="$RUST_LOG" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" $PROGRAM_DIR/$TEST_PROGRAM "$@" &> $LOG_DIR/server.log &
fi
SERVER_PID=$!

echo "Server PID: $SERVER_PID (log: $LOG_DIR/server.log)"

# 等待 server 启动
sleep 3

# 启动 client，连接到 localhost
echo "Starting client..."
echo "  RUST_LOG=$RUST_LOG"
if [ -n "$RDMA_BUFFER_SIZE" ]; then
    echo "  RDMA_BUFFER_SIZE=$RDMA_BUFFER_SIZE"
    sudo env RUST_LOG="$RUST_LOG" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" RDMA_BUFFER_SIZE="$RDMA_BUFFER_SIZE" $PROGRAM_DIR/$TEST_PROGRAM "$@" 127.0.0.1 &> $LOG_DIR/client.log &
else
    sudo env RUST_LOG="$RUST_LOG" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" $PROGRAM_DIR/$TEST_PROGRAM "$@" 127.0.0.1 &> $LOG_DIR/client.log &
fi
CLIENT_PID=$!

echo "Client PID: $CLIENT_PID (log: $LOG_DIR/client.log)"

# 只等待测试程序，不等待 RTL 进程
FAILED=0

if ! wait_for_test_process "server" "$SERVER_PID" "$LOG_DIR/server.log"; then
    FAILED=1
fi

if ! wait_for_test_process "client" "$CLIENT_PID" "$LOG_DIR/client.log"; then
    FAILED=1
fi

if [ "$FAILED" -ne 0 ]; then
    print_test_failed "$TEST_PROGRAM"
    echo "Logs saved to: $LOG_DIR" >&2
    echo "  - Server log: $LOG_DIR/server.log" >&2
    echo "  - Client log: $LOG_DIR/client.log" >&2
    exit 1
fi

# 打印测试结束信息
print_test_end "$TEST_PROGRAM"

echo "Logs saved to: $LOG_DIR"
echo "  - Server log: $LOG_DIR/server.log"
echo "  - Client log: $LOG_DIR/client.log"
