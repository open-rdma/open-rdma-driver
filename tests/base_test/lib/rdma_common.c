#include "rdma_common.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <stdarg.h>
#include <errno.h>

bool rdma_debug_enabled = true;

void rdma_set_debug(bool enabled) {
    rdma_debug_enabled = enabled;
}

void rdma_log(const char *fmt, ...) {
    if (!rdma_debug_enabled) return;

    va_list args;
    va_start(args, fmt);
    vprintf(fmt, args);
    va_end(args);
}

void rdma_die(const char *reason) {
    perror(reason);
    exit(EXIT_FAILURE);
}

int rdma_alloc_buffer(char **buffer, size_t size, bool use_hugetlb) {
    rdma_log("[RDMA] Allocating buffer (size=%zu, use_hugetlb=%d)\n", size, use_hugetlb);

    int flags = MAP_SHARED | MAP_ANONYMOUS | MAP_POPULATE;
    if (use_hugetlb) {
        flags |= MAP_HUGETLB;
    }

    *buffer = mmap(NULL, size, PROT_READ | PROT_WRITE, flags, -1, 0);

    if (*buffer == MAP_FAILED) {
        if (use_hugetlb) {
            rdma_log("[RDMA] mmap with MAP_HUGETLB failed, retrying without it\n");
            flags &= ~MAP_HUGETLB;
            *buffer = mmap(NULL, size, PROT_READ | PROT_WRITE, flags, -1, 0);
        }

        if (*buffer == MAP_FAILED) {
            fprintf(stderr, "[ERROR] Failed to allocate buffer: %s\n", strerror(errno));
            return -1;
        }
    }

    rdma_log("[RDMA] Buffer allocated at %p\n", *buffer);
    return 0;
}

void rdma_free_buffer(char *buffer, size_t size) {
    if (buffer && buffer != MAP_FAILED) {
        munmap(buffer, size);
    }
}

int rdma_clear_buffer(char *buffer, size_t size) {
    if (!buffer) {
        fprintf(stderr, "[ERROR] Invalid buffer pointer\n");
        return -1;
    }
    memset(buffer, 0, size);
    return 0;
}

int rdma_init_context(struct rdma_context *ctx, const struct rdma_config *config) {
    if (!ctx || !config) {
        fprintf(stderr, "[ERROR] Invalid context or config\n");
        return -1;
    }

    memset(ctx, 0, sizeof(*ctx));
    ctx->dev_index = config->dev_index;
    ctx->buffer_size = config->buffer_size;

    rdma_log("[RDMA] Initializing RDMA context (dev_index=%d)\n", ctx->dev_index);

    // Get device list
    int num_devices;
    struct ibv_device **dev_list = ibv_get_device_list(&num_devices);
    if (!dev_list) {
        fprintf(stderr, "[ERROR] Failed to get IB devices list\n");
        return -1;
    }

    rdma_log("[RDMA] Found %d RDMA devices\n", num_devices);
    for (int i = 0; i < num_devices && dev_list[i]; i++) {
        rdma_log("[RDMA]   Device[%d]: %s\n", i, ibv_get_device_name(dev_list[i]));
    }

    // Check device index
    if (ctx->dev_index >= num_devices || !dev_list[ctx->dev_index]) {
        fprintf(stderr, "[ERROR] Device index %d not available (found %d devices)\n",
                ctx->dev_index, num_devices);
        ibv_free_device_list(dev_list);
        return -1;
    }

    // Open device
    rdma_log("[RDMA] Opening device: %s\n", ibv_get_device_name(dev_list[ctx->dev_index]));
    ctx->ctx = ibv_open_device(dev_list[ctx->dev_index]);
    if (!ctx->ctx) {
        fprintf(stderr, "[ERROR] Failed to open IB device\n");
        ibv_free_device_list(dev_list);
        return -1;
    }

    // Allocate PD
    rdma_log("[RDMA] Allocating PD\n");
    ctx->pd = ibv_alloc_pd(ctx->ctx);
    if (!ctx->pd) {
        fprintf(stderr, "[ERROR] Failed to allocate PD\n");
        goto err_close_device;
    }

    // Allocate buffer
    if (rdma_alloc_buffer(&ctx->buffer, ctx->buffer_size, config->use_hugetlb) < 0) {
        goto err_dealloc_pd;
    }

    // Handle fixed address for RTL simulator
    if (config->use_fixed_addr) {
        rdma_log("[RDMA] Using fixed address 0x%lx for RTL simulator\n", config->fixed_addr);
        ctx->buffer = (char *)config->fixed_addr;
    }

    // Register MR
    rdma_log("[RDMA] Registering MR (addr=%p, size=%zu)\n", ctx->buffer, ctx->buffer_size);
    ctx->mr = ibv_reg_mr(ctx->pd, ctx->buffer, ctx->buffer_size,
                         IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_READ);
    if (!ctx->mr) {
        fprintf(stderr, "[ERROR] Failed to register MR\n");
        goto err_free_buffer;
    }
    rdma_log("[RDMA] MR registered - lkey=0x%x, rkey=0x%x\n", ctx->mr->lkey, ctx->mr->rkey);

    // Create CQs
    rdma_log("[RDMA] Creating CQs (size=%d)\n", config->cq_size);
    ctx->send_cq = ibv_create_cq(ctx->ctx, config->cq_size, NULL, NULL, 0);
    ctx->recv_cq = ibv_create_cq(ctx->ctx, config->cq_size, NULL, NULL, 0);
    if (!ctx->send_cq || !ctx->recv_cq) {
        fprintf(stderr, "[ERROR] Failed to create CQs\n");
        goto err_dereg_mr;
    }

    // Create QP
    rdma_log("[RDMA] Creating QP\n");
    struct ibv_qp_init_attr qp_init_attr = {
        .send_cq = ctx->send_cq,
        .recv_cq = ctx->recv_cq,
        .cap = {
            .max_send_wr = config->max_send_wr,
            .max_recv_wr = config->max_recv_wr,
            .max_send_sge = config->max_send_sge,
            .max_recv_sge = config->max_recv_sge
        },
        .qp_type = IBV_QPT_RC
    };

    ctx->qp = ibv_create_qp(ctx->pd, &qp_init_attr);
    if (!ctx->qp) {
        fprintf(stderr, "[ERROR] Failed to create QP\n");
        goto err_destroy_cq;
    }
    rdma_log("[RDMA] QP created - qp_num=%u\n", ctx->qp->qp_num);

    ibv_free_device_list(dev_list);
    rdma_log("[RDMA] Context initialization completed\n");
    return 0;

err_destroy_cq:
    if (ctx->send_cq) ibv_destroy_cq(ctx->send_cq);
    if (ctx->recv_cq) ibv_destroy_cq(ctx->recv_cq);
err_dereg_mr:
    if (ctx->mr) ibv_dereg_mr(ctx->mr);
err_free_buffer:
    if (!config->use_fixed_addr) {
        rdma_free_buffer(ctx->buffer, ctx->buffer_size);
    }
err_dealloc_pd:
    ibv_dealloc_pd(ctx->pd);
err_close_device:
    ibv_close_device(ctx->ctx);
    ibv_free_device_list(dev_list);
    return -1;
}

void rdma_destroy_context(struct rdma_context *ctx) {
    if (!ctx) return;

    rdma_log("[RDMA] Destroying context\n");

    if (ctx->qp) ibv_destroy_qp(ctx->qp);
    if (ctx->send_cq) ibv_destroy_cq(ctx->send_cq);
    if (ctx->recv_cq) ibv_destroy_cq(ctx->recv_cq);
    if (ctx->mr) ibv_dereg_mr(ctx->mr);
    if (ctx->buffer) rdma_free_buffer(ctx->buffer, ctx->buffer_size);
    if (ctx->pd) ibv_dealloc_pd(ctx->pd);
    if (ctx->ctx) ibv_close_device(ctx->ctx);

    memset(ctx, 0, sizeof(*ctx));
}

int rdma_qp_to_init(struct ibv_qp *qp) {
    rdma_log("[RDMA] Transitioning QP to INIT state\n");

    struct ibv_qp_attr attr = {
        .qp_state = IBV_QPS_INIT,
        .pkey_index = 0,
        .port_num = 1,
        .qp_access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_READ
    };

    if (ibv_modify_qp(qp, &attr, IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS)) {
        fprintf(stderr, "[ERROR] Failed to transition QP to INIT\n");
        return -1;
    }

    return 0;
}

int rdma_qp_to_rtr(struct ibv_qp *qp, uint32_t dest_qp_num, uint32_t dest_gid_ipv4) {
    rdma_log("[RDMA] Transitioning QP to RTR state (dest_qp_num=%u)\n", dest_qp_num);

    struct ibv_qp_attr attr = {
        .qp_state = IBV_QPS_RTR,
        .path_mtu = IBV_MTU_4096,
        .dest_qp_num = dest_qp_num,
        .rq_psn = 0,
        .max_dest_rd_atomic = 1,
        .min_rnr_timer = 12,
        .ah_attr = {
            .is_global = 0,
            .dlid = 0,
            .sl = 0,
            .src_path_bits = 0,
            .port_num = 1
        }
    };

    rdma_log("[RDMA] Setting dest GID with IPv4 address 0x%08x\n",
             dest_gid_ipv4);
    attr.ah_attr.grh.dgid.raw[10] = 0xFF;
    attr.ah_attr.grh.dgid.raw[11] = 0xFF;
    attr.ah_attr.grh.dgid.raw[12] = (dest_gid_ipv4 >> 24) & 0xFF;
    attr.ah_attr.grh.dgid.raw[13] = (dest_gid_ipv4 >> 16) & 0xFF;
    attr.ah_attr.grh.dgid.raw[14] = (dest_gid_ipv4 >> 8) & 0xFF;
    attr.ah_attr.grh.dgid.raw[15] = dest_gid_ipv4 & 0xFF;

    if (ibv_modify_qp(qp, &attr,
                      IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
                      IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER)) {
        fprintf(stderr, "[ERROR] Failed to transition QP to RTR\n");
        return -1;
    }

    return 0;
}

int rdma_qp_to_rts(struct ibv_qp *qp) {
    rdma_log("[RDMA] Transitioning QP to RTS state\n");

    struct ibv_qp_attr attr = {
        .qp_state = IBV_QPS_RTS,
        .timeout = 14,
        .retry_cnt = 7,
        .rnr_retry = 7,
        .sq_psn = 0,
        .max_rd_atomic = 1
    };



    if (ibv_modify_qp(qp, &attr,
                      IBV_QP_STATE | IBV_QP_AV | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT |
                      IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC)) {
        fprintf(stderr, "[ERROR] Failed to transition QP to RTS\n");
        return -1;
    }

    return 0;
}

int rdma_connect_qp(struct ibv_qp *qp, uint32_t dest_qp_num, uint32_t dest_gid_ipv4) {
    if (rdma_qp_to_init(qp) < 0) return -1;
    if (rdma_qp_to_rtr(qp, dest_qp_num, dest_gid_ipv4) < 0) return -1;
    if (rdma_qp_to_rts(qp) < 0) return -1;

    rdma_log("[RDMA] QP connected successfully\n");
    return 0;
}
