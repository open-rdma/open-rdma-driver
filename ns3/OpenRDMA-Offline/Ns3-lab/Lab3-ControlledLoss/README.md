# Lab 3：可控丢包与随机种子

这个实验在接收端 NetDevice 上安装 `RateErrorModel`。每个包按固定概率独立丢弃，测试会记录
全部缺失序号以及一个简短的 loss signature。

## 运行

```bash
make lab3
```

验证可复现性：

```bash
make lab3-repro
```

`lab3-repro` 用相同 seed/run 执行两次并比较 JSON；随后修改 run，确认丢包样本确实变化。

可以手动关闭丢包：

```bash
make run LAB=lab3 ARGS='--lossRate=0 --output=../results/lab3-zero.json'
```

## 观察重点

- `SetSeed` 决定基础随机流，`SetRun` 选择独立实验；
- 相同 seed/run 和相同模型配置应产生相同结果；
- 随机测试失败时必须保留 seed、run 和缺失序号，才有办法复现。
