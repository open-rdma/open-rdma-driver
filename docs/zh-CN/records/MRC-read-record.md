# MRC 阅读记录：按协议机制看 open-rdma 可借鉴点

## 本期主题

MRC 面向 multipath spraying 场景，默认 packet 乱序是正常现象，因此它不再把 `PSN == ePSN` 作为唯一可接收条件，而是通过有界 bitmap window、Reliability SACK/NACK、Transport ACK 分层、ECN/RTT 反馈和 Probe 机制来维持可靠性。本期主题是从这些协议机制出发，梳理 open-rdma 在 bitmap window、ACK/SACK 分层、流控反馈和语义完成边界上可以借鉴的设计点。

参考资料：

- `OCP-MRC-1.0.pdf`
- `open-rdma-rtl/src/RdmaHeaders.bsv`
- `open-rdma-rtl/src/PsnContinousChecker.bsv`
- `open-rdma-rtl/src/RQ.bsv`
- `open-rdma-rtl/src/AutoAckGenerator.bsv`

## 1. bitmap window 长度与上限

MRC 的 responder 用 `ePSN = cack_psn + 1` 和 `max_psn_range/MPR` 定义可接收窗口：`[ePSN, ePSN + max_psn_range)`。窗口内 packet 即使乱序也应按可靠性规则处理；窗口外 packet 不进入正常可靠性处理。这个设计的关键点是：bitmap window 有协议上限，requester 也必须受这个上限约束，不能无限发送超过 responder 跟踪能力的 packet。

open-rdma-rtl 当前已经有定长 bitmap 基础：

```bsv
typedef 128 ACK_BITMAP_WIDTH;
typedef 16  ACK_WINDOW_STRIDE;
typedef Bit#(ACK_BITMAP_WIDTH) AckBitmap;
```

`PsnContinousChecker.bsv::mkBitmapWindowStorage` 用 `leftBound + data` 表示一个 QP 的 ACK bitmap，并在新 PSN 超过当前覆盖范围时滑动窗口。这个机制硬件上直接，但更像“本地 bitmap 可以持续前滑”的实现细节，还没有把 responder 可接受乱序范围作为 MPR 暴露给发送端。

MRC 的启发是：可以保留 128-bit bitmap 这种硬件友好的定长结构，但给它加上协议上限。`max_psn_range` 可以按 128 packets 为单位配置或协商，因此固定窗口不等于窗口必须很小。实现上可以把接收跟踪资源组织成多个 128-bit bank：资源少时窗口小，资源多时窗口大；发送端只需要遵守 `cack_psn + MPR` 的上界。

这样做的收益是硬件判断更简单：packet 是否落在 tracking window 内、是否需要丢弃或报告异常，都可以在固定范围内完成。代价是可接受乱序度不再无限，但可以通过增大 128-bit bank 数量弥补。

## 2. bitmap 合并位置

当前 open-rdma-rtl 中，`RQ.bsv` 大致在 packet 通过 QP/MR/长度检查后，先发出 payload consume 请求，等 `PayloadCon` 在最后一个 payload beat 返回 consume response，再由 `handleConResp` 向 `AutoAckGenerator` 提交 `AutoAckGeneratorReq`。也就是说，当前 bitmap 合并位置偏后，更接近“payload 已经进入放置流水线末端”之后再更新 ACK bitmap。

MRC 的做法更激进：packet 通过物理层正确性、基础 RC 检查和必要资源检查后，就进入 responder bitmap 合并与 SACK 报告阶段，不等待 payload 最终写入内存。这样可靠性反馈更快，requester 可以更早知道哪些 PSN 已到达、哪些位置存在 hole、当前 MPR 和拥塞反馈是什么。

这个思路可以借鉴，但必须同时拆开两类状态：

- packet reliability state：基础检查通过后即可进入 bitmap，可触发 SACK/ACK report。
- semantic completion state：payload placement、WriteImm immediate data stash、RQ/CQ 条件满足后，才能推进语义完成。

也就是说，如果 open-rdma 将 bitmap 合并提前，bitmap 就不能再承担 CQ 完成语义。可以让合并后的 `ePSN` 或 packet tag 继续随 payload placement 流水线向后走，到最后由语义层决定是否生成 CQ 或 Transport ACK/NAK。早 SACK 只说明 packet-level reliability 层已经记录该 PSN，不说明 message 已完成。

## 3. SACK 与 Transport ACK 分层

MRC 中 Reliability SACK/NACK 与 RDMA Transport ACK/NAK 是逻辑分离的。SACK/NACK 负责 packet 层可靠性状态：`cack_psn`、`sack_bitmap`、MPR、ECN、RTT、`ooo_count` 等；Transport ACK/NAK 负责 RDMA transport / semantic 层结果，例如 operation 是否完成、是否访问错误、是否请求错误。

p26 中“accepted packet 如果满足 IBTA ACK/NACK 条件，responder 必须发 Transport ACK”并不和 packet 乱序冲突。这里的 ACK 是语义层 ACK，不是 SACK bitmap。一个 packet 可以先被 reliability 层 SACK，因为 responder 已经接收并记录了它；但后续如果 semantic processing 失败，仍可能被 Transport NAK。

因此 open-rdma 后续如果引入更接近 MRC 的 SACK，需要避免把以下概念混在一起：

- SACK bitmap：packet 是否到达并进入可靠性跟踪。
- Transport ACK/NAK：RDMA operation 的语义结果。
- CQ：本地软件可见的完成事件。

当前 `AutoAckGenerator.bsv` 生成的是基于 RC `ACKNOWLEDGE` opcode 和自定义 AETH bitmap 的 ACK/NAK 风格包，还不是 MRC 独立 opcode 的 Reliability SACK/NACK。如果继续演进，至少需要在文档和状态机里先明确：bitmap report 不直接等价于 CQ，也不直接等价于 semantic ACK。

## 4. 流控与拥塞反馈

MRC 的发送资格同时受两类窗口约束：

- MPR / PSN window：packet 数量维度，限制 responder 能跟踪多少 in-flight packet。
- NSCC congestion window：byte 维度，结合 ECN、RTT、ACK-clock 等信号调整发送速率。

Reliability SACK 中除了 bitmap，还会携带拥塞和状态反馈，例如 ECN marked 信息、timestamp/RTT、`ooo_count`、`rcvd_bytes`、`rcv_cwnd_pen` 等。这样 requester 不只是根据 timeout 猜测丢包，也能根据 responder 的乱序程度和网络拥塞信号调节发送。

对 open-rdma 来说，可以先把 MPR 和 congestion window 看成两个独立约束：即使 cwnd 允许继续发，也不能超过 responder 的 PSN tracking window；即使 MPR 允许，如果 ECN/RTT 表明拥塞，也要降低发送节奏。这个方向比只靠 ACK bitmap 判断是否丢包更完整。

`ooo_count` 也值得保留关注。它不是 bitmap 本身，而是对累计 ACK 以上乱序包数量的聚合反馈。发送端可以用它辅助判断 hole 是真实丢包，还是只是多路径乱序尚未收敛。

## 5. WriteImm 与 RNR-NAK

MRC 只重点支持 Write 和 WriteImm。Write/WriteImm payload 可以乱序放置，但 WriteImm completion 必须按 requestor send order 交付。为此，MRC 的 METH header 中有 `MSN` 和 `RQMSN`：`RQMSN` 对 WriteImm 有效，`MSN/RQMSN` 可以用于跟踪 in-flight WriteImm 和接收侧 completion 顺序。

同时，MRC 用 `max_wimm_inflight` 限制未完成 WriteImm 数量，并明确不支持 RNR-NAK 硬件级重试。如果 responder 无法交付 WriteImm completion 到 RQ，QP 进入 error，并返回语义层错误。这个设计把接收端资源是否充足的问题前移到发送端和软件配置，避免在硬件里维护复杂的 RNR retry 状态。

open-rdma 可以借鉴这个边界：WriteImm 的 in-flight 数量不要只靠后端资源碰运气，而应作为 QP 能力或连接参数暴露出来。发送侧用 `MSN/RQMSN` 或等价字段限制未完成 WriteImm；接收侧只负责检查资源、stash immediate data，并在语义条件满足后按序完成。

## 6. Probe

MRC 定义 Reliability Probe，requester 可以发送不消耗数据 PSN 的 probe request，responder 返回带 `pr=1` 的 SACK，并回显 `probe_id`。它的用途是主动探测 responder 的可靠性状态或路径健康程度，而不是等待正常数据包触发 ACK。

对 open-rdma 来说，Probe 可以先作为调试和状态重新同步工具：requester 拉取当前 QP 的 bitmap base、bitmap、MPR 和时间戳，用于确认双方可靠性状态是否一致。即使暂时不做完整路径健康管理，这个机制也能帮助处理 idle flow、ACK 丢失或调试 bitmap 状态不一致的问题。

## 总结

MRC 对 open-rdma 的启发可以压缩成几条协议边界：

- bitmap window 应该有上限，并以硬件友好的固定粒度扩展。
- bitmap 可以更早合并，但不能因此提前 CQ 或语义完成。
- SACK 是 packet reliability 状态，Transport ACK/NAK 才是语义结果。
- 流控需要同时考虑 MPR、NSCC cwnd、ECN、RTT 和乱序反馈。
- WriteImm 的 in-flight 数量应显式受控，避免依赖 RNR-NAK 硬件重试。
- Probe 可以作为主动状态探测与重新同步机制。

这些点不要求一次性切换到完整 MRC 包格式，但可以作为 open-rdma 后续重构 ACK bitmap、接收窗口和语义完成边界时的参考。
