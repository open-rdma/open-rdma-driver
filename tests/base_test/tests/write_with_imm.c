#include "../lib/rdma_common.h"
#include "../lib/rdma_transport.h"
#include "../lib/rdma_debug.h"
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define DEFAULT_PORT 12348
#define MIN_BUFFER_SIZE 4096

// RDMA WRITE with Immediate test
// Server posts recv to receive immediate data
// Client performs RDMA WRITE with immediate value

int run_server(int msg_len) {
    struct rdma_context ctx;
    struct rdma_config config;
    struct tcp_transport transport;
    struct qp_info local_info, remote_info;

    printf("========== RDMA WRITE with IMM Server ==========\n");
    printf("Message length: %d bytes\n", msg_len);

    // Setup RDMA context (ensure minimum buffer size)
    rdma_default_config(&config);
    config.dev_index = 0;
    config.buffer_size = (msg_len < MIN_BUFFER_SIZE) ? MIN_BUFFER_SIZE : msg_len;

    if (rdma_init_context(&ctx, &config) < 0) {
        return -1;
    }

    // Setup TCP server
    if (tcp_server_init(&transport, DEFAULT_PORT) < 0) {
        rdma_destroy_context(&ctx);
        return -1;
    }

    printf("[SERVER] Waiting for client connection...\n");
    if (tcp_server_accept(&transport) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Exchange QP information
    local_info.qp_num = ctx.qp->qp_num;
    local_info.rkey = ctx.mr->rkey;
    local_info.remote_addr = (uint64_t)ctx.buffer;

    if (rdma_exchange_qp_info(transport.client_fd, &local_info, &remote_info) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    printf("[SERVER] Remote QP: qp_num=%u, rkey=0x%x, addr=0x%lx\n",
           remote_info.qp_num, remote_info.rkey, remote_info.remote_addr);

    // Connect QP
    u_int32_t dest_gid_ipv4 = 0x1122330B; //client IP
    if (rdma_connect_qp(ctx.qp, remote_info.qp_num, dest_gid_ipv4) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Synchronize
    printf("[SERVER] Synchronizing with client...\n");
    if (rdma_handshake(transport.client_fd) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Clear buffer
    memset(ctx.buffer, 0, ctx.buffer_size);

    // Post receive to get immediate data
    // For RDMA WRITE with IMM, recv length can be 0 (only for immediate data)
    // or > 0 if combined with data write
    struct ibv_recv_wr wr = {0};
    struct ibv_recv_wr *bad_wr;
    struct ibv_sge sge = {
        .addr = (uint64_t)ctx.buffer,
        .length = msg_len,  // Allow data to be written
        .lkey = ctx.mr->lkey
    };
    wr.sg_list = &sge;
    wr.num_sge = 1;

    printf("[SERVER] Posting receive for WRITE_WITH_IMM (recv_len=%d)...\n", msg_len);
    if (ibv_post_recv(ctx.qp, &wr, &bad_wr) != 0) {
        fprintf(stderr, "[ERROR] ibv_post_recv failed\n");
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Notify client that recv is posted
    if (rdma_handshake(transport.client_fd) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Wait for completion
    printf("[SERVER] Waiting for RDMA WRITE with IMM...\n");
    struct ibv_wc wc = {0};
    int poll_count = 0;
    while (ibv_poll_cq(ctx.recv_cq, 1, &wc) < 1) {
        usleep(1000);
        poll_count++;
        if (poll_count % 1000 == 0) {
            printf("[SERVER] Still waiting... (poll_count=%d)\n", poll_count);
        }
    }

    printf("[SERVER] Receive completed: status=%d, byte_len=%u\n",
           wc.status, wc.byte_len);

    if (wc.status != IBV_WC_SUCCESS) {
        fprintf(stderr, ANSI_COLOR_RED "[ERROR] Work completion failed: status=%d\n" ANSI_COLOR_RESET,
                wc.status);
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Check for immediate data
    if (wc.wc_flags & IBV_WC_WITH_IMM) {
        printf(ANSI_COLOR_GREEN "[SERVER] Received immediate data: 0x%08x (%u)\n" ANSI_COLOR_RESET,
               wc.imm_data, wc.imm_data);
    } else {
        fprintf(stderr, ANSI_COLOR_RED "[ERROR] No immediate data received!\n" ANSI_COLOR_RESET);
    }

    // Validate written data (if msg_len > 0)
    if (msg_len > 0) {
        size_t error_count = 0;
        struct rdma_pattern pattern = RDMA_PATTERN_CHAR('W');
        rdma_verify_data(ctx.buffer, msg_len, &pattern, &error_count);
        int cnt_valid = msg_len - error_count;

        printf("[SERVER] Data verification: %d/%d bytes correct", cnt_valid, msg_len);
        if (error_count > 0) {
            printf(ANSI_COLOR_RED " (%zu errors)" ANSI_COLOR_RESET "\n", error_count);
            printf(ANSI_COLOR_RED "[SERVER] Data verification FAILED!\n" ANSI_COLOR_RESET);
        } else {
            printf(ANSI_COLOR_GREEN " (PASS)" ANSI_COLOR_RESET "\n");
            printf(ANSI_COLOR_GREEN "[SERVER] Data verification PASSED!\n" ANSI_COLOR_RESET);
        }
    }

    // Final sync
    rdma_handshake(transport.client_fd);

    tcp_transport_close(&transport);
    rdma_destroy_context(&ctx);

    return 0;
}

int run_client(const char *server_ip, int msg_len) {
    struct rdma_context ctx;
    struct rdma_config config;
    struct tcp_transport transport;
    struct qp_info local_info, remote_info;

    printf("========== RDMA WRITE with IMM Client ==========\n");
    printf("Server: %s, Message length: %d bytes\n", server_ip, msg_len);

    // Setup RDMA context
    rdma_default_config(&config);
    config.dev_index = 1;  // Use different device for client
    config.buffer_size = (msg_len < MIN_BUFFER_SIZE) ? MIN_BUFFER_SIZE : msg_len;

    if (rdma_init_context(&ctx, &config) < 0) {
        return -1;
    }

    // Connect to server
    printf("[CLIENT] Connecting to %s:%d...\n", server_ip, DEFAULT_PORT);
    if (tcp_client_connect(&transport, server_ip, DEFAULT_PORT, 30) < 0) {
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Exchange QP information
    local_info.qp_num = ctx.qp->qp_num;
    local_info.rkey = ctx.mr->rkey;
    local_info.remote_addr = (uint64_t)ctx.buffer;

    if (rdma_exchange_qp_info(transport.sock_fd, &local_info, &remote_info) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    printf("[CLIENT] Remote QP: qp_num=%u, rkey=0x%x, addr=0x%lx\n",
           remote_info.qp_num, remote_info.rkey, remote_info.remote_addr);

    // Connect QP
    uint32_t dest_gid_ipv4 = 0x1122330A; //server IP
    if (rdma_connect_qp(ctx.qp, remote_info.qp_num, dest_gid_ipv4) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Synchronize
    printf("[CLIENT] Synchronizing with server...\n");
    if (rdma_handshake(transport.sock_fd) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Wait for server to post receive
    if (rdma_handshake(transport.sock_fd) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Fill buffer with pattern 'W'
    if (msg_len > 0) {
        printf("[CLIENT] Filling buffer with 'W' pattern...\n");
        memset(ctx.buffer, 'W', msg_len);
    }

    // Prepare RDMA WRITE with Immediate
    struct ibv_sge sge = {
        .addr = (uint64_t)ctx.buffer,
        .length = msg_len,
        .lkey = ctx.mr->lkey
    };

    uint32_t immediate_data = 0xDEADBEEF;  // Test immediate value
    struct ibv_send_wr wr = {
        .wr_id = 42,
        .sg_list = &sge,
        .num_sge = 1,
        .opcode = IBV_WR_RDMA_WRITE_WITH_IMM,
        .send_flags = IBV_SEND_SIGNALED,
        .imm_data = immediate_data,
        .wr = {
            .rdma = {
                .remote_addr = remote_info.remote_addr,
                .rkey = remote_info.rkey
            }
        }
    };

    struct ibv_send_wr *bad_wr;

    printf("[CLIENT] Posting RDMA WRITE with IMM (imm_data=0x%08x, len=%d)...\n",
           immediate_data, msg_len);
    if (ibv_post_send(ctx.qp, &wr, &bad_wr) != 0) {
        fprintf(stderr, "[ERROR] ibv_post_send failed\n");
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Wait for completion
    struct ibv_wc wc;
    int poll_count = 0;
    while (ibv_poll_cq(ctx.send_cq, 1, &wc) < 1) {
        usleep(1000);
        poll_count++;
        if (poll_count % 1000 == 0) {
            printf("[CLIENT] Still waiting for completion... (poll_count=%d)\n", poll_count);
        }
    }

    printf("[CLIENT] RDMA WRITE with IMM completed: status=%d, wr_id=%lu\n",
           wc.status, wc.wr_id);

    if (wc.status == IBV_WC_SUCCESS) {
        printf(ANSI_COLOR_GREEN "[CLIENT] RDMA WRITE with IMM SUCCESS!\n" ANSI_COLOR_RESET);
    } else {
        printf(ANSI_COLOR_RED "[CLIENT] RDMA WRITE with IMM FAILED (status=%d)\n" ANSI_COLOR_RESET,
               wc.status);
    }

    // Final sync
    rdma_handshake(transport.sock_fd);

    tcp_transport_close(&transport);
    rdma_destroy_context(&ctx);

    return (wc.status == IBV_WC_SUCCESS) ? 0 : -1;
}

int main(int argc, char *argv[]) {
    // Disable stdout buffering
    setvbuf(stdout, NULL, _IONBF, 0);

    if (argc < 2) {
        fprintf(stderr, "Usage:\n");
        fprintf(stderr, "  Server: %s <msg_len>\n", argv[0]);
        fprintf(stderr, "  Client: %s <msg_len> <server_ip>\n", argv[0]);
        fprintf(stderr, "\nExample:\n");
        fprintf(stderr, "  Server: %s 4096\n", argv[0]);
        fprintf(stderr, "  Client: %s 4096 127.0.0.1\n", argv[0]);
        return EXIT_FAILURE;
    }

    int msg_len = atoi(argv[1]);
    if (msg_len < 0) {
        fprintf(stderr, "Error: msg_len must be >= 0\n");
        return EXIT_FAILURE;
    }

    if (argc == 2) {
        // Server mode
        return run_server(msg_len);
    } else {
        // Client mode
        return run_client(argv[2], msg_len);
    }
}
