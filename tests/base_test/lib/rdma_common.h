#ifndef RDMA_COMMON_H
#define RDMA_COMMON_H

#include <infiniband/verbs.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

// RDMA context structure
struct rdma_context {
    struct ibv_context *ctx;
    struct ibv_pd *pd;
    struct ibv_cq *send_cq;
    struct ibv_cq *recv_cq;
    struct ibv_qp *qp;
    struct ibv_mr *mr;
    char *buffer;
    size_t buffer_size;
    int dev_index;
};

// Configuration for RDMA context creation
struct rdma_config {
    int dev_index;              // Device index to use
    size_t buffer_size;         // Buffer size in bytes
    int max_send_wr;           // Maximum send work requests
    int max_recv_wr;           // Maximum receive work requests
    int max_send_sge;          // Maximum send SGEs
    int max_recv_sge;          // Maximum receive SGEs
    int cq_size;               // Completion queue size
    bool use_hugetlb;          // Try to use huge pages
    bool use_fixed_addr;       // Use fixed address for RTL simulator
    uint64_t fixed_addr;       // Fixed address (if use_fixed_addr is true)
};

// Default configuration
static inline void rdma_default_config(struct rdma_config *config) {
    config->dev_index = 0;
    config->buffer_size = 128 * 1024;
    config->max_send_wr = 128;
    config->max_recv_wr = 128;
    config->max_send_sge = 1;
    config->max_recv_sge = 1;
    config->cq_size = 512;
    config->use_hugetlb = true;
    config->use_fixed_addr = false;
    config->fixed_addr = 0;
}

// Error handling
void rdma_die(const char *reason);

// Device and context management
int rdma_init_context(struct rdma_context *ctx, const struct rdma_config *config);
void rdma_destroy_context(struct rdma_context *ctx);

// QP state transitions
int rdma_qp_to_init(struct ibv_qp *qp);
int rdma_qp_to_rtr(struct ibv_qp *qp, uint32_t dest_qp_num, uint32_t dest_gid_ipv4);
int rdma_qp_to_rts(struct ibv_qp *qp);

// Helper: Combined QP state transition to RTS
int rdma_connect_qp(struct ibv_qp *qp, uint32_t dest_qp_num, uint32_t dest_gid_ipv4);

// Buffer operations
int rdma_alloc_buffer(char **buffer, size_t size, bool use_hugetlb);
void rdma_free_buffer(char *buffer, size_t size);
int rdma_clear_buffer(char *buffer, size_t size);

// Logging control
extern bool rdma_debug_enabled;
void rdma_set_debug(bool enabled);
void rdma_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

#endif // RDMA_COMMON_H
