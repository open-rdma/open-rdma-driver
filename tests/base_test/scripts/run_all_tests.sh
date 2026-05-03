#!/bin/bash

# 运行所有 base_test 测试套件
# 用法: ./run_all_tests.sh

set -e  # 遇到错误立即退出

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 测试结果跟踪
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

# 测试结果数组
declare -a TEST_RESULTS

# 运行单个测试
run_test() {
    local test_name=$1
    local test_script=$2
    shift 2
    local test_args="$@"

    TOTAL_TESTS=$((TOTAL_TESTS + 1))

    echo ""
    echo "=========================================="
    echo -e "${YELLOW}Running: $test_name${NC}"
    echo "Script: $test_script $test_args"
    echo "=========================================="

    if "$SCRIPT_DIR/$test_script" $test_args; then
        echo -e "${GREEN}✓ $test_name PASSED${NC}"
        PASSED_TESTS=$((PASSED_TESTS + 1))
        TEST_RESULTS+=("PASS: $test_name")
    else
        echo -e "${RED}✗ $test_name FAILED${NC}"
        FAILED_TESTS=$((FAILED_TESTS + 1))
        TEST_RESULTS+=("FAIL: $test_name")
    fi
}

# 打印测试套件标题
echo "=========================================="
echo "  RDMA Base Test Suite - RTL Simulator"
echo "=========================================="
echo ""

# 运行所有测试
# 注意：这些测试需要 RTL 模拟器环境

echo "Starting test suite..."
echo ""

# 1. Loopback 测试（单端，不需要 server/client）
run_test "Loopback (4KB)" test_loopback_sim.sh 4096

# 2. PCIe Loopback 测试（单端）
run_test "PCIe Loopback (4KB)" test_pcie_loopback_sim.sh 4096

# 3. Send/Recv 测试
run_test "Send/Recv (4KB)" test_send_recv_sim.sh 4096

# 4. RDMA WRITE 测试
run_test "RDMA WRITE (4KB, 5 rounds)" test_rdma_write_sim.sh 4096 5

# 5. WRITE with Immediate 测试
run_test "WRITE with IMM (4KB)" test_write_imm_sim.sh 4096

# 6. WRITE with Immediate 单次测试（零长度）
run_test "WRITE with IMM Single (zero-length)" test_write_imm_sim.sh 0

# 7. WRITE with Immediate 单次测试（4KB）
run_test "WRITE with IMM Single (4KB)" test_write_imm_sim.sh 4096

# 打印测试总结
echo ""
echo "=========================================="
echo "          Test Suite Summary"
echo "=========================================="
echo ""

for result in "${TEST_RESULTS[@]}"; do
    if [[ $result == PASS:* ]]; then
        echo -e "${GREEN}$result${NC}"
    else
        echo -e "${RED}$result${NC}"
    fi
done

echo ""
echo "=========================================="
echo -e "Total:  $TOTAL_TESTS"
echo -e "${GREEN}Passed: $PASSED_TESTS${NC}"
echo -e "${RED}Failed: $FAILED_TESTS${NC}"
echo "=========================================="

if [ $FAILED_TESTS -eq 0 ]; then
    echo -e "${GREEN}All tests passed! ✓${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed! ✗${NC}"
    exit 1
fi
