# rdma-core Build Path Length Issue

## Problem Description

When building `rdma-core-55.0`, if the absolute path of the project is too long, the build will fail. The failure typically occurs at around 81% of the build, specifically as a `BUILD_ASSERT` error when compiling `ibacm/src/acm.c`.

## Example Error Log

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
[ 80%] Built target ibsendtrap
[ 80%] Building C object infiniband-diags/CMakeFiles/mcm_rereg_test.dir/mcm_rereg_test.c.o
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

## Technical Details

### Root Cause

The `BUILD_ASSERT` macro checks at compile time that a condition is true. In `ibacm/src/acm.c`, the following assertion exists:

```c
BUILD_ASSERT(sizeof(IBACM_IBACME_SERVER_PATH) <= sizeof(addr.sun_path));
```

This assertion ensures that the `IBACM_IBACME_SERVER_PATH` string (the Unix domain socket path) does not exceed the maximum length of `sockaddr_un.sun_path` (typically 108 bytes).

### Effect of a Long Path

The `IBACM_IBACME_SERVER_PATH` macro is expanded to a string containing the full build path, for example:

```
/home/peng/projects/rdma_all/blue-rdma-driver-fork/dtld-ibverbs/rdma-core-55.0/build/var/run/ibacm.sock
```

When the project path is too long, the fully expanded path exceeds the maximum length allowed for Unix domain sockets (108 bytes), causing the `BUILD_ASSERT` to fail and the build to abort.

### The BUILD_ASSERT Mechanism

```c
#define BUILD_ASSERT(cond) \
do { (void) sizeof(char [1 - 2*!(cond)]); } while(0)
```

- If `cond` is true, the array size is `char[1]` — valid
- If `cond` is false, the array size is `char[-1]` — the compiler reports: "size of unnamed array is negative"

## Solutions

### 1. Use a Shorter Project Path (Recommended)

Place the project in a directory with a shorter path, for example:

**Recommended path**:
```bash
/home/user/blue-rdma-driver/
```

**Path to avoid**:
```bash
/home/peng/projects/rdma_all/blue-rdma-driver-fork/
```

### 2. Maximum Safe Path Length

Based on experience, the absolute path of the project root should be kept to **50 characters or fewer** to ensure a successful build.

Calculation:
- Unix socket maximum path: 108 bytes
- rdma-core internal sub-path: ~58 bytes (`/dtld-ibverbs/rdma-core-55.0/build/var/run/ibacm.sock`)
- Safe project root path: 108 − 58 = **50 bytes**

### 3. Check the Current Path Length

Use the following command to check the length of the current project path:

```bash
pwd | wc -c
```

If the output exceeds 50, it is advisable to move the project to a shorter path.

### 4. Steps to Migrate the Project

If the project was already cloned in a long path, migrate it as follows:

```bash
# 1. Move to a shorter path
cd /home/user/
mv /path/to/old/location/blue-rdma-driver ./

# 2. Rebuild
cd blue-rdma-driver/dtld-ibverbs/rdma-core-55.0
rm -rf build  # Remove old build artifacts
./build.sh    # Rebuild
```

## Verification

After a successful build, you should see output similar to:

```log
[100%] Built target ibacm
[100%] Built target man
```

rather than a failure at 81%.

## Related Information

- Unix domain socket path limit: `sizeof(sockaddr_un.sun_path)` = 108 bytes
- Relevant source file: `rdma-core-55.0/ibacm/src/acm.c:628`
- Macro definition location: `rdma-core-55.0/build/include/ccan/build_assert.h:23`
