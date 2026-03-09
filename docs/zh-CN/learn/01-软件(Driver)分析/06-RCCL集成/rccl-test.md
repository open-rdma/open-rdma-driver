## 目前问题

### mock 模式
sge=0 mock 模式不支持

destroy 的时候会出现：
```log

[2025-11-17T08:08:15.903340075Z INFO  blue_rdma_driver::verbs::mock] destroying qp: 6422
[2025-11-17T08:08:15.903333895Z INFO  blue_rdma_driver::verbs::mock] qp: 4773 destroyed

thread '<unnamed>' (2576740) panicked at /home/peng/projects/rdma_all/blue-rdma-driver/rust-driver/src/verbs/mock.rs:396:18:
called `Option::unwrap()` on a `None` value
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
[2025-11-17T08:08:15.903394898Z INFO  bluerdma_rust::rxe::ctx_ops] Deregistering memory region
[2025-11-17T08:08:15.903450590Z INFO  blue_rdma_driver::verbs::mock] mock dereg mr
[2025-11-17T08:08:15.903500492Z INFO  bluerdma_rust::rxe::ctx_ops] Destroying completion queue

```


### 仿真模式

未测试，但是担心page 大小会成为影响因素，目前page size 只能指定一种
