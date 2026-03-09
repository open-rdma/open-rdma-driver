# EthernetFrameIO256.bsv RETH 提取位置修复

## 问题描述

在 `EthernetFrameIO256.bsv` 的 `mkRdmaMetaAndPayloadExtractor` 模块中，RETH (RDMA Extended Transport Header) 的提取位置不正确，导致解析错误。

## 原始实现（错误）

之前在 `handleSecondBeat` 规则中尝试提取 RETH：

```bsv
rule handleSecondBeat if (stateReg == RdmaMetaAndPayloadExtractorStateHandleSecondBeat);
    // ... 其他代码 ...

    // 错误：此时 rdmaExtendHeaderBuf 只包含第二个 beat 的部分扩展头
    let reth = extractPriRETH(rdmaMeta.header.rdmaExtendHeaderBuf, rdmaMeta.header.bth.trans);
    let isZeroPayload = reth.dlen == 0;
    rdmaMeta.hasPayload = rdmaMeta.hasPayload && !isZeroPayload;
endrule
```

## 问题根因

RDMA 包的布局（每个 beat 32 字节）：

- **第一个 beat**: ETH header (14B) + IP header 的前 18 字节
- **第二个 beat**: IP header 剩余部分 (2B) + UDP header (8B) + BTH (12B) + 扩展头片段 (10B)
- **第三个 beat**: 扩展头剩余部分 (32B)

在 `handleSecondBeat` 中：
- `rdmaExtendHeaderBuf` 只包含第二个 beat 中的 10 字节扩展头片段
- 此时还没有接收到第三个 beat 的数据
- **RETH 需要 16 字节**，但此时只有 10 字节可用，导致提取的 RETH 数据不完整

## 修复方案

将 RETH 提取移到 `handleThirdBeat` 规则中：

```bsv
rule handleThirdBeat if (stateReg == RdmaMetaAndPayloadExtractorStateHandleThirdBeat);
    let curFpDebugTime <- getSimulationTime;
    let ds = ethPipeInQ.first;
    ethPipeInQ.deq;
    ds.data = swapEndianByte(ds.data);

    let rdmaMeta = partialRdmaMetaReg;
    let fpDebugTime = rdmaMeta.fpDebugTime;

    // 拼接完整的扩展头缓冲区
    RdmaExtendHeaderFragmentInSecondBeat rdmaExtendHeaderSecondBeatFragment =
        truncateLSB(rdmaMeta.header.rdmaExtendHeaderBuf);
    rdmaMeta.header.rdmaExtendHeaderBuf =
        truncateLSB({rdmaExtendHeaderSecondBeatFragment, ds.data});
    rdmaMeta.fpDebugTime = curFpDebugTime;

    // 正确：现在 rdmaExtendHeaderBuf 包含完整的扩展头数据
    let reth = extractPriRETH(rdmaMeta.header.rdmaExtendHeaderBuf, rdmaMeta.header.bth.trans);
    let isZeroPayload = reth.dlen == 0;
    rdmaMeta.hasPayload = rdmaMeta.hasPayload && !isZeroPayload;

    rdmaPacketMetaPipeOutQ.enq(rdmaMeta);

    // ... 其他代码 ...
endrule
```

## 修复位置

文件：`open-rdma-rtl/src/EthernetFrameIO256.bsv`

- **原位置**：`handleSecondBeat` 规则（约第 485-533 行）
- **新位置**：`handleThirdBeat` 规则（约第 535-570 行）

具体变更：
- 第 549-551 行：添加 RETH 提取和零负载检查

## 影响

此修复确保：

1. **数据完整性**：在提取 RETH 之前，`rdmaExtendHeaderBuf` 包含了第二和第三个 beat 的所有扩展头数据
2. **正确解析**：RETH 的所有字段（va, rkey, dlen）都能被正确提取
3. **零负载检测**：能正确识别 `dlen == 0` 的情况，避免错误地等待不存在的负载数据

## 相关代码位置

```bsv
// 第二个 beat 中的扩展头片段大小（位）
typedef Bit#(TSub#(BTH_FIRST_BIT_ONE_BASED_INDEX_IN_SECOND_BEAT, SizeOf#(BTH)))
    RdmaExtendHeaderFragmentInSecondBeat;

// BTH_FIRST_BIT_ONE_BASED_INDEX_IN_SECOND_BEAT = 176 bits (22 bytes)
// SizeOf#(BTH) = 96 bits (12 bytes)
// 因此片段大小 = 80 bits (10 bytes)
```

## 测试验证

修复后应验证：
- RDMA WRITE/READ 操作能正确解析 RETH
- 零长度负载的包能正确处理（不会卡在等待负载数据）
- 包含负载的包能正常接收

## 日期

2026-01-19