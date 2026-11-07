# 06｜账户快照、会计账本、组合与风险预占

版本 1.0｜2026-11-07｜目标设计｜关联：[07 执行](07_EXECUTION_AND_RECOVERY.md)、[08 表/事务](08_SCHEMA_API_AND_EVENTS.md)、[11 验收](11_TEST_AND_ACCEPTANCE.md)

## 1. 为什么这是第一个交易前置工程

现有 RiskEngine 有 NAV、日损、gross、position count、报价时间等字段，但没有真实账户来源、购买力、持仓感知的减仓、挂单预占。仅把 IB 的某个余额字段填进去仍然不够：两笔同时审批可能各自用同一笔资金，部分成交与取消又会使预算失真。

本模块目标：**同一时刻所有持仓和所有尚可能成交的订单，总风险必须统一计量；资金或状态未知时不能放新风险。**

## 2. 金额、数量与单位

- 账本/订单/risk inputs 使用 `Decimal`；数据库目标 `Numeric(28,10)` 或经资产约束定义的定点精度，禁止用 float 做唯一会计真值。
- 所有金额带 currency；MVP 只 USD，其他 currency 返回 UNSUPPORTED，不能默认为 USD。
- STK multiplier 明确为合约确定值，不从缺失字段猜衍生品乘数。
- quantity 使用 Decimal 类型，但 MVP `quantity == floor(quantity)` 且≥1；为将来分数股留类型不代表授权分数交易。
- 策略指标允许 float，在进入金额边界时按字符串/确定舍入转 Decimal，检查 finite、范围和最小价格增量。
- 费用与汇率保存来源/生效时间；余额美元和证券价格美元不可混用计量单位。

迁移 float 到 Numeric 需影子列、校验报告与对账，不做盲目 ALTER 后认为历史误差自动消失。

## 3. AccountSnapshot 契约

新增字段至少：

`account_id, broker_account_ref(encrypted), snapshot_id, collection_epoch, started_at, completed_at, received_at, available_at, base_currency, net_liquidation, cash, settled_cash, available_funds, buying_power, excess_liquidity, initial_margin, maintenance_margin, broker_realized_pnl?, broker_unrealized_pnl?, positions_revision, open_orders_revision, account_type, quality_flags, complete`。

### 3.1 采集规则

- 在当前连接代次发起 account summary/updates 等 SDK 请求，映射字段名与币种；完成屏障以实际 API 文档为准。
- 必要字段清单按 cash/margin 账户 profile 固定；非必要字段可 null，必要字段未知则 INCOMPLETE。
- initial snapshot 与后续增量区分；多字段不同时间到达，必须有一致性/最大时间跨度限制。
- 建议 account TTL=15 秒，集合内必要字段跨度≤5 秒；账户心跳/订阅更新频率不足时，按限流允许频率主动刷新。达不到 TTL 就减少交易，不擅自扩大 TTL。
- account、positions、orders 的 collection_epoch 必须一致；但同代次也不保证同瞬间，需用账本增量及记录 revision 重校验。
- 断线、重新登录、账户替换、完整快照开始：原快照失效；不会因为一个新 cash tick 到来就把旧持仓整体刷新。
- broker PnL 可辅助对照；主要交易 PnL 来自内部账本与可审计费用，不将未定义时间基准的 broker dailyPnL 直接当日损。

### 3.2 购买力语义

MVP 不启用杠杆：可用开仓现金取内部可支配现金与 broker 确认可用资金的保守较小值，再减安全缓冲与尚未反映的预占。不要把 buyingPower 的杠杆值全部用于 cash-only profile。

现金账户未结算资金、券商 buying power、日内交易限制的含义依账户/辖区不同，必须检查。规划不硬编码当前监管门槛。默认不能用无法确认可再次使用的 sale proceeds 开新仓。

## 4. 账本设计

### 4.1 事实源

不可变 ledger entries；持仓/现金/PnL 是可重建投影。broker executions/commission/corporate actions/external cash events 入 inbox，经校验后在同事务写 ledger 与 cursor。

事件类型：`TRADE_FILL, COMMISSION, FEE_ADJUSTMENT, DIVIDEND, SPLIT, CASH_TRANSFER, BROKER_CORRECTION, REVERSAL`。不以直接覆盖 positions.quantity 修复差异。

### 4.2 成交示例

买 10 股 ×100 USD、费用 1 USD：

- 现金经济余额减少 1001。
- 证券持仓增加 10、成本增加 1001（若费用资本化政策如此，必须全程一致）。
- 未实现收益 mark=101 时为 10×101−1001=9。

卖 4 股 ×102、费用 0.40，按平均成本/选定 lot 规则释放成本 400.40：

- 现金增加 407.60；持仓剩余 6。
- 已实现 PnL=407.60−400.40=7.20。
- 剩余成本 600.60。

策略绩效、broker 报表可能使用不同成本归属/税务 lot 规则，必须分开记录并说明差异。内部账本不是税务报表。现金 settlement_date 按官方规则及假日计算，不假设所有产品完全一样。

### 4.3 幂等与修正

唯一经济事件键为 broker/account/execId/revision 或其明确的 correction key；重复同 payload 无效果；相同身份但不同 payload 进入 CONFLICT 队列。broker 修正以 reversal+replacement 保留先前事实，不修改原 entry。

佣金晚到：先 provisional fee reserve，收到 commission 后补记差额并重算日损；严禁等费用到齐才记录持仓，也不能把暂未收到的费用永远当 0。

### 4.4 投影一致性

更新 ledger、position/cash projection、risk reservation、processed_event_id 在一个数据库事务；事务失败整体重试。重放所有 ledger 能重建相同余额和 PnL；checkpoint 包含 ledger offset/hash，不能从任意旧 JSON 启动就READY。

## 5. 风控输入与纯函数评估

`RiskContext` = 完整 AccountSnapshot + PortfolioView + 活跃 reservations + quote snapshot + contract rules + session/runtime profile + operator gate generation + strategy bundle + intent。

RiskEngine 是纯函数，返回所有规则结果或决定性拒绝集合；无 I/O、无真实时钟、无持久化副作用。应用层负责获取、锁、预占与落库。

建议检查顺序：

1. schema/身份/有限值/单位/币种/数量/tick 合法。
2. runtime profile 与用户批准范围、单写入者 lease。
3. broker READY、当前 epoch；gate/只读条件。
4. 账户/持仓/订单/报价完整且新鲜，信号未过期。
5. classify 为 OPEN_OR_INCREASE、REDUCE_ONLY 或非法穿零。
6. OPEN 分支检查日损、buying power、gross/position/group/stop-risk、流动性、频率。
7. REDUCE 分支检查已确认可减持仓、已有卖单/保护组占用，不能因开仓限制禁止风险减少。
8. 决定、快照 hash、规则版本、数量和拒绝原因全部 durable。

## 6. 敞口与 sizing 公式

对只做多 STK：

- 当前 gross = Σ |quantity_i × conservative_mark_i|。
- 风险预占 gross = 所有尚可能成交入场剩余数量 × 允许最差价格，不扣相反方向未确认退出。
- projected gross = current gross + not-yet-reflected reservations + candidate notional。
- 单标的 projected notional 使用该标的**现有持仓 + 已批准挂单 + 新候选**，不是只检查本次 order 的金额。
- open_positions 统计持仓或有有效新增风险预占的不同标的；同标的不重复占 slot，但仍受 no-pyramiding profile 约束。
- 组合相关上限初期可用行业/ETF-overlap 群组规则，后续再引入 covariance；群组不明按保守默认处理。

计算顺序先生成符合价格增量的最差 limit/stop，再计算：

```text
risk_budget = NAV * max_trade_risk_pct / 100
per_share_risk = max(0, worst_entry - stop_reference) + fee_slip_buffer
Q_risk = floor(risk_budget / per_share_risk)
Q_position = floor((NAV*max_position_pct/100 - existing_symbol - reserved_symbol) / worst_entry)
Q_gross = floor((NAV*max_total_exposure_pct/100 - current_gross - reserved_gross) / worst_entry)
Q_cash = floor((usable_cash - reserved_cash - cash_buffer) / (worst_entry + fee_per_share))
Q_final = min(Q_risk, Q_position, Q_gross, Q_cash, Q_liquidity, Q_operator_cap)
```

所有负剩余额度夹到 0；Q_final<1 拒绝。费用有最低值时需以最终 quantity 重新迭代成本，不能只用固定 fee_per_share。新增算法不能通过减小 stop distance 到不合理程度放大股数。

示例 NAV=100,000、entry=100、stop=98.50、每股 friction buffer=0.10：Q_risk=floor(250/1.60)=156；单标的 5% 帽为50股；gross/cash足够时上限50股。若处于首批 Paper profile，额外 1 股帽最终变成1；如果预算不够1股就0，不强制交易。

## 7. 日损与回撤

日 session baseline 使用入场前可验证 NAV，日收益剔除 external cash flows。`session_pnl = realized + unrealized - fees - baseline_adjustments`；日损率按固定 session_start_nav 计算，避免亏损后分母下降造成漂移。若 broker paper 账户被 reset，识别为外部事件并停机重建 baseline，不能当暴利或巨亏。

初值日损≥1% 停止新风险并取消未成交入场请求；存量保护不撤销，reduce-only 继续可用。达到阈值不是保证止损，跨价/停牌仍可能继续亏损。

另外有 high-water drawdown guard（如建议 5% 为研究起始值）、单日订单量/拒单率/异常价格 guard。限额均版本化、需要审批，不由 AI 修改。

## 8. Reservation：防止同一资金被批准两次

状态建议：`HELD → PARTIALLY_CONSUMED → CONSUMED/RELEASED`，另有 `UNCERTAIN`。TTL 只适用于**尚未发出命令且能证明未发送**的授权；订单已发送/未知时到期不能自动释放钱。

事务 R1（不持锁执行网络 I/O）：

1. 获取 account_id 的数据库锁/一致 serializable 边界，检查 writer fencing token。
2. 读 portfolio revision 和全部活跃 reservations，确认 snapshots/quote 在 TTL 内。
3. 以 canonical intent hash 去重，评估全部规则。
4. APPROVED：写 RiskDecision、Reservation、OrderCommand + outbox；REJECTED：写决策与证据，零预占。
5. 提交。dispatcher 消费已提交 command，不在事务内调用 IB。

事务 R2（最终发送检查）：验证 command/lease/gate generation/quote/account revision/TTL；若风险上下文变更，重新完整评估并原子替换同一 reservation，不增加第二份。之后按 07 durable ID 流程发送。

**对现有接口的重要迁移**：当前 `PreTradeCheck.authorize(intent)` 在 submit 中调用两次。它必须是“校验已有授权/同一预占”的幂等操作，绝不能每次调用都创建一个 reservation。新增 reserve_once 放在应用服务中，以 command_key 唯一约束保护；迁移前无实际上下文继续拒绝。

### 8.1 partial fill/cancel 生命周期

- fill 3/10：投影增加3股，同时 reservation 从10转为7股剩余风险，二者同事务，不重复计3股。
- cancel_requested：仍保留7股可能成交风险。
- cancel_confirmed remaining=7 且执行对账完整：释放7股。
- fill 与 cancel 回调冲突/窗口不完整：保留保守预占并 UNKNOWN，不能取对自己有利的那个回调。
- send timeout：标 UNCERTAIN，保留预占直到按 orderRef/permId/窗口和操作审计确定结局。

## 9. reduce-only 与保护数量

`sellable = confirmed_long - unrelated_working_sell_reservations`，同一 OCA 保护组不简单把两个互斥子单相加，但必须验证 broker 组语义；无法保证互斥时用最坏总量并暂停。

任何 exit quantity≤可确认 long，且不能穿过0。无新鲜报价时，开仓绝对拒绝；减仓是否允许 broker-native emergency order 由独立用户批准 profile 定义，不自动从 LIMIT 变 MARKET。保护/应急机制要在事故前完成测试，不能事故中临时发明。

## 10. 验收与故障例子

- 两候选各需3000现金，余额5000：至多一笔获准或第二笔裁量至合法数量。
- 同 UUID authorize 两次：一份预占，不是两份。
- DB断开：无send；提交成功但响应丢失：幂等查询找到原command。
- fill持久化后进程崩溃：重放后持仓/预占一致。
- 佣金晚到触发日损：及时阻止下一次开仓。
- CANCEL_PENDING 超时：不能自动释放。
- 账户快照部分更新：保持INCOMPLETE，不用旧NAV+新cash拼完整。
- 已日损超限但合法减仓：允许reduce-only，不允许新的BUY。
- 持仓浮点转Decimal前后差异：有迁移报告，无法解释则拒绝发布。

任务 ACCT-01/LEDGER-01/LEDGER-02/RISK-01/RISK-02 在 [10](10_ROADMAP_AND_BACKLOG.md)。账户上下文、账本和预占未全部验收前，不进入任何自动模拟下单阶段。
