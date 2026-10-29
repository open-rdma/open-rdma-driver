package DmaPayloadCompactor;

import Assert :: *;
import Vector :: *;

import CommonTypes :: *;
import DmaTypes :: *;
import TxTypes :: *;

// Compactor 位于 DMA Engine 与 Phase-A M2 之间。DMA raw beat保留
// 128B memory-window lane，keep可以non-prefix；M2要求从byte0开始、
// non-EOP满128B的canonical MessagePayloadBeat。本模块完成二者转换。
//
// 不变量：cycle boundary上的residual少于128B，因此加上一个raw beat后
// 最多255B，2048-bit working window足够容纳全部待处理数据。

interface DmaPayloadCompactorIfc;
    interface PipeIn#(DmaPayloadBeat) payloadIn;
    interface PipeOut#(MessagePayloadBeat) payloadOut;
endinterface

(* synthesize *)
module mkDmaPayloadCompactor(DmaPayloadCompactorIfc);
    // 输入和输出各使用 2-entry register queue。Wire 统一提交允许同拍
    // enq/deq，避免普通 FIFOF method conflict 把稳态吞吐降到半速。
    Reg#(DmaPayloadBeat) rawHead <- mkReg(unpack(0));
    Reg#(DmaPayloadBeat) rawTail <- mkReg(unpack(0));
    Reg#(Bool) rawHeadValid <- mkReg(False);
    Reg#(Bool) rawTailValid <- mkReg(False);
    Wire#(Maybe#(DmaPayloadBeat)) rawEnqReq <- mkDWire(tagged Invalid);
    Wire#(Bool) rawDeqReq <- mkDWire(False);

    Reg#(MessagePayloadBeat) outHead <- mkReg(unpack(0));
    Reg#(MessagePayloadBeat) outTail <- mkReg(unpack(0));
    Reg#(Bool) outHeadValid <- mkReg(False);
    Reg#(Bool) outTailValid <- mkReg(False);
    Wire#(Maybe#(MessagePayloadBeat)) outEnqReq <- mkDWire(tagged Invalid);
    Wire#(Bool) outDeqReq <- mkDWire(False);

    // raw queue统一提交producer enq和compactor deq。steady state只有head有效时，
    // 同拍deq+enq会用新beat直接替换head，不产生结构性气泡。
    rule commitRawQueue;
        if (rawDeqReq) begin
            case (rawEnqReq) matches
                tagged Valid .value: begin
                    if (rawTailValid) begin
                        rawHead <= rawTail;
                        rawHeadValid <= True;
                        rawTail <= value;
                        rawTailValid <= True;
                    end else begin
                        rawHead <= value;
                        rawHeadValid <= True;
                        rawTailValid <= False;
                    end
                end
                tagged Invalid: begin
                    rawHead <= rawTail;
                    rawHeadValid <= rawTailValid;
                    rawTailValid <= False;
                end
            endcase
        end else begin
            case (rawEnqReq) matches
                tagged Valid .value: begin
                    if (rawHeadValid) begin
                        rawTail <= value;
                        rawTailValid <= True;
                    end else begin
                        rawHead <= value;
                        rawHeadValid <= True;
                    end
                end
            endcase
        end
    endrule

    // output queue使用同一elastic commit模式，使M2消费当前beat时，本模块
    // 可以同拍发布下一beat并保持持续吞吐。
    rule commitOutputQueue;
        if (outDeqReq) begin
            case (outEnqReq) matches
                tagged Valid .value: begin
                    if (outTailValid) begin
                        outHead <= outTail;
                        outHeadValid <= True;
                        outTail <= value;
                        outTailValid <= True;
                    end else begin
                        outHead <= value;
                        outHeadValid <= True;
                        outTailValid <= False;
                    end
                end
                tagged Invalid: begin
                    outHead <= outTail;
                    outHeadValid <= outTailValid;
                    outTailValid <= False;
                end
            endcase
        end else begin
            case (outEnqReq) matches
                tagged Valid .value: begin
                    if (outHeadValid) begin
                        outTail <= value;
                        outTailValid <= True;
                    end else begin
                        outHead <= value;
                        outHeadValid <= True;
                    end
                end
            endcase
        end
    endrule

    // residual 始终低位对齐，cycle boundary 上保持少于 128 bytes。
    Reg#(Bit#(2048)) residual <- mkReg(0);
    Reg#(Bit#(8)) residualBytes <- mkReg(0);

    // currentTag/nextRawOffset校验输入仍属于同一message且没有逻辑byte gap；
    // outputOffset独立记录已经发布给M2的canonical byte数。
    Reg#(DmaWqeTag) currentTag <- mkReg(0);
    Reg#(DmaMessageLength) nextRawOffset <- mkReg(0);
    Reg#(DmaMessageLength) outputOffset <- mkReg(0);
    Reg#(Bool) inMessage <- mkReg(False);
    // endPending表示final raw beat已经被消费，但一次full输出后仍有不足128B
    // residual，需要在没有新输入的情况下主动生成最后一个EOP tail。
    Reg#(Bool) endPending <- mkReg(False);

    // 组合判断当前raw beat与residual合并后是否应产生输出；只有需要输出时
    // 才把output queue空间加入rule guard，否则短beat可以继续累计。
    Bit#(8) headValidBytes = dmaKeepCount(rawHead.keep);
    Bit#(9) suppliedBytes = zeroExtend(residualBytes) +
        zeroExtend(headValidBytes);
    Bool headNeedsOutput = suppliedBytes >= 9'd128 || rawHead.last == 1;

    // 处理步骤：按keep找到raw lane起点 -> 右移压紧 -> append到residual ->
    // 输出一个full beat、一个final partial beat，或仅更新residual。
    rule processRawBeat (rawHeadValid && !endPending &&
                         (!headNeedsOutput || !outTailValid));
        let raw = rawHead;
        Bit#(8) laneOffset = dmaKeepOffset(raw.keep);
        Bit#(8) validBytes = dmaKeepCount(raw.keep);
        Bit#(11) laneShift = 8 * zeroExtend(laneOffset);
        Bit#(11) validShift = 8 * zeroExtend(validBytes);
        Bit#(1025) validMaskWide = (1025'h1 << validShift) - 1025'h1;
        Bit#(1024) compacted = (raw.data >> laneShift) &
            truncate(validMaskWide);
        Bit#(11) appendShift = 8 * zeroExtend(residualBytes);
        Bit#(2048) combined = residual |
            (zeroExtend(compacted) << appendShift);
        Bit#(9) totalBytes = zeroExtend(residualBytes) +
            zeroExtend(validBytes);
        Bit#(33) nextRawWide = zeroExtend(raw.messageOffset) +
            zeroExtend(validBytes);

        dynamicAssert(raw.keep != 0, "DMA_COMPACTOR_ZERO_KEEP");
        dynamicAssert(dmaKeepIsContiguous(raw.keep),
            "DMA_COMPACTOR_NONCONTIGUOUS_KEEP");
        dynamicAssert(nextRawWide[32] == 0, "DMA_COMPACTOR_OFFSET_OVERFLOW");
        dynamicAssert(totalBytes <= 9'd255, "DMA_COMPACTOR_BUFFER_OVERFLOW");

        if (!inMessage) begin
            dynamicAssert(raw.first == 1, "DMA_COMPACTOR_MISSING_SOP");
            dynamicAssert(raw.messageOffset == 0, "DMA_COMPACTOR_FIRST_OFFSET");
        end else begin
            dynamicAssert(raw.first == 0, "DMA_COMPACTOR_DUPLICATE_SOP");
            dynamicAssert(raw.wqeTag == currentTag, "DMA_COMPACTOR_TAG_CHANGED");
            dynamicAssert(raw.messageOffset == nextRawOffset,
                "DMA_COMPACTOR_RAW_OFFSET");
        end

        rawDeqReq <= True;
        // 供应达到128B时优先发布full beat。若这也是message精确结尾，则
        // 该full beat同时携带EOP；否则保存剩余byte。
        if (totalBytes >= 9'd128) begin
            Bit#(33) nextOutputWide = zeroExtend(outputOffset) + 33'd128;
            dynamicAssert(nextOutputWide[32] == 0,
                "DMA_COMPACTOR_OUTPUT_OFFSET_OVERFLOW");
            dynamicAssert(outputOffset[31:25] == 0,
                "DMA_COMPACTOR_PHASEA_OFFSET_OVERFLOW");
            Bool completesNow = raw.last == 1 && totalBytes == 9'd128;
            outEnqReq <= tagged Valid MessagePayloadBeat {
                requestId: raw.wqeTag,
                messageOffset: truncate(outputOffset),
                data: truncate(combined),
                keep: '1,
                messageSop: pack(outputOffset == 0),
                messageEop: pack(completesNow)
            };

            Bit#(9) remaining = totalBytes - 9'd128;
            if (completesNow) begin
                residual <= 0;
                residualBytes <= 0;
                currentTag <= 0;
                nextRawOffset <= 0;
                outputOffset <= 0;
                inMessage <= False;
                endPending <= False;
            end else begin
                residual <= combined >> 1024;
                residualBytes <= truncate(remaining);
                outputOffset <= truncate(nextOutputWide);
                nextRawOffset <= truncate(nextRawWide);
                currentTag <= raw.wqeTag;
                inMessage <= True;
                endPending <= raw.last == 1;
            end
        end else if (raw.last == 1) begin
            // final供应不足128B时直接生成prefix-keep EOP，不等待下一message补齐。
            dynamicAssert(totalBytes != 0, "DMA_COMPACTOR_EMPTY_EOP");
            dynamicAssert(outputOffset[31:25] == 0,
                "DMA_COMPACTOR_PHASEA_OFFSET_OVERFLOW");
            Bit#(8) outputBytes = truncate(totalBytes);
            Bit#(11) outputShift = 8 * zeroExtend(outputBytes);
            Bit#(1025) outputMaskWide = (1025'h1 << outputShift) - 1025'h1;
            outEnqReq <= tagged Valid MessagePayloadBeat {
                requestId: raw.wqeTag,
                messageOffset: truncate(outputOffset),
                data: truncate(combined) & truncate(outputMaskWide),
                keep: dmaKeepMask(0, outputBytes),
                messageSop: pack(outputOffset == 0),
                messageEop: 1
            };
            residual <= 0;
            residualBytes <= 0;
            currentTag <= 0;
            nextRawOffset <= 0;
            outputOffset <= 0;
            inMessage <= False;
            endPending <= False;
        end else begin
            // 普通短beat只进入residual，直到后续raw beat将供应量补到128B。
            residual <= combined;
            residualBytes <= truncate(totalBytes);
            currentTag <= raw.wqeTag;
            nextRawOffset <= truncate(nextRawWide);
            inMessage <= True;
        end
    endrule

    // final raw beat 可能先产生一个 full beat，再留下不足 128B 的尾部。
    rule flushPendingTail (endPending && !outTailValid);
        dynamicAssert(residualBytes != 0, "DMA_COMPACTOR_EMPTY_PENDING_EOP");
        dynamicAssert(outputOffset[31:25] == 0,
            "DMA_COMPACTOR_PHASEA_OFFSET_OVERFLOW");
        Bit#(11) outputShift = 8 * zeroExtend(residualBytes);
        Bit#(2049) outputMaskWide = (2049'h1 << outputShift) - 2049'h1;
        outEnqReq <= tagged Valid MessagePayloadBeat {
            requestId: currentTag,
            messageOffset: truncate(outputOffset),
            data: truncate(residual & truncate(outputMaskWide)),
            keep: dmaKeepMask(0, residualBytes),
            messageSop: pack(outputOffset == 0),
            messageEop: 1
        };
        residual <= 0;
        residualBytes <= 0;
        currentTag <= 0;
        nextRawOffset <= 0;
        outputOffset <= 0;
        inMessage <= False;
        endPending <= False;
    endrule

    // notFull只反映2-entry raw queue容量；所有message顺序和keep合法性在
    // processRawBeat中检查，避免协议错误被output backpressure永久隐藏。
    interface PipeIn payloadIn;
        method Action enq(DmaPayloadBeat value) if (!rawTailValid);
            rawEnqReq <= tagged Valid value;
        endmethod
        method Bit#(1) notFull = pack(!rawTailValid);
    endinterface

    interface PipeOut payloadOut;
        method MessagePayloadBeat first if (outHeadValid);
            return outHead;
        endmethod
        method Action deq if (outHeadValid);
            outDeqReq <= True;
        endmethod
        method Bit#(1) notEmpty = pack(outHeadValid);
    endinterface
endmodule

endpackage
