#include "../lib/rdma_common.h"
#include "../lib/rdma_transport.h"
#include "../lib/rdma_debug.h"
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define DEFAULT_PORT 12346

int run_server(int msg_len) {
    struct rdma_context ctx;
    struct rdma_config config;
    struct tcp_transport transport;
    struct qp_info local_info, remote_info;

    printf("========== SEND/RECV Server ==========\n");

    // Setup RDMA context
    rdma_default_config(&config);
    config.dev_index = 0;
    config.buffer_size = msg_len;

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

    // Connect QP
    uint32_t dest_gid_ipv4 = 0x1122330B; //client IP
    if (rdma_connect_qp(ctx.qp, remote_info.qp_num, dest_gid_ipv4) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Synchronize with client
    printf("[SERVER] Synchronizing with client...\n");
    if (rdma_handshake(transport.client_fd) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Clear buffer and post receive
    memset(ctx.buffer, 0, msg_len);

    struct ibv_recv_wr wr = {0};
    struct ibv_recv_wr *bad_wr;
    struct ibv_sge sge = {
        .addr = (uint64_t)ctx.buffer,
        .length = msg_len,
        .lkey = ctx.mr->lkey
    };
    wr.sg_list = &sge;
    wr.num_sge = 1;

    printf("[SERVER] Posting receive...\n");
    if (ibv_post_recv(ctx.qp, &wr, &bad_wr) != 0) {
        fprintf(stderr, "[ERROR] ibv_post_recv failed\n");
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Notify client that receive is posted
    if (rdma_handshake(transport.client_fd) < 0) {
        tcp_transport_close(&transport);
        rdma_destroy_context(&ctx);
        return -1;
    }

    // Wait for completion
    printf("[SERVER] Waiting for data...\n");
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

    // Validate received data with full byte-by-byte comparison
    size_t error_count = 0;
    struct rdma_pattern pattern = RDMA_PATTERN_CHAR('c');
    rdma_verify_data(ctx.buffer, msg_len, &pattern, &error_count);
    int cnt_valid = msg_len - error_count;

    printf("[SERVER] Data verification: %d/%d bytes correct", cnt_valid, msg_len);
    if (error_count > 0) {
        printf(ANSI_COLOR_RED " (%zu errors)" ANSI_COLOR_RESET "\n", error_count);
        printf(ANSI_COLOR_RED "[SERVER] Test FAILED!\n" ANSI_COLOR_RESET);
    } else {
        printf(ANSI_COLOR_GREEN " (PASS)" ANSI_COLOR_RESET "\n");
        printf(ANSI_COLOR_GREEN "[SERVER] Test PASSED!\n" ANSI_COLOR_RESET);
    }

    // Final sync
    rdma_handshake(transport.client_fd);

    tcp_transport_close(&transport);
    rdma_destroy_context(&ctx);

    return (cnt_valid == msg_len) ? 0 : -1;
}

int run_client(int msg_len, const char *server_ip) {
    struct rdma_context ctx;
    struct rdma_config config;
    struct tcp_transport transport;
    struct qp_info local_info, remote_info;

    printf("========== SEND/RECV Client ==========\n");

    // Setup RDMA context
    rdma_default_config(&config);
    config.dev_index = 1;  // Use different device for client
    config.buffer_size = msg_len;

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

    // Synchronize with server
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

    // Fill buffer with test pattern
    printf("[CLIENT] Filling buffer with 'c' pattern...\n");
    memset(ctx.buffer, 'c', msg_len);

    // Prepare send operation
    struct ibv_sge sge = {
        .addr = (uint64_t)ctx.buffer,
        .length = msg_len,
        .lkey = ctx.mr->lkey
    };

    struct ibv_send_wr wr = {
        .wr_id = 7,
        .sg_list = &sge,
        .num_sge = 1,
        .opcode = IBV_WR_SEND,
        .send_flags = IBV_SEND_SIGNALED
    };

    struct ibv_send_wr *bad_wr;

    printf("[CLIENT] Posting send...\n");
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

    printf("[CLIENT] Send completed: status=%d, wr_id=%lu\n", wc.status, wc.wr_id);

    if (wc.status == IBV_WC_SUCCESS) {
        printf(ANSI_COLOR_GREEN "[CLIENT] Send SUCCESS!\n" ANSI_COLOR_RESET);
    } else {
        printf(ANSI_COLOR_RED "[CLIENT] Send FAILED (status=%d)\n" ANSI_COLOR_RESET, wc.status);
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
        return EXIT_FAILURE;
    }

    int msg_len = atoi(argv[1]);
    if (msg_len <= 0) {
        fprintf(stderr, "Error: msg_len must be positive\n");
        return EXIT_FAILURE;
    }

    if (argc == 2) {
        return run_server(msg_len);
    } else {
        return run_client(msg_len, argv[2]);
    }
}
