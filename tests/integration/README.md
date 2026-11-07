# IBKR Paper 集成验证

默认测试不连接 IB，不会下单。先保持 `TRADING_MODE=PAPER`、`ALLOW_LIVE_TRADING=false`、`IBKR_READ_ONLY=true`。

## 只读账户/快照

```bash
ENABLE_PAPER_CONNECTION_TESTS=true python -m pytest tests/integration/test_paper_order.py -q
```

保留旧文件名，但已移除发送/撤销低价订单的行为。验证实际模拟账户、握手、三个快照屏障和断开连接；不采用 broker 数据写入数据库，不授予 READY。

## 行情

```bash
ENABLE_MARKET_DATA_TESTS=true python -m pytest tests/integration/test_market_data_live.py -q
```

需要可用实时行情授权和市场报价。连接成功但无行情、只有延迟/冻结报价，测试仍应失败，不能伪装为可交易。错误 10167 是提供延迟行情的警告，不是可执行实时数据证明。

只需排查连接或显示延迟行情时：

```bash
python -m apps.doctor --snapshot --quotes AAPL SPY --duration 10 --market-data-type 3
```

## 订单验收前置条件（尚未完成）

需要真实风险上下文/购买力、挂单预占、策略或明确批准的意图、故障恢复，以及用户明确授权的模拟订单验收。不要用测试中的 AllowRisk 替代生产风控，也不要凭 LIMIT 价格保证不会成交。
