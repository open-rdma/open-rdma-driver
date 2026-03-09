# Blue RDMA Driver

## Installation

### Clone the Project

First, clone this repository with:

```bash
git clone --recursive https://github.com/bsbds/blue-rdma-driver.git
cd blue-rdma-driver
```

### Load the Driver

```bash
make
make install
```

### Allocate Hugepages

A convenient script is provided to allocate hugepages, which are required for the driver's operation.

```bash
./scripts/hugepages.sh alloc 2048
```
Adjust `2048` to the desired number of hugepages (in MB).

## Running Examples

### Compile Dynamic Library

First, compile the necessary dynamic library used by the examples:

```bash

# uncomment the following line if 
# sudo apt install libibverbs-dev if cargo build can't find `infiniband/verbs_api.h`

cd dtld-ibverbs
cargo build

# if you want to use RTL simulator to do debug, use the following:
# cargo build --no-default-features --features sim

cd -
```

Second, compile rdma-core fork, which has the blue-rdma provider;
```bash
cd dtld-ibverbs/rdma-core-55.0
# uncomment following line to generate compile_commands.json for debug
# export EXTRA_CMAKE_FLAGS=-DCMAKE_EXPORT_COMPILE_COMMANDS=1
./build.sh
cd -
```

### Run Example

Make sure the network interface `blue0` has an IP address:
```bash
sudo ip addr add 17.34.51.10/24 dev blue0
sudo ip addr add 17.34.51.11/24 dev blue1
```

Then, navigate to the `examples` directory, compile them, and run:

```bash
cd examples
make
export LD_LIBRARY_PATH=../dtld-ibverbs/target/debug:../dtld-ibverbs/rdma-core-55.0/build/lib
RUST_LOG=debug ./loopback 8192


# setpci  -s 01:00.0 COMMAND=0x02
# setpci  -s 01:00.0 98.b=0x16
# setpci  -s 01:00.0 CAP_EXP+28.w=0x1000

```
