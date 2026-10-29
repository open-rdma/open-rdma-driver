# RX 任务 Agent 角色索引

本目录记录本轮 OpenRDMA-flow TX→RX 实现和验证任务中**实际使用过的六类工作角色**。这是根据本轮调用与交付内容整理的可复用中文角色定义，不是内建 Agent 的原始系统提示词，也不包含内部运行 ID 或私有上下文。

| 角色文件 | 本轮使用方式 | 职责 |
|---|---|---|
| [TX 链路探索](tx-path-explorer.md) | Explore 子 Agent | 还原 M0–M3 的 wire format、四通道调度和 payload 契约 |
| [RX 初稿审计](rx-draft-auditor.md) | Explore 子 Agent | 逐模块检查五项目标的实现与缺口 |
| [测试体系探索](cocotb-infrastructure-explorer.md) | Explore 子 Agent | 找出 BSC→Verilog→cocotb 的可复用模式和双 simulator 测试要求 |
| [RX 架构规划](rx-architecture-planner.md) | Plan 子 Agent | 设计双水位、四路 packet DMA、globalSeq retire 和 Flag barrier 的实施方案 |
| [RTL 与测试审阅](rx-code-reviewer.md) | code-review fork | 从具体反例审查行为回归、调度、反压与覆盖缺口 |
| [RX 实施与验证](rx-implementer.md) | 主 Agent | 修改 BSV、编写 cocotb+Python/Makefile、编译执行并记录真实结果 |

## 复用方式与边界

1. 每份文件的“可复用任务提示”可作为再次委派任务时的工作说明；调用时明确仓库位置、分支和只读/可编辑权限。Explore 和 Plan 角色保持只读，除非重新获得明确修改授权。
2. 这些文件位于项目指定的 `mvp/rx/agent/`，**并非** Claude Code 自动发现的 `.claude/agents/` 目录；不会因为写入而自动启用 Agent。
3. 本轮**没有调用** `RDMA-AI-TEAM/agents/` 下的六人 RDMA 协议审阅集群，不能将其工作或审阅结论归属到本目录的角色。
4. 设计和验证以 [RX 说明](../README.md) 与 [最新验证记录](../VALIDATION.md) 为准。已有 cocotb 通过只证明抽象 DMA adapter 下的功能行为，不等于真实 FTile/PCIe/GPU 板级验证或 400G 时序收敛。
