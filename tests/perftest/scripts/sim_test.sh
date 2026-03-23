
PERF_TEST_PARAMS="--use_hugepages --data_validation_debug -t 1 -x 3"
RUST_LOG=${RUST_LOG:-info}
PERFTEST_CMD="ib_write_bw"


# 设置目录路径
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DRIVER_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)

# 设置日志目录
mkdir -p $SCRIPT_DIR/../log/sim
LOG_DIR=$(cd "$SCRIPT_DIR/../log/sim" && pwd)

# Source 共同函数库
source $SCRIPT_DIR/../../common/test_common.sh

# 设置信号处理
setup_signal_handler

# 打印测试开始信息
print_test_start "RCCL nompi sim"

# 初始化测试环境
init_test_environment

# 编译 Rust 驱动
build_rust_driver "sim"

# 启动 RTL 模拟器（2个实例）
start_rtl_simulators 2 "rccl"



cd $SCRIPT_DIR/..
sudo env \
	RUST_LOG=${RUST_LOG} \
	LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
	$PERFTEST_CMD -d bluerdma0 $PERF_TEST_PARAMS  &

sudo env \
	RUST_LOG=${RUST_LOG} \
	LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
	$PERFTEST_CMD -d bluerdma1 $PERF_TEST_PARAMS 127.0.0.1 &> $LOG_DIR/client.log &

wait

