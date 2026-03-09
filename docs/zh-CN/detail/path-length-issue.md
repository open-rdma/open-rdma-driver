# rdma-core 编译路径长度问题

## 问题描述

在编译 `rdma-core-55.0` 时，如果项目所在的绝对路径过长，会导致编译失败。编译通常在 81% 左右时失败，具体表现为在编译 `ibacm/src/acm.c` 文件时出现 `BUILD_ASSERT` 错误。

## 错误日志示例

```log
[ 77%] Building C object infiniband-diags/CMakeFiles/perfquery.dir/perfquery.c.o
[ 77%] Linking C executable ../bin/perfquery
[ 77%] Built target perfquery
[ 77%] Building C object infiniband-diags/CMakeFiles/saquery.dir/saquery.c.o
[ 77%] Linking C executable ../bin/saquery
[ 77%] Built target saquery
[ 77%] Building C object infiniband-diags/CMakeFiles/sminfo.dir/sminfo.c.o
[ 77%] Linking C executable ../bin/sminfo
[ 77%] Built target sminfo
[ 78%] Building C object infiniband-diags/CMakeFiles/smpdump.dir/smpdump.c.o
[ 78%] Linking C executable ../bin/smpdump
[ 78%] Built target smpdump
[ 78%] Building C object infiniband-diags/CMakeFiles/smpquery.dir/smpquery.c.o
[ 79%] Linking C executable ../bin/smpquery
[ 79%] Built target smpquery
[ 80%] Building C object infiniband-diags/CMakeFiles/vendstat.dir/vendstat.c.o
[ 80%] Linking C executable ../bin/vendstat
[ 80%] Built target vendstat
[ 80%] Building C object infiniband-diags/CMakeFiles/ibsendtrap.dir/ibsendtrap.c.o
[ 80%] Linking C executable ../bin/ibsendtrap
[ 80%] Built target ibsendtrap
[ 80%] Building C object infiniband-diags/CMakeFiles/mcm_rereg_test.dir/mcm_rereg_test.c.o
[ 80%] Linking C executable ../bin/mcm_rereg_test
[ 80%] Built target mcm_rereg_test
[ 81%] Building C object ibacm/CMakeFiles/ibacm.dir/src/acm.c.o
In file included from /home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/build/include/ccan/minmax.h:7,
                 from /home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/ibacm/linux/osd.h:48,
                 from /home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/ibacm/src/acm.c:38:
/home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/ibacm/src/acm.c: In function 'acm_listen':
/home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/build/include/ccan/build_assert.h:23:33: error: size of unnamed array is negative
   23 | do { (void) sizeof(char [1 - 2*!(cond)]); } while(0)
      |                                 ^

/home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/ibacm/src/acm.c:628:17: note: in expansion of macro 'BUILD_ASSERT'
  628 |                 BUILD_ASSERT(sizeof(IBACM_IBACME_SERVER_PATH) <=
      |                 ^~~~~~~~~~~~
make[2]: *** [ibacm/CMakeFiles/ibacm.dir/build.make:76: ibacm/CMakeFiles/ibacm.dir/src/acm.c.o] Error 1
make[1]: *** [CMakeFiles/Makefile2:2672: ibacm/CMakeFiles/ibacm.dir/all] Error 2
make: *** [Makefile:136: all] Error 2
```

## 技术细节

### 问题根源

`BUILD_ASSERT` 宏用于在编译期检查某个条件是否为真。在 `ibacm/src/acm.c` 文件中，有如下断言：

```c
BUILD_ASSERT(sizeof(IBACM_IBACME_SERVER_PATH) <= sizeof(addr.sun_path));
```

这个断言确保 `IBACM_IBACME_SERVER_PATH` 字符串（Unix domain socket 路径）不超过 `sockaddr_un.sun_path` 的最大长度（通常为 108 字节）。

### 路径过长的影响

`IBACM_IBACME_SERVER_PATH` 宏会被展开为包含完整构建路径的字符串，例如：

```
/home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/build/var/run/ibacm.sock
```

当项目路径过长时，展开后的完整路径会超过 Unix domain socket 允许的最大路径长度（108字节），导致 `BUILD_ASSERT` 失败，从而中止编译。

### BUILD_ASSERT 机制

```c
#define BUILD_ASSERT(cond) \
do { (void) sizeof(char [1 - 2*!(cond)]); } while(0)
```

- 如果 `cond` 为真，数组大小为 `char[1]`，合法
- 如果 `cond` 为假，数组大小为 `char[-1]`，编译器报错："size of unnamed array is negative"

## 解决方案

### 1. 使用较短的项目路径（推荐）

将项目放置在路径较短的目录中，例如：

**推荐路径**:
```bash
/home/user/blue-rdma-driver/
```

**不推荐的路径**:
```bash
/home/peng/projects/rdma_all/blue-rdma-driver-fork/
```

### 2. 最大安全路径长度

根据经验，项目根目录的绝对路径应控制在 **50 个字符以内**，以确保编译成功。

计算公式：
- Unix socket 最大路径：108 字节
- rdma-core 内部子路径：约 58 字节 (`/dtld-ibverbs/rdma-core-55.0/build/var/run/ibacm.sock`)
- 安全的项目根路径：108 - 58 = **50 字节**

### 3. 检查当前路径长度

可以使用以下命令检查当前项目路径的长度：

```bash
pwd | wc -c
```

如果输出超过 50，建议迁移项目到更短的路径。

### 4. 迁移项目步骤

如果已经在较长路径中克隆了项目，可以按如下步骤迁移：

```bash
# 1. 移动到较短的路径
cd /home/user/
mv /path/to/old/location/blue-rdma-driver ./

# 2. 重新编译
cd blue-rdma-driver/dtld-ibverbs/rdma-core-55.0
rm -rf build  # 清除旧的构建文件
./build.sh    # 重新构建
```

## 验证

成功构建后，你应该看到类似的输出：

```log
[100%] Built target ibacm
[100%] Built target man
```

而不是在 81% 处失败。

## 相关信息

- Unix domain socket 路径限制：`sizeof(sockaddr_un.sun_path)` = 108 字节
- 相关源文件：`rdma-core-55.0/ibacm/src/acm.c:628`
- 宏定义位置：`rdma-core-55.0/build/include/ccan/build_assert.h:23`
