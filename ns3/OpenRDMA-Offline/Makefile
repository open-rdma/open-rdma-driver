# Offline TX -> ns-3 -> RX co-simulation.
#
#   make            run the whole flow (TX RTL -> ns-3 -> RX RTL)
#   make tx         only produce results/tx-*.csv from the TX RTL
#   make ns3        only replay the trace over the network
#                   (ARGS="..." is forwarded to the ns-3 program)
#   make rx         only replay the arrival trace into the RX RTL
#   make test-tx    TX unit + integration suites (m0..m3, integration)
#   make test-rx    RX unit + integration suites (m0..m4, rx_top, loopback)
#   make test       both
#
# The ns-3 engine is shared with the lab tree instead of copied (it is over a
# gigabyte and already configured): it sits at Ns3-lab/ns-3, one level down.
# Point NS3_DIR at another checkout to use a different one.  Everything else in
# this tree is self-contained.

COCOTB_PYTHON ?= /home/l/miniconda3/envs/cocotb2/bin/python3
SIM ?= verilator
BSC ?= bsc
NS3_DIR ?= $(abspath Ns3-lab/ns-3)
OFFLINE_RESULTS ?= $(abspath results)

RUN = NS3_DIR=$(NS3_DIR) OFFLINE_RESULTS=$(OFFLINE_RESULTS) SIM=$(SIM) BSC=$(BSC) \
	$(COCOTB_PYTHON) ns3/cocotb/offline_runner.py

# Extra arguments for the ns-3 program, e.g.
#   make ns3 ARGS="--lossless=0 --queueSize=4p"
# (also honoured by `make all`).  See ns3/README.md for the full list.
ARGS ?=

.PHONY: all tx ns3 rx test test-tx test-rx clean

all:
	$(RUN) all $(ARGS)

tx:
	$(RUN) tx

ns3:
	$(RUN) ns3 $(ARGS)

rx:
	$(RUN) rx

test-tx:
	$(MAKE) -C tx test-all SIM=$(SIM) PYTHON=$(COCOTB_PYTHON) BSC=$(BSC)

test-rx:
	$(MAKE) -C rx test-all SIM=$(SIM) PYTHON=$(COCOTB_PYTHON) BSC=$(BSC)

test: test-tx test-rx

clean:
	$(MAKE) -C tx clean
	$(MAKE) -C rx clean
	rm -rf ns3/build results
