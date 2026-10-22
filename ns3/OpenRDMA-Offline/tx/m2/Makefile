# M2 (PacketAssembly): MessageContext + payload beats -> PMTU-size packets
# carrying descriptor metadata (PSN, per-packet VA/rkey/MSN) and payload beats.
# M2 does not serialize any wire header; Ethernet/IP/UDP/BTH/RETH are built in
# M3.  Self-contained: types live in src/.
#
# Usage:
#   make test                # short suite, SIM=verilator
#   make test SIM=icarus
# Use PYTHON pointing at a cocotb 2 interpreter (e.g. the cocotb2 conda env).
BSC ?= bsc
PYTHON ?= python3
SIM ?= verilator
SUITE ?= short

BUILD_DIR := build
VERILOG_DIR := $(BUILD_DIR)/verilog
BSC_DIR := $(BUILD_DIR)/bsc
TOP := mkTestM2
TOP_FILE := tb/TestM2.bsv
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
