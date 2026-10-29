package CommonTypes;

import Connectable :: *;
import FIFOF :: *;

interface PipeIn#(type t);
    method Action enq(t value);
    method Bit#(1) notFull;
endinterface

interface PipeOut#(type t);
    method t first;
    method Action deq;
    method Bit#(1) notEmpty;
endinterface

interface ServerP#(type reqT, type respT);
    interface PipeIn#(reqT) request;
    interface PipeOut#(respT) response;
endinterface

interface ClientP#(type reqT, type respT);
    interface PipeOut#(reqT) request;
    interface PipeIn#(respT) response;
endinterface

function PipeIn#(t) toPipeIn(FIFOF#(t) fifo);
    return (interface PipeIn;
        method Action enq(t value) if (fifo.notFull);
            fifo.enq(value);
        endmethod
        method Bit#(1) notFull = pack(fifo.notFull);
    endinterface);
endfunction

function PipeOut#(t) toPipeOut(FIFOF#(t) fifo);
    return (interface PipeOut;
        method t first if (fifo.notEmpty);
            return fifo.first;
        endmethod
        method Action deq if (fifo.notEmpty);
            fifo.deq;
        endmethod
        method Bit#(1) notEmpty = pack(fifo.notEmpty);
    endinterface);
endfunction

function ServerP#(reqT, respT) toServerP(
    PipeIn#(reqT) requestIfc,
    PipeOut#(respT) responseIfc
);
    return (interface ServerP;
        interface request = requestIfc;
        interface response = responseIfc;
    endinterface);
endfunction

function ClientP#(reqT, respT) toClientP(
    PipeOut#(reqT) requestIfc,
    PipeIn#(respT) responseIfc
);
    return (interface ClientP;
        interface request = requestIfc;
        interface response = responseIfc;
    endinterface);
endfunction

instance Connectable#(PipeOut#(t), PipeIn#(t));
    module mkConnection#(PipeOut#(t) source, PipeIn#(t) sink)(Empty);
        rule connectPipe;
            sink.enq(source.first);
            source.deq;
        endrule
    endmodule
endinstance

instance Connectable#(ClientP#(reqT, respT), ServerP#(reqT, respT));
    module mkConnection#(
        ClientP#(reqT, respT) client,
        ServerP#(reqT, respT) server
    )(Empty);
        mkConnection(client.request, server.request);
        mkConnection(server.response, client.response);
    endmodule
endinstance

endpackage
