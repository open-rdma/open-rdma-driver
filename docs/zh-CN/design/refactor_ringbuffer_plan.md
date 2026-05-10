# ringbuffer 重构结论

## 结论

`producer` 和 `consumer` 对外应操作“能转换为 desc 的逻辑元素”，而不是直接操作单个 `desc`。

底层 DMA buffer 仍然按 `desc` 存储和推进指针；上层接口暴露 `element`。

## 原因

1. `desc` 是硬件传输单位，不一定是软件语义单位。
2. 有些元素天然对应多个 `desc`，如果接口强制单 `desc`，抽象会泄漏到业务层。
3. 现在 `consumer` 已经在做“1 或 2 个 desc 组装成一个元素”，`producer` 也应沿同一抽象设计。
4. 这样更容易支持以后出现的变长元素，而不是把多段拼接逻辑散落在调用方。

## 建议分层

1. `DescRing`
   只负责按槽位读写 `desc`，维护 head/tail 和内存屏障。
2. `Producer`
   接收一个 `element`，把它编码成 1 个或多个 `desc`，然后一次提交。
3. `Consumer`
   从当前 tail 开始查看 `desc`，判断一个完整元素占几个 `desc`，完整后再解码返回。

## 设计原则

1. `producer` 不做内部缓冲。
   编码开销通常可接受；要么整元素写入并提交，要么不写。
2. `consumer` 可以有很薄的一层“预读/组装”逻辑。
   因为它本来就要判断元素是否完整，适合在这里处理多 `desc` 场景。
3. 不要把“最多两个 desc”写死在通用接口里。
   两个 `desc` 只是当前实现细节，不应成为抽象边界。

## trait 方向

目标不是 `to_desc() -> Desc`，而是“编码/解码一个 desc 序列”。

- 编码侧：`element -> [desc]`
- 解码侧：`[desc] -> element`
- 同时需要一个“这个元素需要多少个 desc”或“从首 desc 判断还需要几个 desc”的能力

这样 producer/consumer 的抽象才是对称的。

## 当前实现启示

当前 `ToRingBytes` 仍偏向“单 desc”。

当前 `FromRingBytes` 已经是“多个 desc 还原一个元素”。

所以重构方向应是：把 producer 也提升到“element <-> desc 序列”这一层，而不是把 consumer 降回单 `desc`。

## 这样设计的问题

### producer

producer 的写入接口应该是怎么样的？由于每个 element 的长度并不确定，所以这个接口感觉不太好写

#### ToRingBytes

ToRingBytes 提供的接口应该长什么样？

当前先采用 `encode_to_slice` 方案。

目标是让 `ToRingBytes` 只负责“一个 element 如何编码成 desc 序列”，而不负责“这些 desc 存放在哪种容器里”。

建议接口形状：

```rust
trait ToRingBytes {
    type Bytes: Copy;

    const MAX_DESC_COUNT: usize;

    fn desc_count(&self) -> usize;

    fn encode_to_slice(&self, out: &mut [Self::Bytes]);
}
```

这个接口的语义是：

1. `desc_count()` 返回当前 element 实际需要的 `desc` 数量。
2. `encode_to_slice()` 只负责把编码结果写入调用方提供的缓冲区前缀。
3. `ToRingBytes` 不绑定 `Vec`、数组还是其他 writer，缓冲策略由 `ProducerRing` 决定。

采用这个方案的原因：

1. 不会像 `fn encode() -> Vec<_>` 一样把分配策略绑定到 trait 上。
2. 不会像返回 `[desc; MAX_DESC_COUNT]` 一样引入 generic const expression 和额外长度字段的问题。
3. 比 writer 风格更简单，当前阶段更容易落地。

调用约定：

1. `ProducerRing` 先调用 `desc_count()`，判断 ring 剩余槽位是否足够。
2. `ProducerRing` 再准备好 scratch buffer，并把对应长度的切片传给 `encode_to_slice()`。
3. `encode_to_slice()` 只写入前 `desc_count()` 个位置，不负责提交 ring。

约束与断言：

1. trait 契约应保证 `desc_count() <= MAX_DESC_COUNT`。
2. `ProducerRing` 在调用前负责主检查，并按返回长度准备输出切片。
3. `encode_to_slice()` 内部可以保留防御性 `assert`，例如检查 `out.len() >= desc_count()`。

为什么不先编码到中间缓冲区、再决定是否写 ring：

1. 这样虽然能绕开接口上的长度判断，但会引入一次额外的数据落地和拷贝。
2. 它优化掉的只是一次很便宜的长度比较，却放大了状态管理复杂度。
3. 对当前场景来说，先 `desc_count()` 再判 ring 空间通常更直接，也更便宜。

额外拷贝的成本：

1. `encode_to_slice()` 会让数据先写入 scratch buffer，再从 scratch buffer 写入 ring，因此确实比“直接写 ring”多一次拷贝。
2. 这部分成本通常按 `desc` 数量线性增长，但在当前场景里一般仍是小常数。
3. 如果 `desc` 很小且数量也很少，例如 1 到 2 个 32B `desc`，额外拷贝的成本通常大约是几个到几十个 CPU cycle，常见大致可理解为几 ns 到十几 ns。
4. 即使到 4 到 8 个 `desc`，通常也仍然只是几十 ns 量级，很难超过一次 CSR 访问或一次较差的 cache miss。

当前判断：

1. `encode_to_slice()` 作为第一版接口是可接受的折中，优先解决抽象边界和稳定实现问题。
2. 如果后续 profiling 证明 producer 编码路径已经成为热点，再考虑把接口进一步演进为“直接写目标槽位”或 writer 风格，以去掉这次额外拷贝。

### consumer

consumer 可能需要一段临时缓冲区，用来组装一个逻辑元素对应的多个 `desc`。

这段缓冲区的大小不应按 ring 大小决定，而应按“单个元素最多占多少个 `desc`”决定。

不建议的方案：

1. 每次新建 `Vec`
   热路径上会引入重复分配，开销不稳定。
2. 缓冲区长度直接取 ring 长度
   这会把“组装一个元素”的问题放大成“缓存整个 ring”的问题，既浪费空间，也容易掩盖协议设计问题。

推荐方案：

1. 在 trait 中显式给出单元素的最大 `desc` 数量，例如 `MAX_DESC_COUNT`。
2. `consumer` 只保留一个可复用的临时缓冲区，容量就是这个上界。
3. 读取时先看首 `desc`，判断该元素总共需要多少个 `desc`；只有完整时才解码。

这样做的好处：

1. 大小有明确语义，和协议绑定，不靠猜测。
2. 不需要频繁分配。
3. 不会把“当前最多 2 个 desc”的实现细节写死到通用抽象里。

关于栈上还是堆上：

1. 如果 `MAX_DESC_COUNT` 很小，例如 2、4、8，优先放栈上，最简单，局部性也更好。
2. 如果上界偏大，或者类型很多导致栈上数组不方便管理，可以放在 `ConsumerRing` 内部复用一块堆内存。
3. 只有在确实需要“偶尔超过小上界”的场景下，才考虑 `SmallVec` 一类的小缓冲优化。

#### 当前实现选择

当前决定采用 `Vec`，但不是每次临时创建，而是在 `ConsumerRing` 内部预分配并复用。

原因：

1. `MAX_DESC_COUNT` 很适合表达为 `FromRingBytes` 的关联常量。
2. 但在 stable Rust 上，很难把这个关联常量直接用到 `ConsumerRing` 的静态数组类型里。
3. 复用 `Vec` 可以保留这层抽象，同时避免 generic const expression 的限制。

实现原则：

1. `ConsumerRing::new()` 时按 `MAX_DESC_COUNT` 预分配容量。
2. 热路径中只复用缓冲区，不重复申请堆内存。
3. `MAX_DESC_COUNT` 仍然作为协议上界使用，但只在值层使用，不进入类型层。

#### Vec 的额外开销

相对栈上静态数组，预分配并复用 `Vec` 仍有一些额外开销，但都属于小常数开销：

1. 多一次指针间接访问。
2. 数据局部性略差。
3. 首次触碰缓冲区时，可能多一次 cache miss。
4. 维护 `ptr/len/cap` 会带来少量元数据和分支开销。
5. 编译器较难做“固定长度数组”那种极致优化。

这些开销的大致量级通常是：

1. 指针访问、长度维护、边界检查：几个到十几个 CPU cycle。
2. 偶发 cache miss：几十到上百个 cycle。
3. 首次堆分配：几十到几百 ns，但只发生在初始化阶段，不在热路径上。

因此，只要 `Vec` 是预分配并复用的，它相比静态数组的损失通常不会成为主要瓶颈。对当前 ringbuffer 而言，更大的成本通常来自 CSR 访问、内存屏障以及 DMA buffer 本身的 cache 行行为。

#### 小结

`consumer` 可以有缓冲区，但它应当是“单元素级别”的小缓冲区，大小由协议定义的 `MAX_DESC_COUNT` 决定，而不是拍一个很大的值，更不是直接等于 ring 长度。

当前实现上，优先选择“预分配并复用 `Vec`”；如果后续 profiling 证明这段路径确实极热，再考虑收紧到静态数组或 `SmallVec`。

## ProducerRing 发送语义

在当前设计约束下，`desc` 应当在 `ProducerRing` 这一层被隐藏掉。

这意味着：

1. 调用者不应直接感知 `desc`。
2. 调用者不应负责计算或传入“需要几个 `desc`”。
3. `ProducerRing` 不应把“按 `desc` reserve / write / commit”暴露成公共主接口。

### 核心目标

`ProducerRing` 的成功返回必须表示：某个 `element` 已经被编码成对应的 `desc` 序列，写入底层 DMA ring，并通过更新 head / doorbell 对硬件可见。

不接受下面这种语义：

1. `push()` 返回成功，但数据其实只是进入了 `ProducerRing` 内部缓冲区。
2. 数据是否真正发出，还要等后续某次 `flush()`。
3. ring 满时，`ProducerRing` 悄悄把 element 暂存在内部，等待以后再发。

换句话说，`ProducerRing` 应当是“同步发布器”，而不是“带内部待发队列的 writer”。

### 谁维护缓冲队列

如果上层需要排队、攒批、重试或调度，应由调用者维护 `element` 队列，而不是由 `ProducerRing` 维护跨调用的 staging / pending 队列。

原因：

1. 只有调用者知道哪些 element 可以延后，哪些必须立刻发送。
2. 只有调用者知道 ring 满时应该等待、重试、丢弃还是重新合批。
3. 如果 `ProducerRing` 内部维护待发队列，调用者将无法仅凭返回值判断数据是否真的已经发布。

因此，更合理的职责划分是：

1. `ProducerRing` 只负责“当前能不能把这个 element 真正发出去”。
2. 调用者负责“发不出去时怎么办”。

### 对 staging 的约束

这里需要区分两种完全不同的东西：

1. 单次调用内部的临时 scratch buffer。
2. 跨调用保存未发送 element 的 staging / pending 队列。

当前设计不接受第 2 种，但可以接受第 1 种。

也就是说：

1. `ProducerRing` 不应跨调用保存“尚未发布”的 element。
2. `ProducerRing` 可以在一次 `try_push()` 调用内部，短暂使用 scratch buffer 完成编码。
3. 只要该 scratch 不跨调用保存未发送数据，它就不构成内部待发缓冲区。

### 推荐接口语义

在这个约束下，`reserve(n) -> writer/view -> commit(n)` 不适合作为 `ProducerRing` 的公共主接口，因为它天然容易把抽象重心拉回 `desc` 这一层。

更适合作为公共接口的是下面两类：

#### 单元素发送

```rust
fn try_push(&mut self, elem: &Spec::Element) -> io::Result<bool>;
```

语义：

1. `Ok(true)`：该 `element` 已经完整写入 ring，并对硬件可见。
2. `Ok(false)`：当前空间不足，该 `element` 完全未发送。
3. `Err(e)`：发生设备或 CSR 错误。

#### 批量原子发送

```rust
fn try_push_batch_atomic(&mut self, elems: &[Spec::Element]) -> io::Result<bool>;
```

语义：

1. `Ok(true)`：整批 `element` 已全部发送。
2. `Ok(false)`：空间不足，整批一个都没发送。
3. `Err(e)`：发生设备或 CSR 错误。

如果后续确实需要“尽量多发一部分”的接口，也可以增加：

```rust
fn try_push_batch(&mut self, elems: &[Spec::Element]) -> io::Result<usize>;
```

它的语义应明确为：

1. 返回已成功发送的前缀元素数量 `n`。
2. `elems[..n]` 已发送。
3. `elems[n..]` 完全未发送。

### ProducerRing 内部发送流程

以 `try_push()` 为例，推荐流程如下：

1. 调用 `elem.desc_count()`，得到当前 element 实际需要的 `desc` 数量。
2. 检查该数量是否满足 trait 契约，例如 `desc_count() <= MAX_DESC_COUNT`。
3. 检查 ring 当前剩余空间是否足够容纳该 element 对应的全部 `desc`。
4. 如果空间不足，直接返回 `Ok(false)`，且不产生任何部分写入。
5. 准备单次调用内部使用的 scratch buffer。
6. 调用 `encode_to_slice()`，把 element 编码到 scratch buffer。
7. 将 scratch buffer 中的 `desc` 顺序写入 DMA ring。
8. 执行 release fence，确保 `desc` 写入先于 head 更新对硬件可见。
9. 推进 head 并写 CSR / doorbell。
10. 返回 `Ok(true)`。

这里的关键点不是“有没有用 scratch”，而是“成功返回时，数据必须已经真正发布”。

### 为什么不把 `reserve` 作为公共接口

如果公共接口暴露 `reserve(n)`，哪怕名字叫 `ProducerRing::reserve()`，调用者也仍然会被迫理解下面这些问题：

1. `n` 的单位到底是 `element` 还是 `desc`。
2. 一个 element 到底占几个 `desc`。
3. 什么时候该 reserve 多大。

这会把本应隐藏在 `ProducerRing` 内部的协议细节重新泄漏到上层。

因此，在当前目标下：

1. `reserve` 可以作为 `ProducerRing` 内部实现技巧存在。
2. 但它不应成为 `ProducerRing` 对外的主接口模型。
3. 对外语义应围绕“是否已经真正发送某个 element”来设计。

### 反压语义

本方案中，反压由 `ProducerRing` 显式向上暴露，而不是在内部消化。

具体表现为：

1. ring 空间不足时，`try_push()` 返回 `Ok(false)`。
2. 调用者自己决定该 element 是进入本地 pending queue、稍后重试、直接丢弃，还是与后续 element 重新合批。
3. `ProducerRing` 不替调用者做调度策略决策。

这样可以保持职责边界清晰：

1. `ProducerRing` 只负责“当前能不能发”。
2. 调用者负责“发不出去时怎么办”。

### 小结

当前关于 producer 的设计结论是：

1. `desc` 在 `ProducerRing` 这一层被抽象隐藏掉。
2. `ProducerRing` 不维护跨调用的 staging / pending 队列。
3. 调用者维护 `element` 队列，而不是 `desc` 缓冲区。
4. `ProducerRing` 成功返回必须表示数据已经真正发布到硬件可见的 ring 中。
5. 因此，`ProducerRing` 的公共接口应优先围绕 `try_push()` / `try_push_batch_atomic()` 这类“同步发布”语义来设计，而不是围绕 `reserve(n)` 来设计。

## ConsumerRing 多 desc element 缓冲区

当前 `ConsumerRing` 已经隐含支持“一个 element 由多个 `desc` 组成”的场景，但实现仍然是写死的“最多两个 `desc`”。

现状大致是：

1. 先读取 `tail` 指向的第一个 `desc`。
2. 如果首 `desc` 无效，则返回 `None`。
3. 如果首 `desc` 表示还需要下一个 `desc`，则继续检查第二个。
4. 两个都有效后，再一次性消费并解码。

这种实现对当前协议是够用的，但它把“最多两个 `desc`”写进了 `ConsumerRing` 的控制流里，不利于后续扩展到真正通用的多 `desc` element。

### 目标

`ConsumerRing` 应显式持有一个“单元素级别”的可复用缓冲区，用来组装当前正在消费的多 `desc` element。

这个缓冲区的目标不是缓存整个 ring，而是：

1. 缓存当前 element 对应的 `desc` 序列。
2. 支持 `1..N` 个 `desc` 组成一个 element。
3. 在确认 element 完整之前，不推进 tail。
4. 在确认 element 完整之后，再一次性读出、解码并提交 tail。

### 为什么需要显式缓冲区

如果继续沿用当前“最多两个 `desc`”的分支写法，会有几个问题：

1. element 长度信息被写死在 `ConsumerRing` 控制流里，而不是由协议 trait 提供。
2. 未来扩展到 `3` 个或更多 `desc` 时，`try_pop()` 会不断长出新的分支。
3. 解码逻辑和 ring 控制逻辑耦合过深，不利于维护。
4. 很难把“先判断完整，再统一消费”的模式写成通用流程。

增加一个单元素级别的缓冲区后，consumer 的职责会更清晰：

1. 先探测当前 element 总共需要多少个 `desc`。
2. 逐个确认这些 `desc` 是否都已经 ready。
3. 完整后再一次性读入缓冲区并解码。

### 缓冲区应该放在哪里

这段缓冲区应放在 `ConsumerRing` 内部，由 `ConsumerRing` 持有并复用。

推荐形态：

```rust
scratch: Vec<<Spec::Element as FromRingBytes>::Bytes>
```

原因：

1. 缓冲区是 consumer 读取协议的一部分，不应泄漏到外部调用方。
2. 复用 `Vec` 可以避免热路径反复分配。
3. 当前 stable Rust 下，用 trait 关联常量直接驱动静态数组类型并不方便。

### 缓冲区大小应该如何确定

缓冲区容量不应按 ring 大小决定，而应按“单个 element 最多占多少个 `desc`”决定。

也就是说，应当由协议 trait 提供类似：

```rust
const MAX_DESC_COUNT: usize;
```

`ConsumerRing::new()` 时按这个上界预分配容量。

这样做的好处：

1. 空间大小和协议语义直接绑定。
2. 不会把“缓存一个 element”的问题错误放大成“缓存整个 ring”的问题。
3. 热路径只做 `clear()` 和复用，不做重复堆分配。

### trait 需要补充什么能力

当前 `FromRingBytes` 只有：

1. `from_bytes(bytes: &[Self::Bytes])`
2. `is_valid(bytes: &Self::Bytes)`
3. `has_next(bytes: &Self::Bytes)`（默认返回 `false`）

这只足够表达“1 个 `desc`”或“最多再跟 1 个 `desc`”。

为了支持真正通用的多 `desc` element，建议把 trait 能力提升为：

1. `MAX_DESC_COUNT`
2. `desc_count(first: &Self::Bytes) -> usize`

例如：

```rust
trait FromRingBytes: Sized {
    type Bytes: Copy;

    const MAX_DESC_COUNT: usize;

    fn from_bytes(bytes: &[Self::Bytes]) -> Option<Self>;

    fn is_valid(bytes: &Self::Bytes) -> bool;

    fn desc_count(first: &Self::Bytes) -> usize;
}
```

如果当前阶段不想一次把接口改太大，也可以先保留 `has_next()`，并给出一个过渡性的默认 `desc_count()`：

```rust
fn desc_count(first: &Self::Bytes) -> usize {
    if Self::has_next(first) { 2 } else { 1 }
}
```

这样可以先兼容现有实现，再逐步演进。

### try_pop 的推荐流程

引入缓冲区后，`try_pop()` 的推荐流程应调整为：

1. 读取 `tail` 指向的首 `desc`。
2. 如果首 `desc` 无效，直接返回 `None`。
3. 根据首 `desc` 计算当前 element 总共需要多少个 `desc`。
4. 检查该数量是否满足协议约束，例如 `desc_count <= MAX_DESC_COUNT`。
5. 在不推进 tail 的前提下，逐个探测后续 `desc` 是否都已经 valid。
6. 若其中任一 `desc` 尚未 ready，则返回 `None`，并保持 tail 不动。
7. 只有当整个 element 都 ready 后，执行 `Acquire fence`。
8. 清空并复用 `scratch`。
9. 依次调用 `read_and_advance()`，把本 element 的所有 `desc` 读入 `scratch`。
10. 写回 tail CSR。
11. 调用 `Spec::Element::from_bytes(&scratch)` 完成解码。

这套流程的关键点是：

1. “探测 element 是否完整” 和 “真正消费 ring” 分成两个阶段。
2. 在确认完整之前，不推进 tail。
3. 只有在确认完整之后，才一次性消费当前 element 对应的全部 `desc`。

### 这种缓冲区不是“内部待发缓存”

这里的 `scratch` 只是 consumer 在一次 `try_pop()` 中组装当前 element 的临时缓冲区，它不是跨元素的缓存队列，也不是 ring 级别的 shadow copy。

它的语义应明确为：

1. 只服务于当前 element 的解码。
2. 每次 `try_pop()` 最多只组装一个 element。
3. 每次消费完成后复用，不保留历史 element。

因此，这段缓冲区不会改变 `ConsumerRing` 的整体语义，只是把现有“1 或 2 个 `desc`”的临时组装逻辑，提升为一个可扩展、可复用的通用机制。

### 小结

当前关于 consumer 多 `desc` 支持的设计结论是：

1. `ConsumerRing` 应内部持有一个单元素级别、可复用的 `scratch buffer`。
2. 该缓冲区容量应由协议给出的 `MAX_DESC_COUNT` 决定，而不是由 ring 大小决定。
3. `FromRingBytes` 应补充“一个 element 需要多少个 `desc`”的能力，而不是继续把“最多两个 `desc`”写死在 `ConsumerRing` 控制流里。
4. `try_pop()` 应改成“先判断完整，再统一消费并解码”的通用流程。
5. 这段缓冲区只是当前 element 的组装区，不是跨调用或跨元素的缓存队列。
