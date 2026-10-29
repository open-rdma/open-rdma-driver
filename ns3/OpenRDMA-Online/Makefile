# Online TX <-> ns-3 <-> RX co-simulation over Unix domain sockets.
#
#   make            run the whole flow (hub + both RTL stages, concurrently)
#   make hub        only the network hub (waits for the two stages to connect)
#   make tx         only the TX stage (needs a hub already listening)
#   make rx         only the RX stage (needs a hub already listening)
#   make test-tx    TX unit + integration suites (m0..m3, integration)
#   make test-rx    RX unit + integration suites (m0..m4, rx_top, loopback)
#   make test       both
#
# The ns-3 engine is shared with OpenRDMA-Offline instead of copied: it is over
# a gigabyte and already configured and built, and both flows link the same
# library set.  Point NS3_DIR at another checkout to use a different one.
# Everything else in this tree is self-contained.

COCOTB_PYTHON ?= /home/l/miniconda3/envs/cocotb2/bin/python3
SIM ?= verilator
BSC ?= bsc
NS3_DIR ?= $(abspath ../OpenRDMA-Offline/Ns3-lab/ns-3)
ONLINE_RESULTS ?= $(abspath results)

RUN = NS3_DIR=$(NS3_DIR) ONLINE_RESULTS=$(ONLINE_RESULTS) SIM=$(SIM) BSC=$(BSC) \
	$(COCOTB_PYTHON) ns3/cocotb/online_runner.py

# Extra arguments for the hub, e.g.
#   make ARGS="--lossless=0 --queueSize=4p"
# See ns3/README.md for the full list.
ARGS ?=

# The tutorial's two teaching switches.  Both are read by the stage that owns
# them, and both are exported so that `make ONLINE_TX_WINDOW_PS=...` and
# `ONLINE_TX_WINDOW_PS=... make` mean the same thing.
#   ONLINE_TX_WINDOW_PS   how much simulation time one ADVANCE window covers
#   ONLINE_RX_FREE_CLOCK  set to 1 to drop the RX's gated clock
ONLINE_TX_WINDOW_PS ?=
ONLINE_RX_FREE_CLOCK ?=
export ONLINE_TX_WINDOW_PS
export ONLINE_RX_FREE_CLOCK

.PHONY: all hub tx rx test test-tx test-rx clean

all:
	$(RUN) all $(ARGS)

hub:
	$(RUN) hub $(ARGS)

tx:
	$(RUN) tx

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
