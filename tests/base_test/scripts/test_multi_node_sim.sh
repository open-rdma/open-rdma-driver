#!/bin/bash

# 多节点模拟send_recv测试脚本

# 检查参数
if [ $# -lt 1 ]; then
    echo "Usage: $0 [args...]"
    echo "Example: $0 4096 4"
    exit 1
fi

# 获取测试程序名称
TEST_PROGRAM="multi_node_send_recv"

# 设置目录路径
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DRIVER_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)
PROGRAM_DIR=$(cd "$SCRIPT_DIR/../build/bin" && pwd)



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

# 编译 Rust 驱动
build_rust_driver "sim"

# 启动 soft switch 模拟器
start_soft_switch

# 启动 RTL 模拟器（2个实例）
start_rtl_simulators_with_switch $2 "$TEST_PROGRAM"

# 编译测试程序
build_test_program "$SCRIPT_DIR/.."

# 设置运行时环境
setup_runtime_environment

# 检查测试程序是否存在
if [ ! -f "$PROGRAM_DIR/$TEST_PROGRAM" ]; then
    echo "Error: Test program $PROGRAM_DIR/$TEST_PROGRAM not found"
    exit 1
fi

echo "Running $TEST_PROGRAM test with args: $@"

cd $SCRIPT_DIR/..

# 设置默认的 RUST_LOG 级别（如果环境变量未设置）
RUST_LOG=${RUST_LOG:-info}

NODE_PIDS=()

# <msg_len> <rank> <world_size> 
for ((i=0; i<$2; i++)); do
    echo "Starting node $i..."
    echo "  RUST_LOG=$RUST_LOG"
    if [ -n "$RDMA_BUFFER_SIZE" ]; then
        echo "  RDMA_BUFFER_SIZE=$RDMA_BUFFER_SIZE"
        sudo env RUST_LOG="$RUST_LOG" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" RDMA_BUFFER_SIZE="$RDMA_BUFFER_SIZE" $PROGRAM_DIR/$TEST_PROGRAM $1 $i $2 &> $LOG_DIR/node_$i.log &
    else
        sudo env RUST_LOG="$RUST_LOG" LD_LIBRARY_PATH="$LD_LIBRARY_PATH" $PROGRAM_DIR/$TEST_PROGRAM $1 $i $2 &> $LOG_DIR/node_$i.log &
    fi
    NODE_PID=$!

    echo "Node PID: $NODE_PID (log: $LOG_DIR/node_$i.log)"
    NODE_PIDS+=("$NODE_PID")
    # 等待 Node 启动
    sleep 3
done


# 等待全部测试程序，不等待 RTL 进程；任一失败则退出失败
FAILED=0
for pid in "${NODE_PIDS[@]}"; do
    if ! wait "$pid"; then
        FAILED=1
    fi
done

if [ "$FAILED" -ne 0 ]; then
    echo "One or more nodes failed"
    exit 1
fi

# 打印测试结束信息
print_test_end "$TEST_PROGRAM"

echo "Logs saved to: $LOG_DIR"
echo "  - Node logs: $LOG_DIR/node_*.log"
