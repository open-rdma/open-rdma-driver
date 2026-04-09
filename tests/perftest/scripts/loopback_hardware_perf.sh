
trap "kill 0" SIGINT


SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DRIVER_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)

# 设置日志目录
mkdir -p $SCRIPT_DIR/../log/loopback/hw
LOG_DIR=$(cd "$SCRIPT_DIR/../log/loopback/hw" && pwd)

# perf 输出目录
PERF_DIR="$LOG_DIR/perf"
mkdir -p "$PERF_DIR"

# FlameGraph 工具路径（如未安装请先 git clone https://github.com/brendangregg/FlameGraph）
FLAMEGRAPH_DIR=${FLAMEGRAPH_DIR:-"$HOME/rdma_all/FlameGraph"}

# Source 共同函数库
source $SCRIPT_DIR/../../common/test_common.sh

# 设置信号处理
setup_signal_handler

# 打印测试开始信息
print_test_start "loopback_perf"

# 初始化测试环境
init_test_environment

# 编译 Rust 驱动（release 模式，同时设置 LD_LIBRARY_PATH）
build_rust_driver "hw" "release"


# 运行 loopback 测试，参数是消息长度
MSG_LEN=${1:-65536}
ROUND=${2:-10}
RUST_LOG=${RUST_LOG:-info}

# perf 采样频率（Hz），默认 99
PERF_FREQ=${PERF_FREQ:-99}

echo "Running loopback perf test with MSG_LEN=$MSG_LEN"


echo "Running hardware test with PCI device reset..."
echo 1 | sudo tee /sys/bus/pci/devices/0000:01:00.0/remove
echo 1 | sudo tee /sys/bus/pci/rescan
sudo setpci  -s 01:00.0 COMMAND=0x02
sudo setpci  -s 01:00.0 98.b=0x16
sudo setpci  -s 01:00.0 CAP_EXP+28.w=0x1000

# 允许非特权用户采集内核栈（临时）
echo -1 | sudo tee /proc/sys/kernel/perf_event_paranoid > /dev/null

MSG_SIZE=${MSG_SIZE:-524288}
PERF_TEST_PARAMS="--loopback -q 60 --use_hugepages -n 50 -t 1 -x 3 -s ${MSG_SIZE}"

# 直接用 perf record 启动 ib_write_bw，perf 全程跟踪该进程
PERF_DATA="$PERF_DIR/perf.data"
echo "Starting perf record (freq=${PERF_FREQ}Hz) directly wrapping ib_write_bw ..."
sudo perf record \
    -F ${PERF_FREQ} \
    -g --call-graph dwarf \
    -o "$PERF_DATA" \
    -- env \
        RUST_LOG=${RUST_LOG} \
        LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
        ib_write_bw -d bluerdma0 $PERF_TEST_PARAMS \
    &> $LOG_DIR/server.log

echo "perf record finished. Data saved to $PERF_DATA"

# 生成火焰图（需要 FlameGraph 工具）
if [ -d "$FLAMEGRAPH_DIR" ]; then
    echo "Generating flame graph..."
    PERF_SCRIPT="$PERF_DIR/perf.unfold"
    PERF_FOLDED="$PERF_DIR/perf.folded"
    FLAMEGRAPH_SVG="$PERF_DIR/flamegraph.svg"

    sudo perf script -i "$PERF_DATA" > "$PERF_SCRIPT"
    "$FLAMEGRAPH_DIR/stackcollapse-perf.pl" "$PERF_SCRIPT" > "$PERF_FOLDED"
    "$FLAMEGRAPH_DIR/flamegraph.pl" "$PERF_FOLDED" > "$FLAMEGRAPH_SVG"

    echo "Flame graph saved to: $FLAMEGRAPH_SVG"
else
    echo "FlameGraph tools not found at $FLAMEGRAPH_DIR"
    echo "To generate flame graph, run:"
    echo "  git clone https://github.com/brendangregg/FlameGraph ~/FlameGraph"
    echo "  sudo perf script -i $PERF_DATA | ~/FlameGraph/stackcollapse-perf.pl | ~/FlameGraph/flamegraph.pl > $PERF_DIR/flamegraph.svg"
fi

wait
