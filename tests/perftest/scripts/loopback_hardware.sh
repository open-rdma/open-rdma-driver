
trap "kill 0" SIGINT



SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DRIVER_DIR=$(cd "$SCRIPT_DIR/../../.." && pwd)

# 设置日志目录
mkdir -p $SCRIPT_DIR/../log/loopback/hw
LOG_DIR=$(cd "$SCRIPT_DIR/../log/loopback/hw" && pwd)

# Source 共同函数库
source $SCRIPT_DIR/../../common/test_common.sh

# 设置信号处理
setup_signal_handler

# 打印测试开始信息
print_test_start "loopback"

# 初始化测试环境
init_test_environment

# 编译 Rust 驱动（release 模式，同时设置 LD_LIBRARY_PATH）
build_rust_driver "hw"


# 运行 loopback 测试，参数是消息长度
MSG_LEN=${1:-209600}  # 默认 4096 字节
ROUND=${2:-10}  # 默认 10 轮
RUST_LOG=${RUST_LOG:-info}  # 默认 info 级别日志

echo "Running loopback test with MSG_LEN=$MSG_LEN"


echo "Running hardware test with PCI device reset..."
echo 1 | sudo tee /sys/bus/pci/devices/0000:01:00.0/remove
echo 1 | sudo tee /sys/bus/pci/rescan
sudo setpci  -s 01:00.0 COMMAND=0x02
sudo setpci  -s 01:00.0 98.b=0x16
sudo setpci  -s 01:00.0 CAP_EXP+28.w=0x1000


MSG_SIZE=${MSG_SIZE:-65536}
PERF_TEST_PARAMS="--loopback -q 2 --use_hugepages -t 8 -x 3 -s ${MSG_SIZE}"

sudo env \
	RUST_LOG=${RUST_LOG} \
	LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
	ib_write_bw -d bluerdma0 $PERF_TEST_PARAMS &> $LOG_DIR/server.log &


wait

