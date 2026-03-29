#!/bin/bash

# 设置目录路径
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DRIVER_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)

# 设置日志目录
mkdir -p $SCRIPT_DIR/../log/sim/loopback
LOG_DIR=$(cd "$SCRIPT_DIR/../log/sim/loopback" && pwd)

# Source 共同函数库
source $SCRIPT_DIR/../../common/test_common.sh

# 设置信号处理
setup_signal_handler

# 打印测试开始信息
print_test_start "loopback"

# 初始化测试环境
init_test_environment

# 编译 Rust 驱动（同时设置 LD_LIBRARY_PATH）
build_rust_driver "sim"

# 启动 RTL 模拟器（1个实例）
start_rtl_simulators 1 "loopback"

# 编译测试程序
build_test_program "$SCRIPT_DIR/.."

# 运行 loopback 测试，参数是消息长度
MSG_LEN=${1:-2096}  # 默认 4096 字节
ROUND=${2:-10}  # 默认 10 轮
RUST_LOG=${RUST_LOG:-info}  # 默认 info 级别日志

echo "Running loopback test with MSG_LEN=$MSG_LEN"

cd $SCRIPT_DIR/..
# sudo env RUST_BACKTRACE=debug RUST_LOG=$RUST_LOG LD_LIBRARY_PATH="$LD_LIBRARY_PATH" ./build/bin/loopback $MSG_LEN $ROUND > $LOG_DIR/loopback.log 2>&1 &
sudo env RUST_BACKTRACE=debug RUST_LOG=$RUST_LOG LD_LIBRARY_PATH="$LD_LIBRARY_PATH" ./build/bin/small_pack_loopback $MSG_LEN $ROUND > $LOG_DIR/loopback.log 2>&1 &

LOOPBACK_PID=$!

echo "Loopback test PID: $LOOPBACK_PID"

# 只等待测试程序，不等待 RTL 进程
wait $LOOPBACK_PID

# 打印测试结束信息
print_test_end "loopback"

