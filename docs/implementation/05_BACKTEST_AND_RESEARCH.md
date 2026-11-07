# 05｜事件回测、实验设计与策略有效性验证

版本 1.0｜2026-11-07｜目标设计｜关联：[02](02_DATA_ENGINEERING.md)、[03](03_STRATEGY_SPECIFICATION.md)、[04](04_ML_AND_LLM.md)、[11](11_TEST_AND_ACCEPTANCE.md)

## 1. 必须拒绝的“伪回测”

- 用当前 bar 的最高/最低/收盘产生信号，又在同 bar 最有利价格成交。
- 随机切分时间序列，训练集包含测试事件的未来标签。
- 不计佣金、价差、滑点、未成交、停牌和订单 TTL。
- 用所有数据 fit scaler/特征选择/阈值，然后只把模型训练称为 train。
- 在几十个策略/OOS 重复试验后只展示最好的 equity curve。
- 把 Paper 模拟成交当真实队列和市场冲击证据。

回测目标是揭示错误和否证假设，不是制造一条好看的曲线。

## 2. 引擎设计与与生产代码复用

新增 `core/backtest/`：

| 文件建议 | 职责 |
|---|---|
| `clock.py` | 单调推进的 ReplayClock，注入 now，不使用真实 wall clock |
| `feed.py` | 按 available_at 读取 dataset version，合约/行动/日历事件 |
| `engine.py` | 稳定事件优先级、控制循环、快照、trace |
| `broker.py` | 仿真接受/拒绝/部分成交/取消，符合 OrderGateway 目标契约 |
| `fills.py` | bar/quote 两种成交模型及保守歧义处理 |
| `costs.py` | 佣金、费用、价差、滑点、冲击参数 |
| `metrics.py` | 现金/权益/交易统计，禁止只用交易百分比累加 |
| `experiments.py` | experiment manifest、fold、trial registry、复现包 |

复用生产策略、feature definitions、risk rules、账本投影和订单状态机。只替换时钟、输入数据源和 broker transport。不能在 backtest 中另写一套“近似的”风控/退出规则。

事件同时间排序要稳定：先企业行动/日历 → 已提交订单可观察到的市场事件 → broker fill/status → 账本更新 → bar finalized → 策略/新 command。新订单不能作用于已经消费过的事件。排序策略写进 engine_version。

## 3. 两级成交模型

### 3.1 一级：1m bar 保守模型

用于快速否证/工程测试，不证明真实 execution quality。

- 5m 信号只能在 available_at 后提交，最早消费下一根可用 1m bar。
- BUY limit 若下一 bar open≤limit：候选成交价为 open 加不利成本/滑点，且不得高于 limit；高于限价则不成交。
- 若 open>limit、但 low≤limit：允许“触及限价”情形，但按保守 fill-probability/穿透规则处理；默认用需要严格穿透一个 tick 的规则，不认为 touch=必成交。
- 卖出对称，价格永远不能违反限价。
- 每 bar 成交量帽子如 1% eligible volume 是假设初值，不是可观察到的真实队列。数量不足则部分成交，TTL 到期请求撤销。
- signal 在 bar 中间才产生而只有整 bar OHLC 时，不允许使用该 bar 中不知道先后顺序的 low/high；等下一 bar。
- 同 bar stop 与 take-profit 都可能触发：采用不利退出或 AMBIGUOUS 标签，单列结果；不能择优。
- limit/slippage 算法不能为了收取“负滑点”产生优于市场的不合理成交。

### 3.2 二级：quote/trade 驱动模型

在数据许可和成本允许时建设。订单进入市场有固定/分布延迟；buy 在 ask≤limit 条件下，按可见 size 上界、参与率和保守队列估计填充。没有完整 order book 不声称有精确 queue position。

不可见流动性、auction、trade condition、hidden order 都是误差源。用 Paper 的 ack latency 与拒单行为校准工程路径，用真实获授权 market history 校准价格可成交性；Paper fill 不等同实盘。

### 3.3 保护和取消

- 取消请求到确认有延迟，期间仍可成交。
- 止损不是保证止损价成交；跳空按 next tradable price 或订单类型规则处理。
- STP-LMT 跳空未成交的持仓风险必须留在账本，不能强制按 stop 价平仓。
- 未成交入场不能生成已持仓收益；部分成交只计实际数量，剩余 reservation 仍占用。

## 4. 成本模型

`net_pnl = gross_trade_pnl - commission - regulatory/exchange_fees - spread_cost - slippage/impact - borrow/financing(if enabled)`。

注意 spread 已由 bid/ask 执行价体现时不能再次全额扣同一项。需要清楚写哪部分在 fill price，哪部分在独立费用。V1 不做空/杠杆，borrow/financing 为 not applicable，不伪装成缺失费用为零。

费用以 provider/account plan 的版本化快照计算；IB 实际报价、最低佣金、交易所/监管费需上线时官方复核，不能把示例费率当真实费率。

成本情景：baseline、2× variable friction、3× variable friction、额外 250ms/1s/5s 决策延迟、fill participation 减半、quote 缺失/停牌。固定最低佣金单独处理，不盲目将所有费用乘同一倍数。

## 5. 资金与权益核算

使用 06 同一个账本：现金、持仓成本、已实现/未实现、待结算、费用、分红、外部现金流。权益曲线以 session 内一致频率 mark-to-market，日终收盘价规则固定。

- 不将 `sum(trade_return)` 当投资组合收益。
- 多笔同时持仓需要总资金约束与风险预占。
- 空仓现金收益是否计入需声明；benchmark 必须采用同样约定。
- 入金/出金从收益剔除；研究可以没有外部流，但实盘/模拟 reset 必须识别。
- 单笔期望/胜率与组合收益是两种指标，不混淆。

## 6. 时间切分与 walk-forward

### 6.1 推荐起始协议

数据可用时，使用滚动 train 12 个月、validation 3 个月、outer test 3 个月，每次前移 3 个月。只是窗口初值，必须保证各段足够标签/市场状态；若数据不足，延长采集，不随机 shuffle。

最后保留最近 6 个月 lockbox，仅在研究流程、参数搜索、特征和通过条件冻结后使用一次。为了容纳12个月训练、3个月验证、至少约12个月拼接OOS及6个月lockbox，建议先评估至少36个月合格历史数据（若许可/质量允许，覆盖更多不同市场状态更有价值）。精确交易日数以日历计算，不能用12个月直接代替252个有效交易日。整个窗口设计依赖实际数据长度，不能在只有半年数据时宣称完成此协议。

训练样本依据 decision_at 分折，但 purge 依据 label interval：任何 `[label_start,label_end]` 与 validation/test 区间重叠的 train 样本移除。边界 embargo/gap 至少覆盖最大标签 horizon 与供应商可见性延迟。对于严格前向训练，重点是训练尾部 purge；多段组合 CV 还要处理测试后相邻区间污染。

所有标的共享相同时间分界；同一个市场时刻不能在 AAPL train、SPY test 中随意交错。新闻按事件/重复报道组进一步分组。

### 6.2 内外循环

inner train/validation：模型、超参、特征选择、缩放、校准、阈值、策略参数全部在这里完成。outer test：只预测/交易，无重新 fit。每个 fold 输出冻结 artifact 和完整预测，不只输出平均分。

看过 outer test 的策略修改必须登记新 trial；lockbox 用过后不能继续叫“未见测试集”。可以进入新一段前瞻数据，不可通过重命名实验擦掉历史搜索。

## 7. 统计与经济评价

### 7.1 必报指标

- 组合：累计/年化净收益（短历史不滥年化）、日波动、最大回撤/持续期、Sharpe、Sortino（定义明确）、Calmar。
- 交易：round trip 数、胜率、profit factor、平均/分位数盈亏、expectancy、持有时间、turnover、fill ratio、取消/拒单率。
- 风险：gross/net、行业/相关群组集中度、最差日、尾部损失、跳空暴露、预算超限次数。
- 执行：mid/arrival/decision price 对比、费用占 gross pnl 比例、延迟和 slip 分布。
- 稳健性：按年/月、波动 regime、标的、时段、成本场景的结果，不隐藏负分组。

Sharpe 的年化频率使用日收益而不是将 5m trade samples 当独立样本。非独立交易需要时间块 bootstrap；推荐以交易日或周为块，并测试不同块长敏感性。

### 7.2 MVP 建议门槛（预注册初值）

- 拼接 outer OOS ≥252 个交易日，完成 round trip ≥200；不足 INCONCLUSIVE。
- baseline-cost 后平均日净收益的 95% 时间块 bootstrap 下界>0。
- 相对于预注册的匹配暴露基准，收益/风险效用改善方向稳定；如果声称 AI 提升，使用配对日差异并报告置信区间。
- 2× variable friction 下净收益不为负；最大回撤建议≤5% paper NAV，仍需与目标 risk budget 一致。
- 任一交易不贡献总净利润的异常大比例（建议检查>10%），任一标的贡献>50% 必须解释并留出额外测试；这是集中度报警而非机械保证。
- 各 fold 无账户/金额不变量违规；数据质量/泄漏问题 0 容忍。
- 概率/文本指标合格但经济增益不合格时，只能保持研究/shadow。

统计阈值不能保证未来获利。多重尝试下的 nominal 95% 区间不足以自动证明 alpha；记录 trial universe，报告 deflated Sharpe/selection bias 等辅助诊断的假设。不要通过调 bootstrap 方法获得想要的显著性。

## 8. 对照基准

同时报告现金、不交易、相关标的 buy-and-hold、风险/时间暴露匹配的规则基准、03 原始策略、随机接受相同比例候选。buy-and-hold 不是日内策略唯一公平对照，但应显示其 beta 和机会成本。

AI ablation：去掉新闻、去掉市场背景、只用简单分数、旧模型与新模型固定相同候选池。必须区分降低交易频率、降低 gross、改善入场排序三种来源。

## 9. 实验可复现包

每个 `experiment_id` 保存：

`code_sha/worktree_patch_hash, dependency_lock_hash, dataset_manifest_ids, calendar_version, universe_version, strategy/model/feature versions, cost_model, execution_model, risk_profile, seed, fold_definition, trial_count, command_args, outputs_hash, metrics, failure_log, reviewer`。

建议输出：`config.json`, `folds.json`, `signals.parquet`, `predictions.parquet`, `orders.parquet`, `fills.parquet`, `ledger.parquet`, `daily_equity.parquet`, `metrics.json`, `report.md`。先写数据和结论，是否做图另行实现；本规划不生成虚构收益图。

## 10. 具体验收用例

BT-01 无行情时不成交；BT-02 下单前事件不可填；BT-03 limit 上限不被滑点突破；BT-04 同 bar 双 barrier 保守；BT-05 split/分红不制造利润；BT-06 cancel/fill 竞态重放一致；BT-07 两个策略竞争余额只允许合法总量；BT-08 train transform 不见 test；BT-09 重跑 stream/账本 hash 一致；BT-10 2×成本自动生成压力结果；BT-11 无成交/亏损/空测试集都有合法报告而非除零；BT-12 缺失时间/复权 manifest 阻断实验。

完成门槛：G2 工程回放与 G3 研究分别签字。不能用 G2 的“程序算对了”替代 G3 的“策略证据足够”。任务 BT-01…BT-04 见 [10](10_ROADMAP_AND_BACKLOG.md)。
