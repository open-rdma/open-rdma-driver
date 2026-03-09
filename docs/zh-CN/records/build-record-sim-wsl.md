先切换到 simple-400g 分支


先安装 bsc ,运行 setup.sh
在往bashrc中插入export语句时有两个问题：
1. 路径没有用 "" 包裹
2. PATH 变量记录的是运行脚本时的变量，不会动态添加


通过apt-get 安装 iverilog
pip install cocotb cocotb-test cocotbext-pcie cocotbext-axi scapy


编译 test/cocotb
```log
(base) peng@DESKTOP-M211L3D:~/projects/rdma_all/achronix-400g/test/cocotb$ make run_system_test_server
cd /home/peng/projects/rdma_all/achronix-400g/test/cocotb
mkdir -p log
set -o pipefail ; \
COCOTB_VERILOG_DIR=/home/peng/projects/rdma_all/achronix-400g/src/verilog:/home/peng/projects/rdma_all/achronix-400g/backend/verilog COCOTB_DUT=mkBsvTopWithoutHardIpInstance BLUERDMA_SIMULATOR_INST_ID=1 BLUERDMA_DATA_BUS_WIDTH=256 python3 tb_top_for_system_test.py 2>&1 | tee /home/peng/projects/rdma_all/achronix-400g/test/cocotb/log/20251017_mkBsvTop.log.1
Traceback (most recent call last):
  File "/home/peng/projects/rdma_all/achronix-400g/test/cocotb/tb_top_for_system_test.py", line 184, in <module>
    test_top_without_hard_ip()
    ~~~~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/peng/projects/rdma_all/achronix-400g/test/cocotb/tb_top_for_system_test.py", line 167, in test_top_without_hard_ip
    cocotb_test.simulator.run(
    ~~~~~~~~~~~~~~~~~~~~~~~~~^
        "verilator",
        ^^^^^^^^^^^^
    ...<10 lines>...
        waves=True,
        ^^^^^^^^^^^
    )
    ^
  File "/home/peng/miniconda3/lib/python3.13/site-packages/cocotb_test/simulator.py", line 1231, in run
    return sim.run()
           ~~~~~~~^^
  File "/home/peng/miniconda3/lib/python3.13/site-packages/cocotb_test/simulator.py", line 203, in run
    cmds = self.build_command()
  File "/home/peng/miniconda3/lib/python3.13/site-packages/cocotb_test/simulator.py", line 1133, in build_command
    raise ValueError("Verilator executable not found.")
ValueError: Verilator executable not found.
make: *** [Makefile:85: run_system_test_server] Error 1
```
用的好像是 verilator 而不是 iverilog
sudo apt-get install verilator

之后继续
```log
(base) peng@DESKTOP-M211L3D:~/projects/rdma_all/achronix-400g/test/cocotb$ make run_system_test_server
cd /home/peng/projects/rdma_all/achronix-400g/test/cocotb
mkdir -p log
set -o pipefail ; \
COCOTB_VERILOG_DIR=/home/peng/projects/rdma_all/achronix-400g/src/verilog:/home/peng/projects/rdma_all/achronix-400g/backend/verilog COCOTB_DUT=mkBsvTopWithoutHardIpInstance BLUERDMA_SIMULATOR_INST_ID=1 BLUERDMA_DATA_BUS_WIDTH=256 python3 tb_top_for_system_test.py 2>&1 | tee /home/peng/projects/rdma_all/achronix-400g/test/cocotb/log/20251017_mkBsvTop.log.1
INFO cocotb: Running command: perl /usr/bin/verilator -cc --exe -Mdir /home/peng/projects/rdma_all/achronix-400g/test/cocotb/sim_build/mkBsvTopWithoutHardIpInstance -DCOCOTB_SIM=1 --top-module mkBsvTopWithoutHardIpInstance --vpi --public-flat-rw --prefix Vtop -o mkBsvTopWithoutHardIpInstance -LDFLAGS -Wl,-rpath,/home/peng/miniconda3/lib/python3.13/site-packages/cocotb/libs -L/home/peng/miniconda3/lib/python3.13/site-packages/cocotb/libs -lcocotbvpi_verilator --no-timing --Wno-WIDTHTRUNC --Wno-CASEINCOMPLETE --Wno-INITIALDLY -Wno-STMTDLY --autoflush --trace-fst --trace-structs --timescale 1ns/1ps /home/peng/miniconda3/lib/python3.13/site-packages/cocotb/share/lib/verilator/verilator.cpp /home/peng/projects/rdma_all/achronix-400g/src/verilog/reset_processor_v2.sv /home/peng/projects/rdma_all/achronix-400g/src/verilog/bram_single_clock_wr_with_read_bypass.v /home/peng/projects/rdma_all/achronix-400g/src/verilog/bsv_modules/SyncFIFO.v /home/peng/projects/rdma_all/achronix-400g/src/verilog/bsv_modules/SyncHandshake.v
ERROR cocotb: %Error: Specified --top-module 'mkBsvTopWithoutHardIpInstance' was not found in design.
ERROR cocotb: %Error: Exiting due to 1 error(s)
Process perl terminated with error 1
make: *** [Makefile:85: run_system_test_server] Error 1
```

backend 没有编译

之后编译 backend
```log

checking package dependencies
All packages are up to date.
bluetcl listVlogFiles.tcl -bdir build -vdir build mkBsvTop mkBsvTop | grep -i '\.v' | xargs -I {} cp {} /home/peng/projects/rdma_all/achronix-400g/backend/verilog
/home/peng/projects/rdma_all/bsc-2023.01-ubuntu-22.04/bin/core/bluetcl: error while loading shared libraries: libtcl8.6.so: cannot open shared object file: No such file or directory
```

# 安装缺失的 TCL 库
  sudo apt-get install tcl8.6 libtcl8.6

之后成功编译 backend

