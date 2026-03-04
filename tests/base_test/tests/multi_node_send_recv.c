#include "../lib/rdma_common.h"
#include "../lib/rdma_transport.h"
#include "../lib/rdma_debug.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/in.h>

#define DEFAULT_BASE_PORT 12400
#define MAX_PEERS 32
#define LOOPBACK_IP "127.0.0.1"

struct rank_msg {
	int rank;
};

struct peer_link {
	int peer_rank;
	int sock_fd;
	bool connected;
	struct ibv_qp *qp;
	struct qp_info local_info;
	struct qp_info remote_info;
	char *send_buf;
	char *recv_buf;
};

static int send_all(int fd, const void *buf, size_t len) {
	const char *ptr = (const char *)buf;
	size_t sent = 0;

	while (sent < len) {
		ssize_t ret = send(fd, ptr + sent, len - sent, 0);
		if (ret < 0) {
			if (errno == EINTR) {
				continue;
			}
			return -1;
		}
		if (ret == 0) {
			return -1;
		}
		sent += (size_t)ret;
	}

	return 0;
}

static int recv_all(int fd, void *buf, size_t len) {
	char *ptr = (char *)buf;
	size_t recvd = 0;

	while (recvd < len) {
		ssize_t ret = recv(fd, ptr + recvd, len - recvd, 0);
		if (ret < 0) {
			if (errno == EINTR) {
				continue;
			}
			return -1;
		}
		if (ret == 0) {
			return -1;
		}
		recvd += (size_t)ret;
	}

	return 0;
}

static int exchange_rank(int sock_fd, int local_rank, int *remote_rank) {
	struct rank_msg tx = { .rank = local_rank };
	struct rank_msg rx = { .rank = -1 };

	if (send_all(sock_fd, &tx, sizeof(tx)) < 0) {
		return -1;
	}
	if (recv_all(sock_fd, &rx, sizeof(rx)) < 0) {
		return -1;
	}

	*remote_rank = rx.rank;
	return 0;
}

static int create_listener(int port, int backlog) {
	int fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0) {
		fprintf(stderr, "[ERROR] socket failed: %s\n", strerror(errno));
		return -1;
	}

	int opt = 1;
	if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
		fprintf(stderr, "[ERROR] setsockopt(SO_REUSEADDR) failed: %s\n", strerror(errno));
		close(fd);
		return -1;
	}

	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = INADDR_ANY,
		.sin_port = htons((uint16_t)port)
	};

	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		fprintf(stderr, "[ERROR] bind(%d) failed: %s\n", port, strerror(errno));
		close(fd);
		return -1;
	}

	if (listen(fd, backlog) < 0) {
		fprintf(stderr, "[ERROR] listen failed: %s\n", strerror(errno));
		close(fd);
		return -1;
	}

	return fd;
}

static void cleanup_links(struct peer_link *links, int world_size, struct rdma_context *shared_ctx) {
	for (int i = 0; i < world_size; i++) {
		if (!links[i].connected) {
			continue;
		}

		if (links[i].qp && shared_ctx && shared_ctx->qp && links[i].qp != shared_ctx->qp) {
			ibv_destroy_qp(links[i].qp);
			links[i].qp = NULL;
		}

		if (links[i].sock_fd >= 0) {
			close(links[i].sock_fd);
			links[i].sock_fd = -1;
		}

		links[i].connected = false;
	}
}

static int setup_all_connections(struct peer_link *links,
								 int rank,
								 int world_size,
								 int base_port) {
	int listen_fd = -1;

	for (int i = 0; i < world_size; i++) {
		links[i].peer_rank = i;
		links[i].sock_fd = -1;
		links[i].connected = false;
		links[i].qp = NULL;
		links[i].send_buf = NULL;
		links[i].recv_buf = NULL;
	}

	listen_fd = create_listener(base_port + rank, world_size + 4);
	if (listen_fd < 0) {
		return -1;
	}

	for (int peer = rank + 1; peer < world_size; peer++) {
		struct tcp_transport tr;
		int remote_rank = -1;

		printf("[RANK %d] Connect to rank %d (%s:%d)\n",
			   rank, peer, LOOPBACK_IP, base_port + peer);
		if (tcp_client_connect(&tr, LOOPBACK_IP, base_port + peer, 30) < 0) {
			fprintf(stderr, "[ERROR] connect to rank %d failed\n", peer);
			close(listen_fd);
			return -1;
		}

		if (exchange_rank(tr.sock_fd, rank, &remote_rank) < 0) {
			fprintf(stderr, "[ERROR] rank exchange with rank %d failed\n", peer);
			close(tr.sock_fd);
			close(listen_fd);
			return -1;
		}

		if (remote_rank != peer) {
			fprintf(stderr, "[ERROR] expected peer %d, got %d\n", peer, remote_rank);
			close(tr.sock_fd);
			close(listen_fd);
			return -1;
		}

		links[peer].sock_fd = tr.sock_fd;
		links[peer].connected = true;
	}

	int expected_lower = rank;
	int accepted = 0;
	while (accepted < expected_lower) {
		int fd = accept(listen_fd, NULL, NULL);
		if (fd < 0) {
			if (errno == EINTR) {
				continue;
			}
			fprintf(stderr, "[ERROR] accept failed: %s\n", strerror(errno));
			close(listen_fd);
			return -1;
		}

		int remote_rank = -1;
		if (exchange_rank(fd, rank, &remote_rank) < 0) {
			fprintf(stderr, "[ERROR] rank exchange on accepted socket failed\n");
			close(fd);
			close(listen_fd);
			return -1;
		}

		if (remote_rank < 0 || remote_rank >= rank) {
			fprintf(stderr, "[ERROR] invalid remote rank %d (local rank=%d)\n", remote_rank, rank);
			close(fd);
			close(listen_fd);
			return -1;
		}

		if (links[remote_rank].connected) {
			fprintf(stderr, "[ERROR] duplicate connection from rank %d\n", remote_rank);
			close(fd);
			close(listen_fd);
			return -1;
		}

		printf("[RANK %d] Accepted connection from rank %d\n", rank, remote_rank);
		links[remote_rank].sock_fd = fd;
		links[remote_rank].connected = true;
		accepted++;
	}

	close(listen_fd);
	return 0;
}

static struct ibv_qp *create_peer_qp(struct rdma_context *shared_ctx) {
	struct ibv_qp_init_attr qp_init_attr = {
		.send_cq = shared_ctx->send_cq,
		.recv_cq = shared_ctx->recv_cq,
		.cap = {
			.max_send_wr = 128,
			.max_recv_wr = 128,
			.max_send_sge = 1,
			.max_recv_sge = 1,
		},
		.qp_type = IBV_QPT_RC,
	};

	return ibv_create_qp(shared_ctx->pd, &qp_init_attr);
}

static int setup_rdma_for_all_peers(struct peer_link *links,
									int rank,
									int world_size,
									int msg_len,
									struct rdma_context *shared_ctx) {
	int slot = 0;
	for (int peer = 0; peer < world_size; peer++) {
		if (peer == rank || !links[peer].connected) {
			continue;
		}

		if (slot == 0) {
			links[peer].qp = shared_ctx->qp;
		} else {
			links[peer].qp = create_peer_qp(shared_ctx);
			if (!links[peer].qp) {
				fprintf(stderr, "[ERROR] create QP for peer %d failed\n", peer);
				return -1;
			}
		}

		char *base = shared_ctx->buffer + (size_t)slot * (size_t)(msg_len * 2);
		links[peer].send_buf = base;
		links[peer].recv_buf = base + msg_len;
		memset(links[peer].send_buf, 'A' + (rank % 26), msg_len);
		memset(links[peer].recv_buf, 0, msg_len);

		links[peer].local_info.qp_num = links[peer].qp->qp_num;
		links[peer].local_info.rkey = shared_ctx->mr->rkey;
		links[peer].local_info.remote_addr = (uint64_t)links[peer].recv_buf;

		if (rdma_exchange_qp_info(links[peer].sock_fd, &links[peer].local_info, &links[peer].remote_info) < 0) {
			return -1;
		}

		uint32_t remote_gid_ipv4 = 0x1122330Au + (uint32_t)peer;
		if (rdma_connect_qp(links[peer].qp, links[peer].remote_info.qp_num, remote_gid_ipv4) < 0) {
			return -1;
		}

		slot++;
	}

	return 0;
}

static int post_one_recv(struct peer_link *link, int msg_len, uint32_t lkey, uint64_t wr_id) {
	struct ibv_recv_wr wr = {0};
	struct ibv_recv_wr *bad_wr = NULL;
	struct ibv_sge sge = {
		.addr = (uint64_t)link->recv_buf,
		.length = msg_len,
		.lkey = lkey
	};

	wr.wr_id = wr_id;
	wr.sg_list = &sge;
	wr.num_sge = 1;

	if (ibv_post_recv(link->qp, &wr, &bad_wr) != 0) {
		return -1;
	}
	rdma_log("Posted recv from peer %d (wr_id=0x%lx)\n", link->peer_rank, wr_id);

	return 0;
}

static int post_one_send(struct peer_link *link, int msg_len, uint32_t lkey, uint64_t wr_id) {
	struct ibv_send_wr wr = {0};
	struct ibv_send_wr *bad_wr = NULL;
	struct ibv_sge sge = {
		.addr = (uint64_t)link->send_buf,
		.length = msg_len,
		.lkey = lkey
	};

	wr.wr_id = wr_id;
	wr.sg_list = &sge;
	wr.num_sge = 1;
	wr.opcode = IBV_WR_SEND;
	wr.send_flags = IBV_SEND_SIGNALED;

	if (ibv_post_send(link->qp, &wr, &bad_wr) != 0) {
		return -1;
	}

	rdma_log("Posted send to peer %d (wr_id=0x%lx)\n", link->peer_rank, wr_id);
	return 0;
}

static int poll_one_wc(struct ibv_cq *cq, struct ibv_wc *wc, int timeout_ms) {
	int loops = timeout_ms;
	while (loops-- > 0) {
		int n = ibv_poll_cq(cq, 1, wc);
		if (n < 0) {
			rdma_log("[ERROR] ibv_poll_cq failed: %d\n", n);
			return -1;
		}
		if (n > 0) {
			return 0;
		}
		usleep(1000);
	}
	rdma_log("[ERROR] poll_one_wc timeout after %d ms\n", timeout_ms);
	return -1;
}

static int run_round(struct peer_link *links,
					 int rank,
					 int world_size,
					 int msg_len,
					 struct rdma_context *shared_ctx) {
	const uint64_t RECV_BASE = 0x20000000ULL;
	const uint64_t SEND_BASE = 0x10000000ULL;
	int send_cnt = 0;
	int recv_cnt = 0;

	for (int peer = 0; peer < world_size; peer++) {
		if (peer == rank || !links[peer].connected) {
			continue;
		}

		if (peer < rank) {
			recv_cnt++;
		}
		if (peer > rank) {
			send_cnt++;
		}

		if (peer < rank &&
			post_one_recv(&links[peer], msg_len, shared_ctx->mr->lkey, RECV_BASE | (uint64_t)peer) < 0) {
			fprintf(stderr, "[ERROR] rank %d post recv from peer %d failed\n", rank, peer);
			return -1;
		}
	}

	for (int peer = 0; peer < world_size; peer++) {
		if (peer == rank || !links[peer].connected) {
			continue;
		}
		if (rdma_handshake(links[peer].sock_fd) < 0) {
			fprintf(stderr, "[ERROR] rank %d handshake(before send) with peer %d failed\n", rank, peer);
			return -1;
		}
		rdma_log("Handshake before send with peer %d done\n", peer);
	}

	for (int peer = 0; peer < world_size; peer++) {
		if (peer == rank || !links[peer].connected) {
			continue;
		}
		if (peer < rank) {
			continue;
		}

		if (post_one_send(&links[peer], msg_len, shared_ctx->mr->lkey, SEND_BASE | (uint64_t)peer) < 0) {
			fprintf(stderr, "[ERROR] rank %d post send to peer %d failed\n", rank, peer);
			return -1;
		}
	}

	int send_done = 0;
	while (send_done < send_cnt) {
		struct ibv_wc send_wc = {0};
		if (poll_one_wc(shared_ctx->send_cq, &send_wc, 1000000) < 0 || send_wc.status != IBV_WC_SUCCESS) {
			fprintf(stderr, "[ERROR] rank %d send completion failed (status=%d)\n",
					rank, send_wc.status);
			return -1;
		}

		int peer = (int)(send_wc.wr_id & 0xFFFFULL);
		if ((send_wc.wr_id & SEND_BASE) != SEND_BASE || peer < 0 || peer >= world_size || !links[peer].connected) {
			fprintf(stderr, "[ERROR] rank %d got unexpected send wr_id=0x%lx\n", rank, send_wc.wr_id);
			return -1;
		}
		if (peer <= rank) {
			fprintf(stderr, "[ERROR] rank %d got send completion from invalid peer %d\n", rank, peer);
			return -1;
		}
		send_done++;
	}

	int recv_done = 0;
	while (recv_done < recv_cnt) {
		struct ibv_wc recv_wc = {0};
		if (poll_one_wc(shared_ctx->recv_cq, &recv_wc, 1000000) < 0 || recv_wc.status != IBV_WC_SUCCESS) {
			fprintf(stderr, "[ERROR] rank %d recv completion failed (status=%d)\n",
					rank, recv_wc.status);
			return -1;
		}

		int peer = (int)(recv_wc.wr_id & 0xFFFFULL);
		if ((recv_wc.wr_id & RECV_BASE) != RECV_BASE || peer < 0 || peer >= world_size || !links[peer].connected) {
			fprintf(stderr, "[ERROR] rank %d got unexpected recv wr_id=0x%lx\n", rank, recv_wc.wr_id);
			return -1;
		}
		if (peer >= rank) {
			fprintf(stderr, "[ERROR] rank %d got recv completion from invalid peer %d\n", rank, peer);
			return -1;
		}
		recv_done++;
	}

	for (int peer = 0; peer < world_size; peer++) {
		if (peer == rank || !links[peer].connected) {
			continue;
		}
		if (peer >= rank) {
			continue;
		}

		char expected = 'A' + (peer % 26);
		size_t error_count = 0;
		struct rdma_pattern pattern = RDMA_PATTERN_CHAR(expected);
		rdma_verify_data(links[peer].recv_buf, msg_len, &pattern, &error_count);
		if (error_count > 0) {
			fprintf(stderr, "[ERROR] rank %d verify data from peer %d failed: %zu bytes mismatch\n",
					rank, peer, error_count);
			return -1;
		}
	}

	for (int peer = 0; peer < world_size; peer++) {
		if (peer == rank || !links[peer].connected) {
			continue;
		}
		if (rdma_handshake(links[peer].sock_fd) < 0) {
			fprintf(stderr, "[ERROR] rank %d final handshake with peer %d failed\n", rank, peer);
			return -1;
		}
	}

	return 0;
}

int main(int argc, char *argv[]) {
	setvbuf(stdout, NULL, _IONBF, 0);

	if (argc < 4) {
		fprintf(stderr,
				"Usage: %s <msg_len> <rank> <world_size> [base_port]\n",
				argv[0]);
		fprintf(stderr, "Example(4 nodes): %s 4096 0 4 12400\n", argv[0]);
		return EXIT_FAILURE;
	}

	int msg_len = atoi(argv[1]);
	int rank = atoi(argv[2]);
	int world_size = atoi(argv[3]);
	int base_port = (argc >= 5) ? atoi(argv[4]) : DEFAULT_BASE_PORT;

	if (msg_len <= 0 || world_size <= 1 || world_size > MAX_PEERS || rank < 0 || rank >= world_size) {
		fprintf(stderr, "[ERROR] Invalid args: msg_len=%d rank=%d world_size=%d\n",
				msg_len, rank, world_size);
		return EXIT_FAILURE;
	}

	struct peer_link links[MAX_PEERS];
	struct rdma_context shared_ctx;
	bool shared_ctx_inited = false;
	memset(links, 0, sizeof(links));
	memset(&shared_ctx, 0, sizeof(shared_ctx));

	printf("========== MULTI NODE TEST ==========\n");
	printf("rank=%d world_size=%d msg_len=%d base_port=%d\n", rank, world_size, msg_len, base_port);
	printf("Rule: lower rank actively connects to higher rank\n");
	printf("TCP IP fixed to %s\n", LOOPBACK_IP);

	if (setup_all_connections(links, rank, world_size, base_port) < 0) {
		cleanup_links(links, world_size, NULL);
		return EXIT_FAILURE;
	}

	int connected_peers = 0;
	for (int peer = 0; peer < world_size; peer++) {
		if (peer != rank && links[peer].connected) {
			connected_peers++;
		}
	}

	if (connected_peers == 0) {
		fprintf(stderr, "[ERROR] no connected peers\n");
		cleanup_links(links, world_size, NULL);
		return EXIT_FAILURE;
	}

	struct rdma_config config;
	rdma_default_config(&config);
	config.dev_index = rank;
	config.buffer_size = (size_t)connected_peers * (size_t)(msg_len * 2);
	if (rdma_init_context(&shared_ctx, &config) < 0) {
		cleanup_links(links, world_size, NULL);
		return EXIT_FAILURE;
	}
	shared_ctx_inited = true;

	if (setup_rdma_for_all_peers(links, rank, world_size, msg_len, &shared_ctx) < 0) {
		fprintf(stderr, "[ERROR] setup RDMA for peers failed\n");
		cleanup_links(links, world_size, &shared_ctx);
		rdma_destroy_context(&shared_ctx);
		return EXIT_FAILURE;
	}

	if (run_round(links, rank, world_size, msg_len, &shared_ctx) < 0) {
		cleanup_links(links, world_size, &shared_ctx);
		if (shared_ctx_inited) {
			rdma_destroy_context(&shared_ctx);
		}
		return EXIT_FAILURE;
	}

	printf(ANSI_COLOR_GREEN "[RANK %d] Multi-node P2P round PASSED\n" ANSI_COLOR_RESET, rank);
	cleanup_links(links, world_size, &shared_ctx);
	if (shared_ctx_inited) {
		rdma_destroy_context(&shared_ctx);
	}
	return EXIT_SUCCESS;
}