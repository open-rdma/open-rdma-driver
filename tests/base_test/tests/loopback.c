#include "../lib/rdma_common.h"
#include "../lib/rdma_debug.h"
#include <stdio.h>
#include <string.h>
#include <unistd.h>


// Loopback test: Two QPs on the same device communicate with each other
int run_loopback_test(int msg_len, int num_rounds) {
    struct rdma_context ctx;
    struct rdma_config config;
    struct ibv_qp *qp0, *qp1;
    char *src_buffer, *dst_buffer;
    int failed_rounds = 0;

    // Configure RDMA context
    rdma_default_config(&config);
    config.dev_index = 0;
    config.buffer_size = msg_len * 2;  // Space for both source and destination

    printf("========== Loopback Test ==========\n");
    printf("Message length: %d bytes\n", msg_len);
    printf("Number of rounds: %d\n", num_rounds);
    printf("===================================\n\n");

    // Initialize RDMA context
    if (rdma_init_context(&ctx, &config) < 0) {
        return -1;
    }

    // Setup buffers
    src_buffer = ctx.buffer;
    dst_buffer = ctx.buffer + msg_len;

    // Create second QP for loopback
    printf("[LOOPBACK] Creating second QP\n");
    struct ibv_qp_init_attr qp_init_attr = {
        .send_cq = ctx.send_cq,
        .recv_cq = ctx.recv_cq,
        .cap = {
            .max_send_wr = 100,
            .max_recv_wr = 100,
            .max_send_sge = 1,
            .max_recv_sge = 1
        },
        .qp_type = IBV_QPT_RC
    };

    qp0 = ctx.qp;
    qp1 = ibv_create_qp(ctx.pd, &qp_init_attr);
    if (!qp1) {
        fprintf(stderr, "[ERROR] Failed to create second QP\n");
        rdma_destroy_context(&ctx);
        return -1;
    }

    printf("[LOOPBACK] QP0: qp_num=%u, QP1: qp_num=%u\n", qp0->qp_num, qp1->qp_num);

    // Connect QPs to each other
    printf("[LOOPBACK] Connecting QP0 -> QP1\n");
    uint32_t dest_gid_ipv4 = 0x1122330A;
    if (rdma_connect_qp(qp0, qp1->qp_num, dest_gid_ipv4) < 0) {
        ibv_destroy_qp(qp1);
        rdma_destroy_context(&ctx);
        return -1;
    }

    printf("[LOOPBACK] Connecting QP1 -> QP0\n");
    if (rdma_connect_qp(qp1, qp0->qp_num, dest_gid_ipv4) < 0) {
        ibv_destroy_qp(qp1);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Fill source buffer with test pattern
    printf("[LOOPBACK] Filling source buffer with test pattern\n");
    for (int i = 0; i < msg_len; i++) {
        src_buffer[i] = (i & 0xFF);
    }

    // Run test rounds
    printf("\n[LOOPBACK] Starting %d test rounds...\n", num_rounds);

    for (int round = 1; round <= num_rounds; round++) {
        printf("\n--- Round %d/%d ---\n", round, num_rounds);

        // Clear destination buffer
        memset(dst_buffer, 0, msg_len);
        COMPILER_BARRIER();

        // Prepare RDMA WRITE operation
        struct ibv_sge sge = {
            .addr = (uint64_t)src_buffer,
            .length = msg_len,
            .lkey = ctx.mr->lkey
        };

        struct ibv_send_wr wr = {
            .wr_id = round,
            .sg_list = &sge,
            .num_sge = 1,
            .opcode = IBV_WR_RDMA_WRITE,
            .send_flags = IBV_SEND_SIGNALED,
            .wr = {
                .rdma = {
                    .remote_addr = (uint64_t)dst_buffer,
                    .rkey = ctx.mr->lkey
                }
            }
        };

        struct ibv_send_wr *bad_wr;

        // Post send
        printf("[LOOPBACK] Posting RDMA WRITE...\n");
        if (ibv_post_send(qp0, &wr, &bad_wr) != 0) {
            fprintf(stderr, "[ERROR] ibv_post_send failed\n");
            failed_rounds++;
            continue;
        }

        // Wait for completion
        struct ibv_wc wc = {0};
        while (ibv_poll_cq(ctx.send_cq, 1, &wc) == 0) {
            usleep(1000);
            COMPILER_BARRIER();
        }

        COMPILER_BARRIER();

        if (wc.status != IBV_WC_SUCCESS) {
            fprintf(stderr, "[ERROR] Work completion failed: status=%d\n", wc.status);
            failed_rounds++;
            continue;
        }

        printf("[LOOPBACK] RDMA WRITE completed (wr_id=%lu, status=%d)\n",
               wc.wr_id, wc.status);

        // Verify data using unified API
        size_t error_count = 0;
        struct rdma_pattern pattern = RDMA_PATTERN_SEQ();
        rdma_verify_data(dst_buffer, msg_len, &pattern, &error_count);
        int valid_count = msg_len - error_count;

        printf("[LOOPBACK] Data verification: %d/%d bytes correct",
               valid_count, msg_len);

        if (error_count > 0) {
            printf(ANSI_COLOR_RED " (%zu errors)" ANSI_COLOR_RESET "\n", error_count);
            failed_rounds++;
        } else {
            printf(ANSI_COLOR_GREEN " (PASS)" ANSI_COLOR_RESET "\n");
        }
    }

    // Print summary
    printf("\n========== Test Summary ==========\n");
    printf("Total rounds: %d\n", num_rounds);
    printf("Passed: " ANSI_COLOR_GREEN "%d" ANSI_COLOR_RESET "\n", num_rounds - failed_rounds);
    printf("Failed: " ANSI_COLOR_RED "%d" ANSI_COLOR_RESET "\n", failed_rounds);
    printf("==================================\n");

    // Cleanup
    ibv_destroy_qp(qp1);
    rdma_destroy_context(&ctx);

    return (failed_rounds == 0) ? 0 : -1;
}

int main(int argc, char *argv[]) {
    // Disable stdout buffering for immediate log output
    setvbuf(stdout, NULL, _IONBF, 0);

    if (argc < 2) {
        fprintf(stderr, "Usage: %s <msg_len> [num_rounds]\n", argv[0]);
        fprintf(stderr, "  msg_len: Message length in bytes\n");
        fprintf(stderr, "  num_rounds: Number of test rounds (default: 2)\n");
        return EXIT_FAILURE;
    }

    int msg_len = atoi(argv[1]);
    int num_rounds = (argc >= 3) ? atoi(argv[2]) : 2;

    if (msg_len <= 0) {
        fprintf(stderr, "Error: msg_len must be positive\n");
        return EXIT_FAILURE;
    }

    return run_loopback_test(msg_len, num_rounds);
}
