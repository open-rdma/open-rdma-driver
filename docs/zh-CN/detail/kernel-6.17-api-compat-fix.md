# 内核 6.17 API 兼容性修复

**日期**: 2026-02-24
**问题**: open-rdma-driver 在 Linux 6.17 内核下编译失败
**状态**: ✅ 已解决

---

## 问题一：`reg_user_mr` 函数签名不匹配

### 错误信息

```
main.c:99:24: error: initialization of 'struct ib_mr * (*)(struct ib_pd *, u64, u64, u64, int, struct ib_dmah *, struct ib_udata *)' from incompatible pointer type 'struct ib_mr * (*)(struct ib_pd *, u64, u64, u64, int, struct ib_udata *)' [-Werror=incompatible-pointer-types]
   99 |         .reg_user_mr = bluerdma_reg_user_mr,
```

### 根本原因

内核 6.11 起，`struct ib_device_ops` 中的 `reg_user_mr` 回调函数签名增加了一个 `struct ib_dmah *` 参数，用于支持 DMA 句柄机制，旧签名为：

```c
// 旧签名（< 6.11）
struct ib_mr *(*reg_user_mr)(struct ib_pd *, u64, u64, u64, int,
                              struct ib_udata *);

// 新签名（>= 6.11）
struct ib_mr *(*reg_user_mr)(struct ib_pd *, u64, u64, u64, int,
                              struct ib_dmah *, struct ib_udata *);
```

### 修复方案

在 `verbs.h` 和 `verbs.c` 中用版本宏对函数声明和实现做条件编译：

**修改文件**: `kernel-driver/verbs.h`

```diff
-struct ib_mr *bluerdma_reg_user_mr(struct ib_pd *pd, u64 start, u64 length,
-                                   u64 virt_addr, int access_flags,
-                                   struct ib_udata *udata);
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 11, 0)
+struct ib_mr *bluerdma_reg_user_mr(struct ib_pd *pd, u64 start, u64 length,
+                                   u64 virt_addr, int access_flags, struct ib_dmah *dmah,
+                                   struct ib_udata *udata);
+#else
+struct ib_mr *bluerdma_reg_user_mr(struct ib_pd *pd, u64 start, u64 length,
+                                   u64 virt_addr, int access_flags,
+                                   struct ib_udata *udata);
+#endif
```

**修改文件**: `kernel-driver/verbs.c`

```diff
-struct ib_mr *bluerdma_reg_user_mr(struct ib_pd *pd, u64 start, u64 length,
-                                   u64 virt_addr, int access_flags,
-                                   struct ib_udata *udata)
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 11, 0)
+struct ib_mr *bluerdma_reg_user_mr(struct ib_pd *pd, u64 start, u64 length,
+                                   u64 virt_addr, int access_flags, struct ib_dmah *dmah,
+                                   struct ib_udata *udata)
+#else
+struct ib_mr *bluerdma_reg_user_mr(struct ib_pd *pd, u64 start, u64 length,
+                                   u64 virt_addr, int access_flags,
+                                   struct ib_udata *udata)
+#endif
```

---

## 问题二 & 三：udmabuf 多处内核 API 不兼容

### 错误信息

```
u-dma-buf.c:1178:6: error: 'const struct dma_buf_ops' has no member named 'cache_sgt_mapping'
 1178 |     .cache_sgt_mapping = true,

u-dma-buf.c:3208:22: warning: assignment discards 'const' qualifier from pointer target type [-Wdiscarded-qualifiers]
 3208 |         *bus_type    = &platform_bus_type;
```

### 根本原因

`third_party/udmabuf` 使用的是 **u-dma-buf 5.4.2**，该版本尚未适配内核 6.11+ 的以下两处 API 变更：

1. **`dma_buf_ops.cache_sgt_mapping` 字段被移除**：该字段在内核 5.3 引入，约在 6.16 被删除。原代码只判断下界（`>= 5.3`），在 6.17 上因字段不存在而编译失败。

2. **`platform_bus_type` 变为 `const`**：内核 6.11+ 中 `platform_bus_type` 被声明为 `const struct bus_type`，而原代码中 `udmabuf_static_parse_bind` 使用非 const 指针接收，赋值时丢失 const 限定符，`-Werror` 下报错。

### 修复方案

升级 `third_party/udmabuf` 到上游最新版本（新版本已对上述内核 API 变更做了适配），替换整个子目录内容。

```bash
cd third_party/udmabuf
git pull origin master   # 或从上游 https://github.com/ikwzm/udmabuf 更新
```

> 若以 git submodule 管理，执行：
> ```bash
> git submodule update --remote third_party/udmabuf
> ```

---

## 内核 API 变更版本对照表

| API 变更 | 引入版本 | 移除/变更版本 | 涉及组件 |
|---------|---------|------------|---------|
| `reg_user_mr` 增加 `struct ib_dmah *` 参数 | 6.11 | — | bluerdma 驱动 |
| `dma_buf_ops.cache_sgt_mapping` 字段移除 | 5.3（引入）| ~6.11（移除）| u-dma-buf |
| `platform_bus_type` 变为 `const` | — | ~6.16 | u-dma-buf |

---


---

**文档版本**: 1.0
**最后更新**: 2026-02-24
**维护者**: Claude Code Assistant
