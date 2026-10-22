# Convenience wrapper: run every suite.  Each module folder keeps its own
# self-contained Makefile; this file only dispatches into them.
PYTHON ?= python3
SIM ?= verilator
BSC ?= bsc

.PHONY: test test-all test-long test-m0chain test-negative clean

test-all:
	$(MAKE) -C m0 test SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C m1 test SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C m1 test TARGET=compactor SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C m2 test SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C m3 test SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C integration test-integration SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C integration test-m0chain SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)

test: test-all

test-long:
	$(MAKE) -C m0 test-long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C m1 test-long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C m2 test-long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C m3 test-long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C integration test-long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C integration test-m0chain-long SIM=$(SIM) PYTHON=$(PYTHON) BSC=$(BSC)

test-negative:
	$(MAKE) -C m1 test-negative SIM=icarus PYTHON=$(PYTHON) BSC=$(BSC)
	$(MAKE) -C integration test-negative SIM=icarus PYTHON=$(PYTHON) BSC=$(BSC)

clean:
	$(MAKE) -C m0 clean
	$(MAKE) -C m1 clean
	$(MAKE) -C m2 clean
	$(MAKE) -C m3 clean
	$(MAKE) -C integration clean
