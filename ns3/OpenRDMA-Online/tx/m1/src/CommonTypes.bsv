package CommonTypes;

import Connectable :: *;
import FIFOF :: *;

interface PipeIn#(type t);  // 输入端
    method Action enq(t value); // 将类型 t 的值入队
    method Bit#(1) notFull; // 这是反压的具体实现：notFull 为 1 代表队列未满，可以 enq；为 0 代表已满。Bit#(1) 直接对应硬件信号，便于综合和连接。
endinterface

interface PipeOut#(type t);  // 输出端
    method t first; // 获取队列中的第一个元素
    method Action deq; // 弹出队列中的第一个元素
    method Bit#(1) notEmpty;
endinterface
/* 这块存在重复定义的问题
interface ServerP#(type reqT, type respT);
    interface PipeIn#(reqT) request;
    interface PipeOut#(respT) response;
endinterface

interface ClientP#(type reqT, type respT);
    method Action deq;
    method Bit#(1) notEmpty;
endinterface
*/
interface ServerP#(type reqT, type respT);
    interface PipeIn#(reqT) request;
    interface PipeOut#(respT) response;
endinterface

interface ClientP#(type reqT, type respT);
    interface PipeOut#(reqT) request;   // 客户端发出请求的输出
    interface PipeIn#(respT) response;  // 客户端接收响应的输入
endinterface

function PipeIn#(t) toPipeIn(FIFOF#(t) fifo);   // 把 FIFOF 的入队侧包装成 PipeIn#(t)。函数名：toPipeIn；参数：FIFOF#(t) fifo；返回：PipeIn#(t)；类型参数 t 由调用时推断。
    return (interface PipeIn;   // interface 表达式构造一个类型为 PipeIn 的匿名接口实例
        method Action enq(t value) if (fifo.notFull);   // 方法 enq 的定义，value 的类型为 t。
            fifo.enq(value);    // 调用 FIFOF 的 enq 方法，将 value 入队
        endmethod
        method Bit#(1) notFull = pack(fifo.notFull);    // fifo.notFull 是 Bool 类型，pack 将其转换为 Bit#(1) 类型
    endinterface);
endfunction

function PipeOut#(t) toPipeOut(FIFOF#(t) fifo); // 把 FIFOF 的出队侧包装成 PipeOut#(t)
    return (interface PipeOut;
        method t first if (fifo.notEmpty);  // 这是反压的具体实现
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
);    // 把 PipeIn#(reqT) 和 PipeOut#(respT) 转化成 ServerP#(reqT, respT)
    return (interface ServerP;  // 创建一个实现 ServerP 接口的匿名对象
        interface request = requestIfc; // 把 ServerP 的子接口 request 绑定到传入的 requestIfc。
        interface response = responseIfc; // 把 ServerP 的子接口 response 绑定到传入的 responseIfc。
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

instance Connectable#(PipeOut#(t), PipeIn#(t)); // 实例：PipeOut 连接到 PipeIn，实现流式数据传输
    module mkConnection#(PipeOut#(t) source, PipeIn#(t) sink)(Empty);   // // 类型类，匹配内部类型定义时才会触发：source 表示数据来源，sink 表示数据目的地；返回 Empty 表示该连接模块不对外暴露接口，只负责内部连线
        rule connectPipe;   // 每个周期执行，一个 rule 里的内容在同一周期执行
            sink.enq(source.first); // 在 source 能够提供数据的情况下，将 source 的第一个元素入队到 sink
            source.deq; // 从 source 弹出该数据
        endrule
    endmodule
endinstance

instance Connectable#(ClientP#(reqT, respT), ServerP#(reqT, respT));    // 实例：ClientP 连接到 ServerP，形成请求-响应通道
    module mkConnection#(
        ClientP#(reqT, respT) client,
        ServerP#(reqT, respT) server
    )(Empty);   // 类型类，匹配内部类型定义时才会触发。因此下方的 mkConnection 触发的不是这个 instance 而是上面那个 instance
        mkConnection(client.request, server.request);   // client.request 是 PipeOut#(reqT)，server.request 是 PipeIn#(reqT)，正是 Connectable#(PipeOut#(t), PipeIn#(t)) 的匹配类型，因此会自动实例化一个连接模块，把客户端请求传给服务端
        mkConnection(server.response, client.response); // 把服务端响应传回给客户端
    endmodule
endinstance

endpackage
