# M3 (MrcTxScheduler): four-channel MRC TX scheduler + Ethernet encapsulator.
# Self-contained: types live in src/; standalone-verilog and yosys targets
# synthesize mkFourChannelTxScheduler from this folder alone.
#
# Usage:
#   make test                # short suite, SIM=verilator
#   make test SIM=icarus
#   make yosys               # yosys synthesis + BRAM2 resource check
# Use PYTHON pointing at a cocotb 2 interpreter (e.g. the cocotb2 conda env).
BSC ?= bsc
YOSYS ?= yosys
PYTHON ?= python3
SIM ?= verilator
SUITE ?= short

BUILD_DIR := build
VERILOG_DIR := $(BUILD_DIR)/verilog
BSC_DIR := $(BUILD_DIR)/bsc
STANDALONE_DIR := $(BUILD_DIR)/standalone-verilog
YOSYS_DIR := $(BUILD_DIR)/yosys
TOP := mkTestM3
TOP_FILE := tb/TestM3.bsv
M3_TOP := mkFourChannelTxScheduler
M3_FILE := src/MrcTxScheduler.bsv
BSC_PATH := +:$(abspath src):$(abspath tb)
BSC_FLAGS := -u -check-assert -warn-action-shadowing -warn-method-urgency -show-schedule
BSC_ROOT := $(shell dirname $$(dirname $$(readlink -f $$(command -v $(BSC)))))
BSC_VERILOG_LIB := $(BSC_ROOT)/lib/Verilog
YOSYS_LOGIC_LIBS := $(BSC_VERILOG_LIB)/FIFO1.v \
	$(BSC_VERILOG_LIB)/FIFO2.v $(BSC_VERILOG_LIB)/SizedFIFO.v \
	$(BSC_VERILOG_LIB)/RegFile.v

.PHONY: compile verilog standalone-verilog bram-check yosys test test-long clean

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

standalone-verilog:
	mkdir -p $(STANDALONE_DIR)
	$(BSC) $(BSC_FLAGS) -verilog -p $(BSC_PATH) \
		-bdir $(STANDALONE_DIR) -vdir $(STANDALONE_DIR) \
		-info-dir $(STANDALONE_DIR) \
		-g $(M3_TOP) $(M3_FILE)

bram-check:
	mkdir -p $(YOSYS_DIR)
	$(YOSYS) -l $(YOSYS_DIR)/bram-check.log -p 'read_verilog -sv $(BSC_VERILOG_LIB)/BRAM2.v; chparam -set ADDR_WIDTH 6 -set DATA_WIDTH 1024 -set MEMSIZE 64 -set PIPELINED 0 BRAM2; hierarchy -check -top BRAM2; proc; memory_collect; check; stat'

yosys: standalone-verilog bram-check
	mkdir -p $(YOSYS_DIR)
	/usr/bin/time -f 'elapsed_seconds=%e\nmax_rss_kb=%M' \
		-o $(YOSYS_DIR)/resources.txt \
		$(YOSYS) -l $(YOSYS_DIR)/synth.log -p 'read_verilog -lib -sv $(BSC_VERILOG_LIB)/BRAM2.v; read_verilog -sv $(YOSYS_LOGIC_LIBS) $(STANDALONE_DIR)/$(M3_TOP).v; hierarchy -check -top $(M3_TOP); synth -top $(M3_TOP) -noabc; check; stat; write_verilog -noattr -noexpr $(YOSYS_DIR)/$(M3_TOP)-netlist.v'

test: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --suite $(SUITE)

test-long: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --suite long

clean:
	rm -rf $(BUILD_DIR)
