BSC ?= bsc
PYTHON ?= python3
SIM ?= verilator
TARGET ?= m0_packet_buffer

BUILD_DIR := build/$(TARGET)
BSV_PATH := +:$(abspath src):$(abspath upstream):$(abspath m0_packet_buffer):$(abspath m1_packet_parser):$(abspath m2_rq_bitmap):$(abspath m3_dma_placement):$(abspath m4_ordered_queue):$(abspath tb)
BSC_FLAGS := -u -check-assert -warn-action-shadowing -warn-method-urgency -show-schedule

TOP_m0_packet_buffer := mkTestRxPacketBuffer
TOP_FILE_m0_packet_buffer := tb/TestRxPacketBuffer.bsv
TOP_m1_packet_parser := mkTestRxPacketParser
TOP_FILE_m1_packet_parser := tb/TestRxPacketParser.bsv
TOP_m2_rq_bitmap := mkTestRxRqBitmap
TOP_FILE_m2_rq_bitmap := tb/TestRxRqBitmap.bsv
TOP_m3_dma_placement := mkTestRxDmaPlacement
TOP_FILE_m3_dma_placement := tb/TestRxDmaPlacement.bsv
TOP_m4_ordered_queue := mkTestRxOrderedQueue
TOP_FILE_m4_ordered_queue := tb/TestRxOrderedQueue.bsv
TOP_rx_top := mkTestRxTop
TOP_FILE_rx_top := tb/TestRxTop.bsv
TOP_tx_rx_loopback := mkTestTxRxLoopback
TOP_FILE_tx_rx_loopback := tb/TestTxRxLoopback.bsv
TOP := $(TOP_$(TARGET))
TOP_FILE := $(TOP_FILE_$(TARGET))
TARGETS := m0_packet_buffer m1_packet_parser m2_rq_bitmap m3_dma_placement m4_ordered_queue rx_top tx_rx_loopback
UNIT_TARGETS := m0_packet_buffer m1_packet_parser m2_rq_bitmap m3_dma_placement m4_ordered_queue

PRODUCTION_TOP := mkRxTop
PRODUCTION_FILE := src/RxTop.bsv
PRODUCTION_DIR := build/production/verilog

ifeq ($(strip $(TOP)),)
$(error Unsupported TARGET '$(TARGET)'; implemented targets: $(TARGETS))
endif

.PHONY: preflight compile verilog test test-long compile-all verilog-all test-all \
	test-long-all production-verilog clean

# RX 固定在一版 legacy TX wire 基线（upstream/）。preflight 用哈希确认快照
# 本身未被意外改动；OpenRDMA-flow 上游若已演进，只提示差异，不阻止构建。
# RX 结果始终描述 upstream/BASELINE.sha256 记录的那一版契约。
preflight:
	@if ! (cd upstream && sha256sum -c BASELINE.sha256 >/dev/null 2>&1); then \
		echo "ERROR: mvp/rx/upstream snapshot 被改动；若是有意更新，请同步刷新 upstream/BASELINE.sha256"; \
		(cd upstream && sha256sum -c BASELINE.sha256) || true; \
		exit 1; \
	fi
	@for spec in \
		"CommonTypes.bsv:../tx/m0/src/CommonTypes.bsv" \
		"TxTypes.bsv:../tx/m3/src/TxTypes.bsv" \
		"PacketAssembly.bsv:../tx/m2/src/PacketAssembly.bsv" \
		"MrcTxScheduler.bsv:../tx/m3/src/MrcTxScheduler.bsv" ; do \
		upstream="$${spec%%:*}"; peer="$${spec#*:}"; \
		if [ -f "$$peer" ] && ! $(PYTHON) ../tx/integration/cocotb/bsv_identical.py "upstream/$$upstream" "$$peer"; then \
			echo "WARNING: 本树 TX 已领先于 RX 固定的 legacy 基线：$$peer 不同（RX 仍按基线验证）"; \
		fi; \
	done

compile: preflight
	mkdir -p $(BUILD_DIR)/bsc
	$(BSC) $(BSC_FLAGS) -sim -p $(BSV_PATH) \
		-bdir $(BUILD_DIR)/bsc -simdir $(BUILD_DIR)/bsc \
		-info-dir $(BUILD_DIR)/bsc -g $(TOP) $(TOP_FILE)

verilog: preflight
	rm -rf $(BUILD_DIR)/verilog
	mkdir -p $(BUILD_DIR)/verilog
	$(BSC) $(BSC_FLAGS) -verilog -p $(BSV_PATH) \
		-bdir $(BUILD_DIR)/verilog -vdir $(BUILD_DIR)/verilog \
		-info-dir $(BUILD_DIR)/verilog -g $(TOP) $(TOP_FILE)

test: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --target $(TARGET) --suite short

test-long: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --target $(TARGET) --suite long

compile-all:
	@set -e; for target in $(TARGETS); do \
		$(MAKE) --no-print-directory TARGET=$$target compile; \
	done

verilog-all:
	@set -e; for target in $(TARGETS); do \
		$(MAKE) --no-print-directory TARGET=$$target verilog; \
	done

test-all:
	@set -e; for target in $(TARGETS); do \
		$(MAKE) --no-print-directory TARGET=$$target SIM=$(SIM) test; \
	done

test-long-all:
	@set -e; for target in $(UNIT_TARGETS) rx_top tx_rx_loopback; do \
		$(MAKE) --no-print-directory TARGET=$$target SIM=$(SIM) test-long; \
	done

production-verilog: preflight
	rm -rf $(PRODUCTION_DIR)
	mkdir -p $(PRODUCTION_DIR)
	$(BSC) $(BSC_FLAGS) -elab -verilog -remove-dollar -p $(BSV_PATH) \
		-bdir $(PRODUCTION_DIR) -vdir $(PRODUCTION_DIR) \
		-info-dir $(PRODUCTION_DIR) -g $(PRODUCTION_TOP) $(PRODUCTION_FILE)

clean:
	rm -rf build
