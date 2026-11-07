# 03｜确定性策略、信号与组合目标规范

版本 1.0｜2026-11-07｜目标设计｜关联：[02 数据](02_DATA_ENGINEERING.md)、[04 AI](04_ML_AND_LLM.md)、[05 回测](05_BACKTEST_AND_RESEARCH.md)、[06 风控](06_PORTFOLIO_AND_RISK.md)

## 1. 为什么先写不带 AI 的完整算法

只有可复现的基准，才能判断 AI 改善了什么。若规则不完整、同根 bar 用未来高低价、策略直接下单，即使模型精度看起来高也没有可比较的结果。

本文件冻结 `MomentumBreakoutV1` 作为研究/工程基准，不声称它有 alpha。参数只作为预注册初值；调整需要新 strategy_version，不能看完 OOS 再改名叫“同一模型”。

## 2. 明确假设与否证

假设：对高流动性美股/ETF，在常规时段，当价格突破过去一段已完成 bar 的上沿、同时成交量和趋势支持时，短期正向延续可能覆盖交易成本。

竞争解释：单纯市场 beta、开盘噪声、隔夜/跨标的相关性、买卖价差幻觉、特定上涨年份、延迟入场后优势消失。

预先否证：含真实成本的 walk-forward 不优于匹配暴露基准；2 倍成本后显著失效；收益只由极少数交易/一个年份解释；参数稍改就崩溃；跨标的/时段不稳定。出现这些结果应暂停该策略，不以“换更强大模型”掩盖。

## 3. 输入与约束

- instrument：已验证 STK/USD、唯一 conId、白名单、可交易 session。
- bar：完整 finalized 5m OHLCV；来源一致；不存在 look-ahead。
- warmup：至少 60 根此前有效 5m bar；昨日可延续 EMA/ATR，但跨拆股断点必须重置/调整。
- 当前 bid/ask：用于入场风控与限价；不是为了改写历史信号。
- position/working orders：策略只读；同标的有有效入场/持仓时不加仓、不金字塔。
- 默认只做多；没有负仓位目标。开仓后未保护不得允许第二笔新风险。

## 4. 时间、窗口与指标的精确定义

以当前刚完成 bar `t` 为判断点；它覆盖 `[start_t,end_t)`，在 `available_at_t` 后才可决策。

| 名称 | 初值 | 定义 |
|---|---:|---|
| breakout lookback L | 20 | `H_prev=max(high[t-L:t])`，明确不包含 t |
| volume window V | 20 | `V_prev=mean(volume[t-V:t])`，明确不包含 t |
| EMA trend | 20 / 50 | 使用截至 t 的 close，alpha=2/(n+1)，首 n 根 SMA 初始化 |
| ATR | 14 | Wilder 平滑；TR=max(high-low,abs(high-prev_close),abs(low-prev_close)) |
| breakout buffer | 0.10 ATR | `close_t > H_prev + 0.10*ATR_t` |
| volume expansion | 1.5 倍 | `volume_t >= 1.5*V_prev`，V_prev=0 时拒绝 |
| stop distance | 1.5 ATR | `entry_reference - 1.5*ATR_t`，正价且不超过配置的合理距离 |
| profit reference | 2R | `entry_reference + 2*(entry_reference-stop_reference)` |
| maximum hold | 12 个决策 bar | 最长约 60 分钟，实际出口受交易状态约束 |
| cooldown | 6 个决策 bar | 上次完成退出后等待 6 根 finalized bar |

指标端必须把是否包含当前 bar 写进 feature definition，禁止 pandas 默认 rolling/shift 隐式决定。V1的20/50/60根窗口使用连续有效RTH bar，允许跨交易日延续，不生成隔夜虚构bar；ATR首个session bar的prev_close为上个有效RTH close，因而包含隔夜跳空。遇拆股而缺少可靠调整映射时清空warmup，不让巨大价格断点直接成为信号。未来改为“仅本日窗口”或隔夜单独建模必须新版本。

### 4.1 入场时间窗口

建议 `session_open + 15m` 到 `session_close - 35m`，只在完成 bar 上生成入场信号。市场常规 16:00 收盘时最后入场决策为 15:25；早收盘日同样相对 close 计算，不硬编码 16:00。

计划 flatten window 从 `session_close - 10m` 开始，不产生新仓。MVP 若无已验收的保护单和应急退出权限，不能进入有持仓 Paper 自动模式。

### 4.2 信号条件：全部同时满足

1. 策略/数据/账户所属 session 一致，当前数据质量通过。
2. 满足 warmup，全部指标有限，ATR>0。
3. `close_t > H_prev + 0.10*ATR_t`。
4. `volume_t >= 1.5*V_prev`。
5. `EMA20_t > EMA50_t` 且 `close_t > EMA20_t`。
6. 处于入场窗口，没有该标的持仓、工作中的入场、未解决 command 或 cooldown。
7. 当前 strategy bundle 没有停用/隔离；若使用 ML/文本特征，满足 bundle 明确的 feature completeness policy。

技术条件满足产生候选 Signal，并不代表通过账户/报价风控。账户/买卖价差/购买力不得在策略内默默“估一个值”。

## 5. Signal 契约与去重

新增 `StrategySignal` 建议字段：

```json
{
  "schema_version": "signal.v1",
  "signal_id": "generated-uuid",
  "signal_key": "hash(strategy_version,instrument_id,bar_end,side)",
  "strategy_id": "MomentumBreakoutV1",
  "strategy_version": "1.0.0",
  "instrument_id": "internal-instrument-id",
  "session_id": "XNYS-2026-11-09",
  "decision_at": "2026-11-09T15:00:02Z",
  "source_bar_end": "2026-11-09T15:00:00Z",
  "side": "BUY",
  "entry_reference": "100.00",
  "stop_reference": "98.50",
  "take_profit_reference": "103.00",
  "score": 0.67,
  "expires_at": "2026-11-09T15:00:12Z",
  "feature_snapshot_id": "feature-id",
  "reason_codes": ["BREAKOUT", "VOLUME_EXPANSION", "UPTREND"],
  "model_bundle_id": null
}
```

日期/数字仅为格式示例，不是实际交易建议。score 是用于排序的确定性归一化分值，不是成功概率。V1固定公式如下，`clip(x)=min(1,max(0,x))`：

- `breakout_strength=clip((close_t-H_prev)/ATR_t/2)`。
- `volume_strength=clip((volume_t/V_prev-1)/2)`。
- `trend_strength=clip((EMA20_t-EMA50_t)/ATR_t)`。
- `score=0.5*breakout_strength+0.3*volume_strength+0.2*trend_strength`。

固定权重是预注册初值，不是优化结果；非法分母返回NoSignal。概率字段另设 `calibrated_probability`，没有校准证据时为空。

`signal_key` 同一 bar 重放只持久化一次；新的模型版本产生新的策略 bundle，并保留基准 counterfactual。Signal 超时默认不重新追价，下一 bar 可以重新满足条件但属于不同候选。

NoSignal 也记录：`INSUFFICIENT_WARMUP / BAD_BAR / OUTSIDE_ENTRY_WINDOW / NO_BREAKOUT / NO_VOLUME / NO_TREND / ALREADY_EXPOSED / COOLDOWN / FEATURE_UNAVAILABLE`。可以按 bar 批量保存计数与详细抽样，避免无信号日志吞没真实故障。

## 6. 从信号到目标，不从信号到 broker

`Strategy.evaluate()` 输出候选；`PortfolioPlanner.plan()` 结合剩余风险预算给出 RequestedTrade；`RiskAuthorizationService` 决定最终 quantity 与预占。

接口示意：

```python
class Strategy(Protocol):
    def evaluate(self, features: FeatureSnapshot,
                 context: ReadOnlyStrategyContext) -> SignalResult: ...

class PortfolioPlanner(Protocol):
    def propose(self, signals: tuple[StrategySignal, ...],
                portfolio: PortfolioView) -> tuple[RequestedTrade, ...]: ...
```

策略不 import `IBKRBroker`、`OrderManager`、数据库 session、系统时钟或网络库。依赖注入 `decision_at`，保证重放产生相同结果。

同一轮多个标的候选：按固定 score 降序、instrument_id 次序打破平局；逐笔在统一账户预算下审批。不能每个策略各自看到“剩余 20%”然后一起用满。

## 7. 定量 sizing 示例

最终公式和事务见 06，这里定义策略需要提供什么：

- entry_reference：当前信号 bar close，不是保证成交价。
- stop_reference：策略原始风险假设；风控必须基于实际允许的最差入场 limit 重算距离。
- `Q_risk=floor(risk_budget / (worst_entry-stop + cost_buffer_per_share))`。
- `Q_final=min(Q_risk,Q_position,Q_gross,Q_buying_power,Q_liquidity,Q_operator_cap)`。
- Q_final<1 则拒绝，禁止为了“至少交易一次”四舍五入到 1。

如果实际成交价让每股风险超预算：先确保已成交部分保护，取消未成交剩余，必要时严格 reduce-only；不得事后扩大 stop 或加大 risk_budget 来匹配已发生交易。

## 8. 入场与退出行为

### 8.1 入场

参考价不是委托价。ExecutionPlanner 从当前新鲜 BBO、tick size、最大滑点/偏离限制生成有上界的 LIMIT；不能隐含 MARKET。建议候选有效期 10 秒、未成交 entry 最长存活 30 秒，超时取消需 broker 确认；未知状态不重发。真实 TTL 必须在 replay/paper 上校准。

### 8.2 退出优先级

1. broker 原生保护止损或目标订单成交。
2. account/system 紧急 reduce-only 指令（需独立权限与状态校验）。
3. flatten window。
4. 最大持有 12 bar 到期。
5. `close_t < EMA20_t` 的趋势失效退出。

若多个触发同一时刻，只产生一个 exit intent；该 intent 与现有保护订单组协调，不允许多个卖单合计超过剩余仓位。退出目标永不为负。

风险预算 stop 与真正 broker 保护单是两回事。初版 LIMIT-only 基础设施在完成 07 的保护设计前只能 shadow/replay，不能宣称可无人值守持仓。STP-MKT 与 STP-LMT 各自的成交/跳空风险需明确评估并由用户批准。

## 9. 状态、重启与参数

策略状态最小集：`last_processed_bar_end, last_signal_key, cooldown_until_bar, position_cycle_id, strategy_version, session_id`。持仓/订单事实来自账本，不由策略复制一份状态并独立修改。

重启从 durable cursor 恢复；重放最近 N 根 bar 建指标，再从 cursor 后消费。cursor 与 signal 持久化同事务或幂等键保证，不可先推进 cursor 再丢掉信号。

配置包括参数值、范围、单位、版本、hash、冻结时间。研究搜索最多预注册 24 个组合（例），超出需更新实验计划并累计 trial count；不要按每份 notebook 各算 24 以规避多重检验。

## 10. 后续策略，不同时全部开发

- 趋势/动量基准失败后，可立项不同经济假设，例如日内均值回归；不在同一 OOS 上不断试直到赚钱。
- ML filter 首先决定“交易或不交易”，而非直接连续调仓；更容易核验新增价值。
- LLM 事件模块先做 risk veto 或独立观察，不把叙述性 bullish 当买入信号。
- 多策略时先有组合风险和相关敞口分配，再加入第二个 strategy_id。

## 11. 最小单元/回放验收集

| 用例 | 必须结果 |
|---|---|
| t 的 high 自己创造突破、但 prior high 未突破 | 按排除 t 的规则正确判断，无 off-by-one |
| volume 当前值误入均值 | 测试断言计算仅用此前 V 根 |
| RSI/EMA 等 warmup 不足 | NoSignal，不返回 NaN score |
| 同一 bar 回放两次 | 相同 signal_key，最多一条候选 |
| 信号过期后收到 fresh quote | 拒绝旧信号，不延长 expires_at |
| 早收盘日 | 相对 close 的最后入场和 flatten 时间正确 |
| 持仓 12 bar 到期 | 一个 reduce-only exit，非新开仓 sell |
| 已有部分成交/保护单 | 不重复 entry，不超量 exit |
| 断线重启 | cursor/持仓/cooldown 还原一致 |
| 只有延迟 BBO | 候选可研究记录，但不可执行 |

G2 基准回放要求代码/配置/数据相同则 signal stream hash 相同；G3 研究有效性由 [05](05_BACKTEST_AND_RESEARCH.md) 定义；任务 STRAT-01…STRAT-03 在 [10](10_ROADMAP_AND_BACKLOG.md)。
