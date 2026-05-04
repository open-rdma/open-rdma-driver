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

    # 编译 rdma-core/provider；如果目标产物已存在，根 Makefile 会自动跳过
    (cd "$DRIVER_DIR" && make rdma-core)

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

ensure_sudo_session() {
    if ! sudo -v; then
        echo "Error: sudo authentication is required for PCIe RTL tests"
        exit 1
    fi
}

# 前置编译 RTL Verilog。
# 参数:
#   $1: flow - "default" 或 "pcie"
build_rtl_verilog_for_test() {
    local flow=${1:-"default"}

    if [ -z "$RTL_DIR" ]; then
        echo "Error: RTL_DIR not set. Call init_test_environment first."
        exit 1
    fi

    local rtl_cocotb_dir="$RTL_DIR/test/cocotb"

    echo "Building RTL Verilog for flow: $flow"
    cd "$rtl_cocotb_dir"

    if [ "$flow" = "pcie" ]; then
        make FLOW=pcie verilog
    else
        make FLOW=default verilog
    fi

    if [ $? -ne 0 ]; then
        echo "Error: Failed to build RTL Verilog for flow: $flow"
        exit 1
    fi
}

resolve_rtl_flow() {
    local test_name=${1:-"test"}

    case "$test_name" in
        pcie_loopback)
            echo "pcie"
            ;;
        *)
            echo "default"
            ;;
    esac
}

resolve_single_instance_rtl_target() {
    local test_name=${1:-"test"}

    case "$test_name" in
        loopback)
            echo "run_system_test_server_loopback"
            ;;
        pcie_loopback)
            echo "run_pcie_system_test"
            ;;
        *)
            echo "run_system_test_server_1"
            ;;
    esac
}

compile_verilator_for_test() {
    local test_name=${1:-"test"}
    local flow
    flow=$(resolve_rtl_flow "$test_name")
    local compile_log="$LOG_DIR/compile_verilator-$test_name.log"

    echo "Compiling Verilator before starting RTL simulators..."
    make FLOW="$flow" compile_verilator > "$compile_log" 2>&1
    if [ $? -ne 0 ]; then
        echo "Error: Failed to compile Verilator for flow: $flow. See log: $compile_log"
        exit 1
    fi
    echo "Verilator compile log: $compile_log"
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

    compile_verilator_for_test "$test_name"

    # 清空 RTL_PIDS 数组
    RTL_PIDS=()

    for i in $(seq 1 $num_instances); do
        make FLOW=default INST_ID=$i run_system_test_multi_node > "$LOG_DIR/rtl-$test_name-$i.log" 2>&1 &
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

    local flow
    flow=$(resolve_rtl_flow "$test_name")

    compile_verilator_for_test "$test_name"

    # 清空 RTL_PIDS 数组
    RTL_PIDS=()

    if [ "$num_instances" -eq 1 ]; then
        local target
        target=$(resolve_single_instance_rtl_target "$test_name")

        # 启动单个 RTL 实例
        make FLOW="$flow" "$target" > "$LOG_DIR/rtl-$test_name.log" 2>&1 &
        RTL_PIDS+=($!)
        echo "RTL instance 1 PID: ${RTL_PIDS[0]}"
    elif [ "$num_instances" -eq 2 ]; then
        if [ "$flow" != "default" ]; then
            echo "Error: test '$test_name' with flow '$flow' only supports 1 instance"
            exit 1
        fi
        # 启动两个 RTL 实例
        make FLOW="$flow" run_system_test_server_1 > "$LOG_DIR/rtl-server.log" 2>&1 &
        RTL_PIDS+=($!)
        echo "RTL instance 1 PID: ${RTL_PIDS[0]}"

        make FLOW="$flow" run_system_test_server_2 > "$LOG_DIR/rtl-client.log" 2>&1 &
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

# 检查进程是否存活；对于 sudo 启动的 RTL 进程，必要时使用 sudo 探测
is_process_alive() {
    local pid=$1
    ps -p "$pid" > /dev/null 2>&1
}

# 递归获取指定 PID 的所有子孙进程
get_descendant_pids() {
    local pid=$1
    local children
    local child

    children=$(pgrep -P "$pid" 2>/dev/null || sudo -n pgrep -P "$pid" 2>/dev/null || true)

    for child in $children; do
        echo "$child"
        get_descendant_pids "$child"
    done
}

# 向进程树发送信号；优先按父子树清理，避免误伤共用进程组的外层 shell
signal_process_or_group() {
    local signal=$1
    local pid=$2
    local descendants=()
    local child_pid
    local i

    while IFS= read -r child_pid; do
        if [ -n "$child_pid" ]; then
            descendants+=("$child_pid")
        fi
    done < <(get_descendant_pids "$pid")

    # 先从叶子节点开始发信号，确保 make/sh/python/tee 这类包装链条整体退出。
    for ((i=${#descendants[@]}-1; i>=0; i--)); do
        kill "-$signal" "${descendants[$i]}" 2>/dev/null ||
            sudo -n kill "-$signal" "${descendants[$i]}" 2>/dev/null ||
            true
    done

    kill "-$signal" "$pid" 2>/dev/null ||
        sudo -n kill "-$signal" "$pid" 2>/dev/null ||
        true
}

# 清理 RTL 模拟器进程
# 先尝试 SIGTERM，如果不成功则使用 SIGKILL 强制终止
# 同时清理所有子进程
# TODO 需要进一步优化
cleanup_rtl_simulators() {
    echo "Cleaning up RTL simulators..."
    local need_fallback_pkill=0

    cleanup_recorded_process() {
        local pid=$1
        local label=$2
        local descendants_before=()
        local descendant_pid
        local survivor_found=0

        while IFS= read -r descendant_pid; do
            if [ -n "$descendant_pid" ]; then
                descendants_before+=("$descendant_pid")
            fi
        done < <(get_descendant_pids "$pid")

        if ! is_process_alive "$pid" && [ ${#descendants_before[@]} -eq 0 ]; then
            return 0
        fi

        echo "Terminating $label (PID: $pid)"
        signal_process_or_group TERM "$pid"
        sleep 1

        if is_process_alive "$pid"; then
            echo "Force killing $label (PID: $pid)"
            signal_process_or_group KILL "$pid"
            sleep 0.2
        fi

        if is_process_alive "$pid"; then
            echo "Warning: $label still alive after direct cleanup (PID: $pid)" >&2
            need_fallback_pkill=1
            survivor_found=1
        fi

        for descendant_pid in "${descendants_before[@]}"; do
            if is_process_alive "$descendant_pid"; then
                echo "Warning: $label child still alive after cleanup (PID: $descendant_pid)" >&2
                need_fallback_pkill=1
                survivor_found=1
            fi
        done

        if [ "$survivor_found" -eq 1 ]; then
            echo "Falling back to command-line cleanup for $label" >&2
        fi
    }

    # 主路径：只清理由当前测试脚本启动并记录下来的进程。
    if [ ${#RTL_PIDS[@]} -gt 0 ]; then
        for pid in "${RTL_PIDS[@]}"; do
            cleanup_recorded_process "$pid" "RTL simulator"
        done
    fi

    if [ -n "$SOFT_SWITCH_PID" ]; then
        cleanup_recorded_process "$SOFT_SWITCH_PID" "soft switch simulator"
    fi

    # 兜底路径：只按 testbench 的 Python 入口清理残留。
    # 兼容 python / python3，避免误杀 sim_build 可执行文件或 tee，导致终端链路异常。
    local fallback_pattern="python(3)? tb_top_for_system_test.py|python(3)? tb_top_for_system_test_two_card.py|python(3)? tb_top_for_system_test_multi_node.py|python(3)? tb_top_pcie_system_test.py"

    if [ "$need_fallback_pkill" -eq 1 ] || \
       { pgrep -f "$fallback_pattern" >/dev/null 2>&1 || sudo -n pgrep -f "$fallback_pattern" >/dev/null 2>&1; }; then
        echo "Cleaning up remaining RTL processes by command line match..."
        local attempt

        for attempt in 1 2 3; do
            pkill -TERM -f "$fallback_pattern" 2>/dev/null || true
            sudo -n pkill -TERM -f "$fallback_pattern" 2>/dev/null || true
            sleep 0.2

            if ! pgrep -f "$fallback_pattern" >/dev/null 2>&1 && \
               ! sudo -n pgrep -f "$fallback_pattern" >/dev/null 2>&1; then
                break
            fi
        done

        if pgrep -f "$fallback_pattern" >/dev/null 2>&1 || sudo -n pgrep -f "$fallback_pattern" >/dev/null 2>&1; then
            pkill -KILL -f "$fallback_pattern" 2>/dev/null || true
            sudo -n pkill -KILL -f "$fallback_pattern" 2>/dev/null || true
            sleep 0.2
        fi
    fi

    RTL_PIDS=()
    SOFT_SWITCH_PID=

    echo "RTL simulators cleanup completed"
}
