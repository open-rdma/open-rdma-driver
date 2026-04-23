#!/bin/bash

# 共同的测试脚本函数库
#
# 使用方法:
#   DRIVER_DIR=/path/to/open-rdma-driver
#   source $SCRIPT_DIR/../../common/test_common.sh
#   init_test_environment
#
# 环境变量:
#   DRIVER_DIR - (必需) open-rdma-driver 目录的绝对路径
#   RTL_DIR - (可选) open-rdma-rtl 目录路径，如果未设置则使用默认值
#   LOG_DIR - (必需) 日志目录路径，由调用脚本设置

# 设置信号处理，确保所有子进程在脚本中断时被终止
setup_signal_handler() {
    trap "cleanup_rtl_simulators; kill 0" SIGINT
    trap "cleanup_rtl_simulators" EXIT
}

# 初始化测试环境
# 从环境变量读取 DRIVER_DIR，计算 DTLD_DIR 和 RTL_DIR
# 设置全局变量:
#   DTLD_DIR - dtld-ibverbs 目录的绝对路径
#   RTL_DIR - open-rdma-rtl 目录的绝对路径
init_test_environment() {
    if [ -z "$DRIVER_DIR" ]; then
        echo "Error: DRIVER_DIR not set"
        exit 1
    fi

    echo "Initializing test environment..."
    echo "Driver directory: $DRIVER_DIR"

    # 计算 DTLD_DIR (dtld-ibverbs 在 DRIVER_DIR 内部)
    DTLD_DIR="$DRIVER_DIR/dtld-ibverbs"
    if [ ! -d "$DTLD_DIR" ]; then
        echo "Error: DTLD directory not found: $DTLD_DIR"
        exit 1
    fi
    echo "DTLD directory: $DTLD_DIR"

    # 设置 RTL_DIR，优先使用环境变量，否则使用默认值
    if [ -z "$RTL_DIR" ]; then
        RTL_DIR="$DRIVER_DIR/../open-rdma-rtl"
        if [ ! -d "$RTL_DIR" ]; then
            echo "Error: RTL directory not found: $RTL_DIR"
            exit 1
        fi
        RTL_DIR=$(cd "$RTL_DIR" && pwd)
    fi
    echo "RTL directory: $RTL_DIR"

    # 导出变量
    export DRIVER_DIR
    export DTLD_DIR
    export RTL_DIR
}

# 编译 Rust 驱动 (dtld-ibverbs) 并设置运行时 LD_LIBRARY_PATH
# 参数:
#   $1: feature - 编译特性 ("sim" 或 "mock"，默认 "sim")
#   $2: profile - 编译模式 ("debug" 或 "release"，默认 "debug")
# 副作用:
#   设置并导出 LD_LIBRARY_PATH
build_rust_driver() {
    local feature=${1:-"sim"}
    local profile=${2:-"debug"}

    if [ -z "$DTLD_DIR" ]; then
        echo "Error: DTLD_DIR not set. Call init_test_environment first."
        exit 1
    fi

    echo "Building Rust driver with feature: $feature, profile: $profile"

    cd "$DTLD_DIR"
    if [ "$profile" = "release" ]; then
        cargo build --no-default-features --features=$feature --release
    else
        cargo build --no-default-features --features=$feature
    fi

    if [ $? -ne 0 ]; then
        echo "Error: Failed to build Rust driver"
        exit 1
    fi

    echo "Rust driver built successfully"

    # 编译 rdma-core
    (cd "$DTLD_DIR/rdma-core-55.0/" && ./build.sh)

    # 根据编译模式设置 LD_LIBRARY_PATH
    export LD_LIBRARY_PATH="$DTLD_DIR/target/$profile:$DTLD_DIR/rdma-core-55.0/build/lib"
    echo "LD_LIBRARY_PATH: $LD_LIBRARY_PATH"
}

start_soft_switch() {
    echo "Starting soft switch simulator..."
    if [ -z "$RTL_DIR" ]; then
        echo "Error: RTL_DIR not set. Call init_test_environment first."
        exit 1
    fi

    SOFT_SWITCH="$RTL_DIR/test/soft_sw/sw.py"
    if [ ! -f "$SOFT_SWITCH" ]; then
        echo "Error: Soft switch simulator not found: $SOFT_SWITCH"
        exit 1
    fi
    python3 "$SOFT_SWITCH" > "$LOG_DIR/soft_switch.log" 2>&1 &

    SOFT_SWITCH_PID=$!

    export SOFT_SWITCH_PID
}

start_rtl_simulators_with_switch() {
    local num_instances=$1
    local test_name=${2:-"test"}

    if [ -z "$RTL_DIR" ]; then
        echo "Error: RTL_DIR not set. Call init_test_environment first."
        exit 1
    fi

    if [ -z "$LOG_DIR" ]; then
        echo "Error: LOG_DIR not set."
        exit 1
    fi

    echo "Starting RTL simulator(s)..."

    local rtl_cocotb_dir="$RTL_DIR/test/cocotb"
    cd "$rtl_cocotb_dir"

    echo "Current directory: $(pwd)"

    # verilator 编译
    make compile_verilator

    # 清空 RTL_PIDS 数组
    RTL_PIDS=()

    for i in $(seq 1 $num_instances); do
        make INST_ID=$i run_system_test_multi_node > "$LOG_DIR/rtl-$test_name-$i.log" 2>&1 &
        RTL_PIDS+=($!)
        echo "RTL instance $i PID: ${RTL_PIDS[$((i-1))]}"
    done

    # 等待 RTL 启动
    echo "Waiting for RTL to start..."
    sleep 2

    # 验证 RTL 进程是否成功启动
    echo "Verifying RTL simulators..."
    for pid in "${RTL_PIDS[@]}"; do
        if ! kill -0 $pid 2>/dev/null; then
            echo "Error: RTL process $pid failed to start or died"
            cleanup_rtl_simulators
            exit 1
        fi
    done
    echo "All RTL simulators verified running"

    # 导出 PID 数组
    export RTL_PIDS
}

# 启动 RTL 模拟器
# 参数:
#   $1: num_instances - RTL 实例数量 (1 或 2)；pcie_loopback 模式固定为 1
#   $2: test_name - 测试名称，决定日志文件名和 make target：
#         "loopback"      - 单实例以太网回环 (mkBsvTopWithoutHardIpInstance)
#         "pcie_loopback" - 单实例 PCIe 回环 (mkBsvTop，需要 sudo)
#         其他            - 双实例 send/recv（server/client）
# 返回:
#   设置 RTL_PIDS 数组，包含所有启动的 RTL 进程 PID
start_rtl_simulators() {
    local num_instances=$1
    local test_name=${2:-"test"}

    if [ -z "$RTL_DIR" ]; then
        echo "Error: RTL_DIR not set. Call init_test_environment first."
        exit 1
    fi

    if [ -z "$LOG_DIR" ]; then
        echo "Error: LOG_DIR not set."
        exit 1
    fi

    echo "Starting RTL simulator(s)..."

    local rtl_cocotb_dir="$RTL_DIR/test/cocotb"
    cd "$rtl_cocotb_dir"

    echo "Current directory: $(pwd)"

    # verilator 编译：PCIe 模式 DUT 是 mkBsvTop，其余使用默认 TOP_MODULE
    if [ "$test_name" = "pcie_loopback" ]; then
        make compile_verilator TOP_MODULE=mkBsvTop
    else
        make compile_verilator
    fi

    # 清空 RTL_PIDS 数组
    RTL_PIDS=()

    if [ "$num_instances" -eq 1 ]; then
        # 启动单个 RTL 实例
        if [ "$test_name" = "loopback" ]; then
            make run_system_test_server_loopback > "$LOG_DIR/rtl-$test_name.log" 2>&1 &
        elif [ "$test_name" = "pcie_loopback" ]; then
            # PCIe loopback 使用 mkBsvTop DUT，make target 内部含 sudo
            make run_pcie_system_test > "$LOG_DIR/rtl-$test_name.log" 2>&1 &
        else
            make run_system_test_server_1 > "$LOG_DIR/rtl-$test_name.log" 2>&1 &
        fi
        RTL_PIDS+=($!)
        echo "RTL instance 1 PID: ${RTL_PIDS[0]}"
    elif [ "$num_instances" -eq 2 ]; then
        if [ "$test_name" = "pcie_loopback" ]; then
            echo "Error: pcie_loopback mode only supports 1 instance"
            exit 1
        fi
        # 启动两个 RTL 实例
        make run_system_test_server_1 > "$LOG_DIR/rtl-server.log" 2>&1 &
        RTL_PIDS+=($!)
        echo "RTL instance 1 PID: ${RTL_PIDS[0]}"

        make run_system_test_server_2 > "$LOG_DIR/rtl-client.log" 2>&1 &
        RTL_PIDS+=($!)
        echo "RTL instance 2 PID: ${RTL_PIDS[1]}"
    else
        echo "Error: Invalid number of RTL instances: $num_instances"
        exit 1
    fi

    # 等待 RTL 启动
    echo "Waiting for RTL to start..."
    sleep 2

    # 验证 RTL 进程是否成功启动
    echo "Verifying RTL simulators..."
    for pid in "${RTL_PIDS[@]}"; do
        if ! kill -0 $pid 2>/dev/null; then
            echo "Error: RTL process $pid failed to start or died"
            cleanup_rtl_simulators
            exit 1
        fi
    done
    echo "All RTL simulators verified running"

    # 导出 PID 数组
    export RTL_PIDS
}


# 编译测试程序
# 参数:
#   $1: test_dir - 测试目录路径
build_test_program() {
    local test_dir=$1

    echo "Building test program..."

    cd "$test_dir"
    make

    if [ $? -ne 0 ]; then
        echo "Error: Failed to build test program"
        exit 1
    fi

    echo "Test program built successfully"
}

# 打印分隔线
print_separator() {
    echo "========================================"
}

# 打印测试开始信息
print_test_start() {
    local test_name=$1
    print_separator
    echo "Starting test: $test_name"
    print_separator
}

# 打印测试结束信息
print_test_end() {
    local test_name=$1
    print_separator
    echo "Test completed successfully: $test_name"
    echo "Check logs in: $LOG_DIR"
    print_separator
}

# 打印测试失败信息
print_test_failed() {
    local test_name=$1
    print_separator
    echo "Test failed: $test_name" >&2
    echo "Check logs in: $LOG_DIR" >&2
    print_separator >&2
}

# 处理 wait 返回的进程退出状态
# 参数:
#   $1: 角色名/进程名
#   $2: PID
#   $3: wait 返回码
#   $4: 日志路径（可选）
handle_process_exit_status() {
    local process_name=$1
    local pid=$2
    local exit_code=$3
    local log_path=$4

    if [ "$exit_code" -eq 0 ]; then
        echo "$process_name exited successfully (pid=$pid)"
        return 0
    fi

    if [ "$exit_code" -ge 128 ]; then
        local signal_num=$((exit_code - 128))
        local signal_name
        signal_name=$(kill -l "$signal_num" 2>/dev/null || echo "$signal_num")
        echo "Error: $process_name (pid=$pid) was terminated by signal $signal_name ($signal_num)" >&2
    else
        echo "Error: $process_name (pid=$pid) exited with code $exit_code" >&2
    fi

    if [ -n "$log_path" ]; then
        echo "Log: $log_path" >&2
    fi

    return 1
}

# 等待指定进程并检查退出状态
# 参数:
#   $1: 角色名/进程名
#   $2: PID
#   $3: 日志路径（可选）
wait_for_test_process() {
    local process_name=$1
    local pid=$2
    local log_path=$3
    local exit_code

    wait "$pid"
    exit_code=$?
    handle_process_exit_status "$process_name" "$pid" "$exit_code" "$log_path"
}

# 清理 RTL 模拟器进程
# 先尝试 SIGTERM，如果不成功则使用 SIGKILL 强制终止
# 同时清理所有子进程
# TODO 需要进一步优化
cleanup_rtl_simulators() {
    echo "========================================" >&2
    echo "CLEANUP CALLED at $(date)" >&2
    echo "RTL_PIDS array length: ${#RTL_PIDS[@]}" >&2
    echo "RTL_PIDS contents: ${RTL_PIDS[@]}" >&2
    echo "========================================" >&2

    echo "Cleaning up RTL simulators..."

    # 方法1: 使用 RTL_PIDS 数组（如果存在）
    if [ ${#RTL_PIDS[@]} -gt 0 ]; then
        for pid in "${RTL_PIDS[@]}"; do
            if kill -0 $pid 2>/dev/null; then
                echo "Terminating RTL process $pid (from RTL_PIDS)"
                # 获取所有子进程
                local children=$(pgrep -P $pid 2>/dev/null || true)
                # 发送 SIGTERM 到进程组
                kill -TERM -$pid 2>/dev/null || kill -TERM $pid 2>/dev/null
                # 等待最多 1 秒
                sleep 1
                # 强制终止
                if kill -0 $pid 2>/dev/null; then
                    kill -9 -$pid 2>/dev/null || kill -9 $pid 2>/dev/null
                    for child in $children; do
                        kill -9 $child 2>/dev/null || true
                    done
                fi
            fi
        done
    fi

    # 方法2: 主动查找所有 RTL 相关进程（确保清理干净，含 PCIe loopback testbench）
    echo "Searching for any remaining RTL processes..."
    local rtl_pids=$(pgrep -f "tb_top_for_system_test\|tb_top_pcie_system_test" 2>/dev/null || true)

    if [ -n "$rtl_pids" ]; then
        for pid in $rtl_pids; do
            if kill -0 $pid 2>/dev/null; then
                echo "Found RTL process $pid, terminating..."
                # 获取所有子进程（mkBsvTopW等）
                local children=$(pgrep -P $pid 2>/dev/null || true)
                # 终止进程组
                kill -TERM -$pid 2>/dev/null || kill -TERM $pid 2>/dev/null
                # 等待 1 秒
                sleep 1
                # 强制清理
                if kill -0 $pid 2>/dev/null; then
                    kill -9 -$pid 2>/dev/null || kill -9 $pid 2>/dev/null
                fi
                # 清理子进程
                for child in $children; do
                    kill -9 $child 2>/dev/null || true
                done
            fi
        done
    fi

    # 方法3: 清理可能残留的 mkBsvTopW 进程
    local bsv_pids=$(pgrep -f "mkBsvTopWithoutHardIpInstance" 2>/dev/null || true)
    if [ -n "$bsv_pids" ]; then
        echo "Cleaning up mkBsvTopW processes: $bsv_pids"
        kill -9 $bsv_pids 2>/dev/null || true
    fi
    
    # 清理软交换机进程
    if [ -n "$SOFT_SWITCH_PID" ]; then
        if kill -0 $SOFT_SWITCH_PID 2>/dev/null; then
            echo "Terminating soft switch simulator (PID: $SOFT_SWITCH_PID)"
            kill -TERM $SOFT_SWITCH_PID 2>/dev/null
            sleep 1
            if kill -0 $SOFT_SWITCH_PID 2>/dev/null; then
                kill -9 $SOFT_SWITCH_PID 2>/dev/null
            fi
        fi
    fi

    echo "RTL simulators cleanup completed"
}
