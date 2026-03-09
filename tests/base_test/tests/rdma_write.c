#include "../lib/rdma_common.h"
#include "../lib/rdma_debug.h"
#include "../lib/rdma_transport.h"
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define DEFAULT_PORT 12345
#define BUF_SIZE (256 * 1024)
#define MAX_ROUNDS 5

// RDMA WRITE test with multiple rounds
// Tests repeated RDMA WRITE operations and data verification

int run_server(int msg_len, int dev_index, int num_rounds) {
  struct rdma_context ctx;
  struct rdma_config config;
  struct tcp_transport transport;
  struct qp_info local_info, remote_info;
  char *dst_buffer;

  printf("========== RDMA WRITE Server ==========\n");
  printf("Device index: %d, Message length: %d, Rounds: %d\n", dev_index,
         msg_len, num_rounds);

  // Setup RDMA context with double buffer (src + dst)
  rdma_default_config(&config);
  config.dev_index = dev_index;
  config.buffer_size = BUF_SIZE * 2;

  // Support RTL simulator fixed address
#ifdef COMPILE_FOR_RTL_SIMULATOR_TEST
  config.use_fixed_addr = true;
  config.fixed_addr = 0x7f7e8e600000;
#endif

  if (rdma_init_context(&ctx, &config) < 0) {
    return -1;
  }

  // dst_buffer is in the second half
  dst_buffer = ctx.buffer + BUF_SIZE;

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
  local_info.remote_addr = (uint64_t)dst_buffer;

  if (rdma_exchange_qp_info(transport.client_fd, &local_info, &remote_info) <
      0) {
    tcp_transport_close(&transport);
    rdma_destroy_context(&ctx);
    return -1;
  }

  // Connect QP
  uint32_t dest_gid_ipv4 = 0x1122330B; // client IP
  if (rdma_connect_qp(ctx.qp, remote_info.qp_num, dest_gid_ipv4) < 0) {
    tcp_transport_close(&transport);
    rdma_destroy_context(&ctx);
    return -1;
  }

  printf("[SERVER] QP connected, ready for RDMA WRITE operations\n");

  // Main test loop
  int failed_rounds = 0;
  for (int round = 1; round <= num_rounds; round++) {
    printf("\n--- Round %d/%d ---\n", round, num_rounds);

    // Wait for client to complete RDMA WRITE
    if (rdma_sync_server_to_client(transport.client_fd) < 0) {
      fprintf(stderr, "[ERROR] Sync failed\n");
      break;
    }

    COMPILER_BARRIER();

    // Check received data
    size_t error_count = 0;
    struct rdma_pattern pattern = RDMA_PATTERN_SEQ();
    rdma_verify_data(dst_buffer, msg_len, &pattern, &error_count);
    int valid_count = msg_len - error_count;

    printf("[SERVER] Round %d: received %d/%d bytes correctly", round,
           valid_count, msg_len);

    if (error_count > 0) {
      printf(ANSI_COLOR_RED " (%zu errors)" ANSI_COLOR_RESET "\n", error_count);
      failed_rounds++;
    } else {
      printf(ANSI_COLOR_GREEN " (perfect!)" ANSI_COLOR_RESET "\n");
    }

    COMPILER_BARRIER();

    // Clear buffer for next round
    memset(dst_buffer, 0, msg_len);
    COMPILER_BARRIER();
  }

  printf("\n========== Server Test Summary ==========\n");
  printf("Total rounds: %d\n", num_rounds);
  printf("Passed: " ANSI_COLOR_GREEN "%d" ANSI_COLOR_RESET "\n",
         num_rounds - failed_rounds);
  printf("Failed: " ANSI_COLOR_RED "%d" ANSI_COLOR_RESET "\n", failed_rounds);
  printf("========================================\n");

  tcp_transport_close(&transport);
  rdma_destroy_context(&ctx);

  return (failed_rounds == 0) ? 0 : -1;
}

int run_client(int msg_len, const char *server_ip, int dev_index,
               int num_rounds) {
  struct rdma_context ctx;
  struct rdma_config config;
  struct tcp_transport transport;
  struct qp_info local_info, remote_info;
  char *src_buffer;

  printf("========== RDMA WRITE Client ==========\n");
  printf("Server: %s, Device: %d, Message: %d bytes, Rounds: %d\n", server_ip,
         dev_index, msg_len, num_rounds);

  // Setup RDMA context
  rdma_default_config(&config);
  config.dev_index = dev_index;
  config.buffer_size = BUF_SIZE * 2;

#ifdef COMPILE_FOR_RTL_SIMULATOR_TEST
  config.use_fixed_addr = true;
  config.fixed_addr = 0x7f7e8e600000;
#endif

  if (rdma_init_context(&ctx, &config) < 0) {
    return -1;
  }

  // src_buffer is at the beginning
  src_buffer = ctx.buffer;

  // Generate data pattern
  for (int i = 0; i < msg_len; i++) {
    src_buffer[i] = i & 0xFF;
  }

  // Connect to server
  printf("[CLIENT] Connecting to %s:%d...\n", server_ip, DEFAULT_PORT);
  if (tcp_client_connect(&transport, server_ip, DEFAULT_PORT, 10) < 0) {
    rdma_destroy_context(&ctx);
    return -1;
  }

  // Exchange QP information
  local_info.qp_num = ctx.qp->qp_num;
  local_info.rkey = ctx.mr->rkey;
  local_info.remote_addr = (uint64_t)src_buffer;

  if (rdma_exchange_qp_info(transport.sock_fd, &local_info, &remote_info) < 0) {
    tcp_transport_close(&transport);
    rdma_destroy_context(&ctx);
    return -1;
  }

  printf("[CLIENT] Remote: qp_num=%u, rkey=0x%x, addr=0x%lx\n",
         remote_info.qp_num, remote_info.rkey, remote_info.remote_addr);

  // Connect QP
  uint32_t dest_gid_ipv4 = 0x1122330A; // server IP
  if (rdma_connect_qp(ctx.qp, remote_info.qp_num, dest_gid_ipv4) < 0) {
    tcp_transport_close(&transport);
    rdma_destroy_context(&ctx);
    return -1;
  }

  printf("[CLIENT] QP connected, starting RDMA WRITE loop\n");

  // Prepare RDMA WRITE operation
  struct ibv_sge sge = {
      .addr = (uint64_t)src_buffer, .length = msg_len, .lkey = ctx.mr->lkey};

  struct ibv_send_wr wr = {
      .sg_list = &sge,
      .num_sge = 1,
      .opcode = IBV_WR_RDMA_WRITE,
      .send_flags = IBV_SEND_SIGNALED,
      .wr_id = 17,
      .wr = {.rdma = {.remote_addr = remote_info.remote_addr,
                      .rkey = remote_info.rkey}}};

  struct ibv_send_wr *bad_wr;

  // Main test loop
  int failed_rounds = 0;
  for (int round = 1; round <= num_rounds; round++) {
    printf("\n--- Round %d/%d ---\n", round, num_rounds);
    COMPILER_BARRIER();

    printf("[CLIENT] Posting RDMA WRITE (%d bytes)...\n", msg_len);
    if (ibv_post_send(ctx.qp, &wr, &bad_wr)) {
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
      fprintf(stderr,
              ANSI_COLOR_RED
              "[CLIENT] RDMA WRITE failed: status=%d\n" ANSI_COLOR_RESET,
              wc.status);
      failed_rounds++;
    } else {
      printf(ANSI_COLOR_GREEN
             "[CLIENT] RDMA WRITE completed (wr_id=%lu)\n" ANSI_COLOR_RESET,
             wc.wr_id);
    }

    // Sync with server
    if (rdma_sync_client_to_server(transport.sock_fd) < 0) {
      fprintf(stderr, "[ERROR] Sync failed\n");
      break;
    }

    // Regenerate pattern for next round
    for (int i = 0; i < msg_len; i++) {
      src_buffer[i] = i & 0xFF;
    }
  }

  printf("\n========== Client Test Summary ==========\n");
  printf("Total rounds: %d\n", num_rounds);
  printf("Passed: " ANSI_COLOR_GREEN "%d" ANSI_COLOR_RESET "\n",
         num_rounds - failed_rounds);
  printf("Failed: " ANSI_COLOR_RED "%d" ANSI_COLOR_RESET "\n", failed_rounds);
  printf("=========================================\n");

  tcp_transport_close(&transport);
  rdma_destroy_context(&ctx);

  return (failed_rounds == 0) ? 0 : -1;
}

int main(int argc, char *argv[]) {
  setvbuf(stdout, NULL, _IONBF, 0);

  if (argc < 1) {
    fprintf(stderr, "Usage:\n");
    fprintf(stderr, "  Server: %s [msg_len] [rounds]\n", argv[0]);
    fprintf(stderr, "  Client: %s [msg_len] [rounds] <server_ip>\n", argv[0]);
    fprintf(stderr, "\nExample:\n");
    fprintf(stderr, "  Server: %s 8192 10\n", argv[0]);
    fprintf(stderr, "  Client: %s 8192 10 127.0.0.1\n", argv[0]);
    fprintf(stderr, "\nDefaults: msg_len=%d, rounds=%d\n", BUF_SIZE,
            MAX_ROUNDS);
    fprintf(stderr, "Note: Device index is fixed (server=1, client=0)\n");
    return EXIT_FAILURE;
  }

  // Parse msg_len (default: BUF_SIZE)
  int msg_len = BUF_SIZE;
  if (argc >= 2) {
    msg_len = atoi(argv[1]);
    if (msg_len == 0)
      msg_len = BUF_SIZE;
  }

  // Detect mode based on last argument
  // If last argument looks like an IP address (contains '.' or ':'), it's
  // client mode
  bool is_client = false;
  const char *server_ip = NULL;

  if (argc >= 2) {
    const char *last_arg = argv[argc - 1];
    if (strchr(last_arg, '.') != NULL || strchr(last_arg, ':') != NULL) {
      // Client mode: last argument is server IP
      is_client = true;
      server_ip = last_arg;
    }
  }

  // Parse rounds (default: MAX_ROUNDS)
  int num_rounds = MAX_ROUNDS;
  if (argc >= 3) {
    // If client mode, rounds is at argc-2 (before IP)
    // If server mode, rounds is at argv[2]
    int rounds_idx = is_client ? (argc - 2) : 2;
    if (rounds_idx >= 2 && rounds_idx < argc) {
      num_rounds = atoi(argv[rounds_idx]);
      if (num_rounds == 0)
        num_rounds = MAX_ROUNDS;
    }
  }

  if (is_client) {
    // Client mode: dev_index=0 (fixed)
    int dev_index = 1;
    return run_client(msg_len, server_ip, dev_index, num_rounds);
  } else {
    // Server mode: dev_index=1 (fixed)
    int dev_index = 0;
    return run_server(msg_len, dev_index, num_rounds);
  }
}
