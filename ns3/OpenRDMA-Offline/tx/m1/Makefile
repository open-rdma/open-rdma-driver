# M1: DmaEngine (mvp half: out-of-order completion -> ordered payload stream)
# and DmaPayloadCompactor (phasea half: raw 128B beats -> MessagePayloadBeat
# stream consumed by M2).  Self-contained: types live in src/.
#
# Usage:
#   make test                        # dma short suite, SIM=verilator
#   make test TARGET=compactor       # compactor unit suite
#   make test-long                   # dma 512-message randomized suite
#   make test-negative               # dma expected-fatal cases (SIM=icarus)
# Use PYTHON pointing at a cocotb 2 interpreter (e.g. the cocotb2 conda env).
BSC ?= bsc
PYTHON ?= python3
SIM ?= verilator
IVERILOG ?= iverilog
VERILATOR ?= verilator
QUARTUS_SH ?= quartus_sh
QUARTUS_FAMILY ?= Agilex 7
QUARTUS_DEVICE ?= AGIB027R31B1E2VR0
TARGET ?= dma
SUITE ?= short

BUILD_DIR := build/$(TARGET)
TOP_dma := mkTestDmaEngine
TOP_FILE_dma := tb/TestDmaEngine.bsv
TOP_compactor := mkTestDmaPayloadCompactor
TOP_FILE_compactor := tb/TestDmaPayloadCompactor.bsv
TOP := $(TOP_$(TARGET))
TOP_FILE := $(TOP_FILE_$(TARGET))
BSC_PATH := +:$(abspath src):$(abspath tb)
BSC_FLAGS := -u -check-assert -warn-action-shadowing -warn-method-urgency -show-schedule

# Production RTL is built directly from mkDmaEngine.  It deliberately excludes
# the Cocotb wrapper and uses a separate clean build directory.
PRODUCTION_TOP := mkDmaEngine
PRODUCTION_FILE := src/DmaEngine.bsv
PRODUCTION_BSC_PATH := +:$(abspath src)
PRODUCTION_BSC_DIR := build/production/bsc
QUARTUS_BUNDLE_DIR := build/quartus
QUARTUS_CHECK_DIR := build/quartus-check
QUARTUS_SYNTH_DIR := build/quartus-synth

ifeq ($(strip $(TOP)),)
$(error Unsupported TARGET '$(TARGET)'; implemented targets: dma compactor)
endif

.PHONY: compile verilog production-verilog quartus-rtl check-quartus-rtl \
	quartus-synth test test-long test-negative clean

compile:
	mkdir -p $(BUILD_DIR)/bsc
	$(BSC) $(BSC_FLAGS) -sim -p $(BSC_PATH) \
		-bdir $(BUILD_DIR)/bsc -simdir $(BUILD_DIR)/bsc \
		-info-dir $(BUILD_DIR)/bsc \
		-g $(TOP) $(TOP_FILE)

verilog:
	rm -rf $(BUILD_DIR)/verilog
	mkdir -p $(BUILD_DIR)/verilog
	$(BSC) $(BSC_FLAGS) -verilog -p $(BSC_PATH) \
		-bdir $(BUILD_DIR)/verilog -vdir $(BUILD_DIR)/verilog \
		-info-dir $(BUILD_DIR)/verilog \
		-g $(TOP) $(TOP_FILE)

production-verilog:
	rm -rf $(PRODUCTION_BSC_DIR)
	mkdir -p $(PRODUCTION_BSC_DIR)
	$(BSC) $(BSC_FLAGS) -elab -verilog -remove-dollar \
		-p $(PRODUCTION_BSC_PATH) \
		-bdir $(PRODUCTION_BSC_DIR) -vdir $(PRODUCTION_BSC_DIR) \
		-info-dir $(PRODUCTION_BSC_DIR) \
		-g $(PRODUCTION_TOP) $(PRODUCTION_FILE)

quartus-rtl: production-verilog
	$(PYTHON) tools/export_quartus_rtl.py \
		--bsc $(BSC) \
		--collector tools/listVlogFiles.tcl \
		--bdir $(PRODUCTION_BSC_DIR) \
		--vdir $(PRODUCTION_BSC_DIR) \
		--top $(PRODUCTION_TOP) \
		--output $(QUARTUS_BUNDLE_DIR)

check-quartus-rtl: quartus-rtl
	rm -rf $(QUARTUS_CHECK_DIR)
	mkdir -p $(QUARTUS_CHECK_DIR)
	cd $(QUARTUS_BUNDLE_DIR) && \
		$(IVERILOG) -g2012 -s $(PRODUCTION_TOP) \
		-o $(abspath $(QUARTUS_CHECK_DIR))/$(PRODUCTION_TOP).vvp \
		-f dma_engine.f
	cd $(QUARTUS_BUNDLE_DIR) && \
		$(VERILATOR) --lint-only --Wno-fatal \
		--top-module $(PRODUCTION_TOP) -f dma_engine.f

quartus-synth: quartus-rtl
	rm -rf $(QUARTUS_SYNTH_DIR)
	mkdir -p $(QUARTUS_SYNTH_DIR)
	$(QUARTUS_SH) -t tools/quartus_synth.tcl \
		$(abspath $(QUARTUS_BUNDLE_DIR)) \
		$(abspath $(QUARTUS_SYNTH_DIR)) \
		$(PRODUCTION_TOP) "$(QUARTUS_FAMILY)" $(QUARTUS_DEVICE)

test: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --target $(TARGET) --suite $(SUITE)

test-long: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --target $(TARGET) --suite long

test-negative: verilog
	@test "$(TARGET)" = "dma" || { echo "test-negative requires TARGET=dma"; exit 2; }
	@test "$(SIM)" = "icarus" || { echo "test-negative requires SIM=icarus"; exit 2; }
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/negative_runner.py

clean:
	rm -rf build
