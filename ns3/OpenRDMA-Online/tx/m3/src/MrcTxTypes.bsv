package MrcTxTypes;

import TxTypes :: *;

typedef enum {
    EV_GOOD,
    EV_SKIP,
    EV_DENIED,
    EV_ASSUMED_BAD
} MrcEvState deriving(Bits, Eq, FShow);

typedef enum {
    EV_FB_ECN,
    EV_FB_SKIP_ONCE,
    EV_FB_TRIMMED,
    EV_FB_ALWAYS_SKIP,
    EV_FB_TIMEOUT,
    EV_FB_PROBE_GOOD,
    EV_FB_PROBE_SKIP,
    EV_FB_CLEAN
} MrcPathFeedbackKind deriving(Bits, Eq, FShow);

typedef struct {
    TxEvProfileId profileId;
    Bit#(3)       entryIndex;
    Bit#(1)       valid;
    Bit#(1)       adminEnable;
    Bit#(32)      entropy;
    Bit#(2)       channel;
} MrcEvEntryConfig deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(16) skipHoldCycles;
    Bit#(16) probeIntervalCycles;
} MrcEvPolicyConfig deriving(Bits, Eq, FShow);

typedef struct {
    Bit#(4) localReachable;
    Bit#(4) peerReachable;
} MrcPortStatusUpdate deriving(Bits, Eq, FShow);

// Per-traffic-class wire routing. M3 keeps this in registers so that rule
// guards never have to read a descriptor FIFO head: reading the head inside a
// guard makes BSC's rule-conflict analysis collapse the start rules. The
// descriptor's own wireProfile/evProfileId must agree with this configuration.
typedef struct {
    Bit#(4)       mrcMask;
    TxEvProfileId firstProfileId;
    TxEvProfileId retryProfileId;
    TxEvProfileId orderedProfileId;
    TxEvProfileId controlProfileId;
} MrcSourceProfileConfig deriving(Bits, Eq, FShow);

typedef struct {
    TxEvProfileId       profileId;
    Bit#(3)             entryIndex;
    Bit#(8)             generation;
    MrcPathFeedbackKind kind;
    Bit#(16)            probeId;
} MrcPathFeedback deriving(Bits, Eq, FShow);

typedef struct {
    TxEvProfileId profileId;
    Bit#(3)       entryIndex;
    Bit#(8)       generation;
    Bit#(32)      entropy;
    Bit#(2)       channel;
} MrcResolvedPath deriving(Bits, Eq, FShow);

typedef struct {
    TxEvProfileId profileId;
    Bit#(3)       entryIndex;
    Bit#(8)       generation;
    Bit#(32)      entropy;
    Bit#(2)       channel;
    Bit#(16)      probeId;
} MrcProbeRequest deriving(Bits, Eq, FShow);

typedef struct {
    TxTrafficClass trafficClass;
    TxEvProfileId  profileId;
} MrcNoPathEvent deriving(Bits, Eq, FShow);

typedef struct {
    TxRequestId   requestId;
    TxQpn         qpn;
    TxPsn         psn;
    TxPsnCount    packetIndex;
    Bit#(2)       channel;
    TxTrafficClass trafficClass;
    Bit#(1)       evValid;
    TxEvProfileId profileId;
    Bit#(3)       evIndex;
    Bit#(8)       evGeneration;
    Bit#(32)      entropy;
} TxEgressEvent deriving(Bits, Eq, FShow);

endpackage
