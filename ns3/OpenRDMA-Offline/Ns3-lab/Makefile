SHELL := /bin/bash

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
NS3_DIR := $(ROOT)/ns-3
NS3 := $(NS3_DIR)/ns3
NS3_TAG := ns-3.48
CMAKE_BIN := $(HOME)/.local/bin/cmake
CONFIG_STAMP := $(NS3_DIR)/.openrdma-labs-configured
MODULES := core;network;internet;point-to-point;applications;flow-monitor
# The RTL side of Lab 6 and Lab 7 used to come from a separate OpenRDMA-flow
# checkout.  It now lives in the parent directory: OFFLINE_DIR is the offline
# pipeline that produces the traces, OPENRDMA_DIR the RTL sources themselves.
OFFLINE_DIR := $(abspath $(ROOT)/..)
OPENRDMA_DIR ?= $(OFFLINE_DIR)/tx
OPENRDMA_RESULTS := $(ROOT)/results/offline
COCOTB_PYTHON ?= /home/l/miniconda3/envs/cocotb2/bin/python3

TARGET_lab1 := lab1
TARGET_lab2 := lab2
TARGET_lab3 := lab3
TARGET_lab4 := lab4
TARGET_lab5 := lab5
TARGET_lab6 := lab6
TARGET_lab7 := lab7
TARGET := $(TARGET_$(LAB))

.PHONY: bootstrap links configure build test ns3-smoke run \
        lab1 lab2 lab3 lab3-repro lab4 lab5 lab5-trace \
        lab6 lab6-trace lab7 test-cosim clean

bootstrap: $(NS3)
	@command -v g++ >/dev/null || { echo "g++ is required"; exit 2; }
	@command -v python3 >/dev/null || { echo "python3 is required"; exit 2; }
	@test -x "$(CMAKE_BIN)" || { \
		echo "CMake 3.25+ is required. Install it with:"; \
		echo "  python3 -m pip install --user cmake==3.31.10"; \
		exit 2; \
	}

$(NS3):
	git clone --branch $(NS3_TAG) --depth 1 https://gitlab.com/nsnam/ns-3-dev.git $(NS3_DIR)

links: bootstrap
	ln -sfn ../common $(NS3_DIR)/common
	ln -sfn ../../Lab1-PointToPoint/main.cc $(NS3_DIR)/scratch/lab1.cc
	ln -sfn ../../Lab2-BottleneckQueue/main.cc $(NS3_DIR)/scratch/lab2.cc
	ln -sfn ../../Lab3-ControlledLoss/main.cc $(NS3_DIR)/scratch/lab3.cc
	ln -sfn ../../Lab4-FourPathReorder/main.cc $(NS3_DIR)/scratch/lab4.cc
	ln -sfn ../../Lab5-RoceFrameReplay/main.cc $(NS3_DIR)/scratch/lab5.cc
	ln -sfn ../../Lab6-OpenRdmaTrace/main.cc $(NS3_DIR)/scratch/lab6.cc
	ln -sfn ../../Lab7-OnlineSocketCosim/main.cc $(NS3_DIR)/scratch/lab7.cc

configure: $(CONFIG_STAMP)

$(CONFIG_STAMP): Makefile | links
	cd $(NS3_DIR) && PATH=$(HOME)/.local/bin:$$PATH ./ns3 configure \
		-d debug --enable-tests --enable-modules="$(MODULES)"
	touch $@

build: configure
	cd $(NS3_DIR) && PATH=$(HOME)/.local/bin:$$PATH ./ns3 build --quiet

run: build
	@test -n "$(TARGET)" || { echo "Use LAB=lab1, lab2, lab3, lab4, lab5, lab6, or lab7"; exit 2; }
	cd $(NS3_DIR) && PATH=$(HOME)/.local/bin:$$PATH ./ns3 run --no-build "$(TARGET) $(ARGS)"

lab1:
	$(MAKE) run LAB=lab1 ARGS='--output=../results/lab1.json --pcapPrefix=../results/lab1'

lab2:
	$(MAKE) run LAB=lab2 ARGS='--output=../results/lab2.json --queueTrace=../results/lab2-queue.csv'

lab3:
	$(MAKE) run LAB=lab3 ARGS='--output=../results/lab3.json --seed=20260914 --run=1'

lab3-repro: build
	cd $(NS3_DIR) && ./ns3 run --no-build '$(TARGET_lab3) --output=../results/lab3-a.json --seed=20260914 --run=1'
	cd $(NS3_DIR) && ./ns3 run --no-build '$(TARGET_lab3) --output=../results/lab3-b.json --seed=20260914 --run=1'
	cmp $(ROOT)/results/lab3-a.json $(ROOT)/results/lab3-b.json
	cd $(NS3_DIR) && ./ns3 run --no-build '$(TARGET_lab3) --output=../results/lab3-c.json --seed=20260914 --run=2'
	@if cmp -s $(ROOT)/results/lab3-a.json $(ROOT)/results/lab3-c.json; then \
		echo "different run numbers unexpectedly produced identical results"; exit 1; \
	fi
	@echo "Lab 3 reproducibility PASS"

lab4:
	$(MAKE) run LAB=lab4 ARGS='--output=../results/lab4.json --pcapPrefix=../results/lab4'

lab5-trace:
	python3 $(ROOT)/Lab5-RoceFrameReplay/generate_trace.py

lab5: lab5-trace
	$(MAKE) run LAB=lab5 ARGS='--trace=../Lab5-RoceFrameReplay/test-data/m3-trace.csv --output=../results/lab5.json --pcapPrefix=../results/lab5'

# The traces come from the offline pipeline in the parent directory.  Its
# tx-trace.csv and tx-messages.csv are byte-compatible with what
# Lab6-OpenRdmaTrace/main.cc reads.  tx-frames.csv differs by one trailing
# 'channel' column, a copy of the trace's own channel, and that reader compares
# the header for exact equality, so the column is trimmed on the way through.
# The pipeline writes into results/offline/ so that `make lab6` leaves the
# parent tree's own results untouched.
lab6-trace:
	@test -x "$(COCOTB_PYTHON)" || { echo "cocotb Python not found: $(COCOTB_PYTHON)"; exit 2; }
	@test -f "$(OPENRDMA_DIR)/integration/Makefile" || { echo "RTL tree not found: $(OPENRDMA_DIR)"; exit 2; }
	$(MAKE) -C $(OFFLINE_DIR) tx OFFLINE_RESULTS=$(OPENRDMA_RESULTS) \
		SIM=verilator COCOTB_PYTHON=$(COCOTB_PYTHON)
	cp $(OPENRDMA_RESULTS)/tx-trace.csv $(ROOT)/results/lab6-trace.csv
	cp $(OPENRDMA_RESULTS)/tx-messages.csv $(ROOT)/results/lab6-messages.csv
	cut -d, -f1-11 $(OPENRDMA_RESULTS)/tx-frames.csv > $(ROOT)/results/lab6-frames.csv

lab6: lab6-trace
	$(MAKE) run LAB=lab6 ARGS='--trace=../results/lab6-trace.csv --frameOracle=../results/lab6-frames.csv --messageOracle=../results/lab6-messages.csv --output=../results/lab6.json --pcapPrefix=../results/lab6'

# Lab 7's RTL side is the 'lab7' cocotb suite, which belongs to the online
# co-simulation and is not part of the offline pipeline.  The socket protocol
# in socket-protocol.h and the ns-3 server here are kept as the reference
# implementation of that approach; running them needs the suite, so the target
# reports the missing piece instead of failing somewhere inside cocotb.
lab7: build
	@test -f "$(OPENRDMA_DIR)/integration/cocotb/tests/test_m0_chain_lab7.py" || { \
		echo "Lab 7 needs the 'lab7' cocotb suite, absent from $(OPENRDMA_DIR)."; \
		echo "The offline pipeline supersedes this online co-simulation; see 2.7 of the manual."; \
		exit 2; }
	OPENRDMA_DIR=$(OPENRDMA_DIR) COCOTB_PYTHON=$(COCOTB_PYTHON) \
		$(COCOTB_PYTHON) $(ROOT)/Lab7-OnlineSocketCosim/run_cosim.py

test-cosim: lab6 lab7
	python3 $(ROOT)/Lab7-OnlineSocketCosim/compare_results.py

test: lab1 lab2 lab3-repro lab4 lab5
	@echo "All five standalone ns-3 labs PASS"

ns3-smoke: build
	cd $(NS3_DIR) && ./test.py -n -s packet
	cd $(NS3_DIR) && ./test.py -n -s devices-point-to-point
	cd $(NS3_DIR) && ./test.py -n -s udp

clean:
	cd $(NS3_DIR) && ./ns3 clean
	rm -f $(CONFIG_STAMP)
	rm -rf $(ROOT)/results/*
