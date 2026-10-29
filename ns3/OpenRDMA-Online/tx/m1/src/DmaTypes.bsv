package DmaTypes;

typedef Bit#(3)  DmaSlot;
typedef Bit#(16) DmaTag;
typedef Bit#(16) DmaWqeTag;
typedef Bit#(16) DmaChunkSeq;
typedef Bit#(17) DmaChunkCount;
typedef Bit#(32) DmaMessageLength;
typedef Bit#(11) DmaBurstLength;

// 与 WqeDmaRead.DmaReq 的字段顺序和位宽保持一致。
typedef struct {
    Bit#(64)    physAddr;
    Bit#(32)    len;
    DmaWqeTag   wqeTag;
    DmaChunkSeq chunkSeq;
    Bool        isFirst;
    Bool        isLast;
} DmaChunkReq deriving(Bits, Eq, FShow);

typedef struct {
    DmaTag          tag;
    DmaWqeTag       wqeTag;
    DmaChunkSeq     chunkSeq;
    Bit#(64)        physAddr;
    DmaBurstLength  len;
    DmaMessageLength messageOffset;
    Bit#(1)         isFirst;
    Bit#(1)         isLast;
} DmaReadRequest deriving(Bits, Eq, FShow);

typedef struct {
    DmaTag       tag;
    Bit#(64)     windowAddr;
    Bit#(1024)   data;
    Bit#(128)    keep;
    Bit#(1)      burstLast;
} DmaCompletionBeat deriving(Bits, Eq, FShow);

typedef struct {
    DmaWqeTag       wqeTag;
    DmaMessageLength messageOffset;
    Bit#(1024)      data;
    Bit#(128)       keep;
    Bit#(1)         first;
    Bit#(1)         last;
} DmaPayloadBeat deriving(Bits, Eq, FShow);

typedef struct {
    DmaWqeTag       wqeTag;
    DmaMessageLength length;
    DmaChunkCount   chunkCount;
} DmaMessageDone deriving(Bits, Eq, FShow);

function Maybe#(DmaSlot) dmaFindFreeSlot(Bit#(8) freeMask, DmaSlot cursor);
    Maybe#(DmaSlot) result = tagged Invalid;
    for (Integer offset = 0; offset < 8; offset = offset + 1) begin
        DmaSlot index = cursor + fromInteger(offset);
        if (!isValid(result) && freeMask[index] == 1)
            result = tagged Valid index;
    end
    return result;
endfunction

function Bit#(8) dmaKeepOffset(Bit#(128) keep);
    return pack(countZerosLSB(keep));
endfunction

function Bit#(8) dmaKeepCount(Bit#(128) keep);
    return pack(countOnes(keep));
endfunction

function Bool dmaKeepIsContiguous(Bit#(128) keep);
    Bit#(8) offset = dmaKeepOffset(keep);
    Bit#(128) shifted = keep >> offset;
    return keep != 0 && ((shifted & (shifted + 1)) == 0);
endfunction

function Bit#(128) dmaKeepMask(Bit#(8) offset, Bit#(8) count);
    Bit#(129) ones = (129'h1 << count) - 129'h1;
    return truncate(ones << offset);
endfunction

endpackage
