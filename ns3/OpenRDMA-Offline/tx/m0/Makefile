# M0 (TxMvp): GDRP submission -> slot/MR/QP tables -> PGT translation ->
# physical DMA chunk request stream.  Self-contained: types live in src/.
#
# Usage:
#   make test                # short suite, SIM=verilator
#   make test SIM=icarus
#   make test-long           # 4096-submission randomized suite
# Use PYTHON pointing at a cocotb 2 interpreter (e.g. the cocotb2 conda env).
BSC ?= bsc
PYTHON ?= python3
SIM ?= verilator
SUITE ?= short

BUILD_DIR := build
VERILOG_DIR := $(BUILD_DIR)/verilog
BSC_DIR := $(BUILD_DIR)/bsc
TOP := mkTestTxMvp
TOP_FILE := tb/TestTxMvp.bsv
BSC_PATH := +:$(abspath src):$(abspath tb)
BSC_FLAGS := -u -check-assert -warn-action-shadowing -warn-method-urgency -show-schedule

.PHONY: compile verilog test test-long clean

compile:
	mkdir -p $(BSC_DIR)
	$(BSC) $(BSC_FLAGS) -sim -p $(BSC_PATH) \
		-bdir $(BSC_DIR) -simdir $(BSC_DIR) -info-dir $(BSC_DIR) \
		-g $(TOP) $(TOP_FILE)

verilog:
	mkdir -p $(VERILOG_DIR)
	$(BSC) $(BSC_FLAGS) -verilog -p $(BSC_PATH) \
		-bdir $(VERILOG_DIR) -vdir $(VERILOG_DIR) -info-dir $(VERILOG_DIR) \
		-g $(TOP) $(TOP_FILE)

test: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --suite $(SUITE)

test-long: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --suite long

clean:
	rm -rf $(BUILD_DIR)
