KERNEL_SRC ?= /lib/modules/$(shell uname -r)/build

BUILD_DIR := build
BLUERDMA_SRC_DIR := kernel-driver
UDMABUF_SRC_DIR := third_party/udmabuf

BLUERDMA_KO := bluerdma.ko
UDMABUF_KO := u-dma-buf.ko
UDMABUF_SIZE ?= 2097152
UDMABUF_PARAMS := udmabuf0=$(UDMABUF_SIZE) udmabuf1=$(UDMABUF_SIZE) udmabuf2=$(UDMABUF_SIZE) udmabuf3=$(UDMABUF_SIZE)

# Phony targets
.PHONY: all clean install uninstall modules bluerdma udmabuf help

# Default target
all: modules

help:
	@echo "Available targets:"
	@echo "  all            - Build all kernel modules (default)"
	@echo "  modules        - Same as 'all'"
	@echo "  bluerdma       - Build only bluerdma module"
	@echo "  udmabuf        - Build only udmabuf module"
	@echo "  clean          - Remove build artifacts"
	@echo "  install        - Load modules (requires root privileges)"
	@echo "  uninstall      - Unload modules (requires root privileges)"
	@echo ""
	@echo "Variables:"
	@echo "  KERNEL_SRC           - Kernel build directory (default: current kernel)"
	@echo ""
	@echo "Note: 'install' and 'uninstall' targets require root privileges to run."

$(BUILD_DIR):
	mkdir -p $@

modules: bluerdma udmabuf

bluerdma: $(BUILD_DIR)
	$(MAKE) -C $(KERNEL_SRC) M=$(CURDIR)/$(BLUERDMA_SRC_DIR) modules
	@mkdir -p $(BUILD_DIR)
	cp $(BLUERDMA_SRC_DIR)/$(BLUERDMA_KO) $(BUILD_DIR)/

udmabuf: $(BUILD_DIR)
	cd $(UDMABUF_SRC_DIR) && $(MAKE) KERNEL_SRC_DIR=$(KERNEL_SRC) all
	@mkdir -p $(BUILD_DIR)
	cp $(UDMABUF_SRC_DIR)/u-dma-buf.ko $(BUILD_DIR)/

clean:
	$(MAKE) -C $(KERNEL_SRC) M=$(CURDIR)/$(BLUERDMA_SRC_DIR) clean || true
	$(MAKE) -C $(KERNEL_SRC) M=$(CURDIR)/$(UDMABUF_SRC_DIR) clean || true
	rm -rf $(BUILD_DIR)

install:
	@echo "Loading kernel modules... (Requires root privileges)"
	modprobe ib_core
	insmod $(BUILD_DIR)/$(BLUERDMA_KO)
	insmod $(BUILD_DIR)/$(UDMABUF_KO) $(UDMABUF_PARAMS)
	@echo "Modules loaded."

uninstall:
	@echo "Unloading kernel modules... (Requires root privileges)"
	-rmmod $(basename $(UDMABUF_KO)) 2>/dev/null || true
	-rmmod $(basename $(BLUERDMA_KO)) 2>/dev/null || true
	@echo "Modules unloaded (if they were loaded)."
