# Base Test — RDMA Test Framework

A refactored RDMA test framework that separates the library from test cases, providing clean and maintainable test code.

## Directory Structure

```
base_test/
├── lib/          → Shared library
│   ├── rdma_common.*      - RDMA basic operations (device, QP, buffer)
│   ├── rdma_transport.*   - Transport abstraction (TCP connection, info exchange)
│   └── rdma_debug.*       - Debug utilities (memory diff, printing)
├── tests/        → Test cases
│   ├── loopback.c         - Loopback test
│   ├── send_recv.c        - Send/Recv test
│   ├── write_with_imm.c   - RDMA WRITE with Immediate
│   └── rdma_write.c       - RDMA WRITE multi-round test
├── scripts/      → Automated test scripts (Sim mode)
└── build/        → Build artifacts
    ├── obj/      - Library object files
    └── bin/      - Executables
```

## Quick Start

### 1. Build Tests

```bash
make              # Build all tests
make clean        # Clean build artifacts
make list         # List available tests
```

### 2. Run Tests

#### Mock mode (no RTL simulator required)

Run the compiled test programs directly:

```bash
# Loopback test
./build/bin/loopback 4096

# Send/Recv test (requires two terminals)
./build/bin/send_recv 4096              # Terminal 1: Server
./build/bin/send_recv 4096 127.0.0.1    # Terminal 2: Client

# RDMA WRITE test
./build/bin/rdma_write 8192 server 1 5              # Terminal 1: Server
./build/bin/rdma_write 8192 client 127.0.0.1 0 5    # Terminal 2: Client

# WRITE with Immediate
./build/bin/write_with_imm 4096              # Terminal 1: Server
./build/bin/write_with_imm 4096 127.0.0.1    # Terminal 2: Client
```

#### Sim mode (requires RTL simulator)

Use the automated scripts, which automatically start the RTL simulator, build the driver and test programs.

**Environment setup: configure the RTL path**

Clone the `open-rdma-rtl` repository into the same parent directory as `open-rdma-driver`:

```bash
# Run from the parent directory
cd /path/to/parent-directory
git clone https://github.com/open-rdma/open-rdma-rtl.git
```

Directory structure:
```
parent-directory/
├── open-rdma-driver/
│   └── tests/base_test/  ← current directory
└── open-rdma-rtl/        ← RTL repository
```

Or set an environment variable to specify a custom path:
```bash
export RTL_DIR=/path/to/your/open-rdma-rtl
```

**Running tests:**

```bash
cd scripts/

# Run individual tests
./test_loopback_sim.sh 4096
./test_send_recv_sim.sh 4096
./test_rdma_write_sim.sh 4096 5
./test_write_imm_sim.sh 4096

# Run all tests
./run_all_tests.sh
```

Test logs are saved in the `log/sim/` directory:
```bash
cat log/sim/rtl-loopback.log                # Loopback log
cat log/sim/send_recv/server.log            # Send/Recv server log
tail -f log/sim/rdma_write/client.log       # Live view of client log
```

## Library API Documentation

### rdma_common — RDMA Basic Operations

```c
// Initialization
struct rdma_context ctx;
struct rdma_config config;
rdma_default_config(&config);
config.dev_index = 0;
config.buffer_size = 4096;
rdma_init_context(&ctx, &config);

// QP state transition
rdma_connect_qp(qp, dest_qp_num);  // Single-step transition

// Cleanup
rdma_destroy_context(&ctx);
```

### rdma_transport — Transport Abstraction

```c
// Server side
struct tcp_transport transport;
tcp_server_init(&transport, port);
tcp_server_accept(&transport);
rdma_exchange_qp_info(transport.client_fd, &local_info, &remote_info);

// Client side
tcp_client_connect(&transport, server_ip, port, max_retries);
rdma_exchange_qp_info(transport.sock_fd, &local_info, &remote_info);

// Cleanup
tcp_transport_close(&transport);
```

### rdma_debug — Debug Utilities

```c
rdma_memory_diff(expected, actual, length);    // Memory diff
rdma_print_memory_hex(buffer, length);         // Print memory as hex
COMPILER_BARRIER();                            // Compiler barrier
```

## Adding New Tests

Create a new file in the `tests/` directory:

```c
#include "../lib/rdma_common.h"
#include "../lib/rdma_transport.h"
#include "../lib/rdma_debug.h"

int main(int argc, char *argv[]) {
    struct rdma_context ctx;
    struct rdma_config config;
    rdma_default_config(&config);
    config.buffer_size = 4096;

    rdma_init_context(&ctx, &config);
    // Test logic...
    rdma_destroy_context(&ctx);
    return 0;
}
```

Running `make` will automatically compile the new test.

## Troubleshooting

### RTL directory not found
```
Error: RTL directory not found
```
**Solution**: Confirm that `open-rdma-rtl` is in the same parent directory as `open-rdma-driver`, or set the `RTL_DIR` environment variable.

### Device not found
```
[ERROR] Device index 0 not available
```
**Solution**: Ensure `LD_LIBRARY_PATH` includes the paths to libibverbs and the driver library.

### Build error
```
fatal error: rdma_common.h: No such file or directory
```
**Solution**: Use `#include "../lib/rdma_common.h"` instead of `#include "rdma_common.h"`.

## References

- [scripts/README.md](scripts/README.md) - Test script usage guide
- [lib/rdma_common.h](lib/rdma_common.h) - API definitions
- [tests/loopback.c](tests/loopback.c) - Example test code
