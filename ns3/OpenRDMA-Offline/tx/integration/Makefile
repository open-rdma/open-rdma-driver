# Integration: M0MessageAdapter glue + DmaPhaseAIntegration (Synchronizer,
# DmaDoneChecker, TxCommitTracker inlined) + end-to-end wrappers.  This is the
# only folder that depends on the module folders: its BSV compiles against the
# types (and DmaEngine/PacketAssembly/MrcTxScheduler) resolved via -p from
# ../m0/src ../m1/src ../m2/src ../m3/src.  It carries no BSV type copies.
#
# The module folders each own a byte-identical copy of the shared type
# packages (CommonTypes.bsv x4, TxTypes.bsv x3); `preflight` fails the build
# if any copy drifts, because BSC's package selection then becomes order-
# dependent and silently unsafe.
#
# Usage:
#   make test-integration          # end-to-end DmaPhaseA suite, SIM=verilator
#   make test-m0chain              # m0 chain e2e suite (GDRP -> frames)
#   make test-long                 # integration long
#   make test-m0chain-long         # m0chain long (huge pages, 2MiB message)
#   make test-negative             # expected-fatal cases (SIM=icarus)
#   make test                      # = test-compactor test-integration test-m0chain
# Use PYTHON pointing at a cocotb 2 interpreter (e.g. the cocotb2 conda env).
BSC ?= bsc
PYTHON ?= python3
SIM ?= verilator
TARGET ?= integration
SUITE ?= short

BUILD_DIR := build/$(TARGET)
INT_SRC := src
INT_TB := tb
M0_SRC := ../m0/src
M1_SRC := ../m1/src
M2_SRC := ../m2/src
M3_SRC := ../m3/src

TOP_integration := mkTestDmaPhaseAIntegration
TOP_FILE_integration := $(INT_TB)/TestDmaPhaseAIntegration.bsv
TOP_m0chain := mkTestM0Chain
TOP_FILE_m0chain := $(INT_TB)/TestM0Chain.bsv
TOP := $(TOP_$(TARGET))
TOP_FILE := $(TOP_FILE_$(TARGET))

BSC_PATH := +:$(abspath $(INT_TB)):$(abspath $(INT_SRC)):$(abspath $(M0_SRC)):$(abspath $(M1_SRC)):$(abspath $(M2_SRC)):$(abspath $(M3_SRC))
BSC_FLAGS := -u -check-assert -warn-action-shadowing -warn-method-urgency -show-schedule

ifeq ($(strip $(TOP)),)
$(error Unsupported TARGET '$(TARGET)'; implemented targets: integration m0chain)
endif

.PHONY: preflight compile verilog run test test-integration test-m0chain test-long test-m0chain-long test-negative clean

# The BSV copies are compared with comments stripped (bsv_identical.py): BSC's
# package selection depends on the declarations, not on the comments, so an
# annotated copy is not a hazard -- and this tree's m1 copy carries study
# annotations that a byte-exact cmp would reject.
preflight:
	@$(PYTHON) cocotb/bsv_identical.py $(M0_SRC)/CommonTypes.bsv $(M1_SRC)/CommonTypes.bsv || { echo "CommonTypes.bsv (m0 vs m1) diverged"; exit 2; }
	@$(PYTHON) cocotb/bsv_identical.py $(M1_SRC)/CommonTypes.bsv $(M2_SRC)/CommonTypes.bsv || { echo "CommonTypes.bsv (m1 vs m2) diverged"; exit 2; }
	@$(PYTHON) cocotb/bsv_identical.py $(M2_SRC)/CommonTypes.bsv $(M3_SRC)/CommonTypes.bsv || { echo "CommonTypes.bsv (m2 vs m3) diverged"; exit 2; }
	@$(PYTHON) cocotb/bsv_identical.py $(M1_SRC)/TxTypes.bsv $(M2_SRC)/TxTypes.bsv || { echo "TxTypes.bsv (m1 vs m2) diverged"; exit 2; }
	@$(PYTHON) cocotb/bsv_identical.py $(M2_SRC)/TxTypes.bsv $(M3_SRC)/TxTypes.bsv || { echo "TxTypes.bsv (m2 vs m3) diverged"; exit 2; }
	@for f in model/m2.py bfm/m2.py; do \
		cmp -s cocotb/$$f ../m1/cocotb/$$f || { echo "$$f (integration vs m1) diverged"; exit 2; }; \
		cmp -s cocotb/$$f ../m2/cocotb/$$f || { echo "$$f (integration vs m2) diverged"; exit 2; }; \
		cmp -s cocotb/$$f ../m3/cocotb/$$f || { echo "$$f (integration vs m3) diverged"; exit 2; }; \
	done
	@for f in model/m3.py bfm/m3.py; do \
		cmp -s cocotb/$$f ../m3/cocotb/$$f || { echo "$$f (integration vs m3) diverged"; exit 2; }; \
	done
	@cmp -s cocotb/dma_phasea_model.py ../m1/cocotb/dma_phasea_model.py || { echo "dma_phasea_model.py (integration vs m1) diverged"; exit 2; }
	@for f in model/common.py lib/cocotb_bsv/__init__.py lib/cocotb_bsv/methods.py lib/cocotb_bsv/packing.py lib/cocotb_bsv/pipes.py lib/cocotb_bsv/timing.py; do \
		for d in ../m0 ../m1 ../m2 ../m3; do \
			cmp -s cocotb/$$f $$d/cocotb/$$f || { echo "$$f (integration vs $$d) diverged"; exit 2; }; \
		done; \
	done
	@echo "preflight OK: CommonTypes x4, TxTypes x3 and all shared python mirrors identical"

compile: preflight
	mkdir -p $(BUILD_DIR)/bsc
	$(BSC) $(BSC_FLAGS) -sim -p $(BSC_PATH) \
		-bdir $(BUILD_DIR)/bsc -simdir $(BUILD_DIR)/bsc \
		-info-dir $(BUILD_DIR)/bsc \
		-g $(TOP) $(TOP_FILE)

verilog: preflight
	mkdir -p $(BUILD_DIR)/verilog
	$(BSC) $(BSC_FLAGS) -verilog -p $(BSC_PATH) \
		-bdir $(BUILD_DIR)/verilog -vdir $(BUILD_DIR)/verilog \
		-info-dir $(BUILD_DIR)/verilog \
		-g $(TOP) $(TOP_FILE)

run: verilog
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/sim_runner.py --target $(TARGET) --suite $(SUITE)

test: test-integration test-m0chain

test-integration:
	$(MAKE) run TARGET=integration SUITE=short SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)

test-m0chain:
	$(MAKE) run TARGET=m0chain SUITE=short SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)

test-long:
	$(MAKE) run TARGET=integration SUITE=long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)

test-m0chain-long:
	$(MAKE) run TARGET=m0chain SUITE=long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)

test-negative: verilog
	@test "$(TARGET)" = "integration" || { echo "test-negative requires TARGET=integration"; exit 2; }
	@test "$(SIM)" = "icarus" || { echo "test-negative requires SIM=icarus"; exit 2; }
	TMPDIR=/tmp TMP=/tmp TEMP=/tmp SIM=$(SIM) BSC=$(BSC) \
		$(PYTHON) cocotb/negative_runner.py

clean:
	rm -rf build
