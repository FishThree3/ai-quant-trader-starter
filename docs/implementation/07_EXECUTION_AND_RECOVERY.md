# 07｜IBKR 执行、保护订单、对账与故障恢复

版本 1.0｜2026-11-07｜目标设计｜关联：[06 风控](06_PORTFOLIO_AND_RISK.md)、[08 契约](08_SCHEMA_API_AND_EVENTS.md)、[09 运维](09_OPERATIONS_AND_SECURITY.md)

## 1. 执行目标与不承诺事项

目标是所有发单都有可追溯身份/授权、所有可能发生的成交都得到保守处理，并能在重启后确定“知道什么、不知道什么”。不承诺网络 exactly-once、止损价保证或 Paper/实盘一致。

现有适配器支持 LIMIT、cancel、基本 SDK 回调、屏障对账与只读保护。尚未完整支持保护单组、commission、执行修正、自动重连编排或对全部账户活动的持续运营处理。

## 2. 身份命名空间

- 内部：`account_id, run_id, signal_id, command_id, order_uuid, order_group_id`。
- IB：`client_id, order_id, perm_id, exec_id, con_id`；broker orderId 不能脱离 client/account 使用。
- command_key = canonical intent/策略信号/目标修订的稳定业务键；不能每次 HTTP 重试生成新 UUID。
- orderRef 保存短而稳定的内部关联值，长度/编码以 SDK/IB 限制复核；数据库保留全 UUID 与映射。
- connection_epoch 只用于连接/回调有效性，不代表可以在每次重连重用同一 broker orderId。若 broker 重置 ID 序列，必须停机做专门迁移/历史命名空间处理。
- 未完成/历史账户数据需要 account/client namespace 的唯一约束，见08。MVP固定client，不能临时加offset成为新的交易writer；doctor的offset仅只读。

## 3. 订单命令与发单事务

1. 应用服务从 06 得到一个已持久化、未过期、对应固定 payload 的 authorization/reservation。
2. dispatcher 验证单写入者 lease、broker账户/epoch、gate、只读profile、quote、限额。
3. 预留IB orderId，并在数据库写 `PENDING_SUBMIT, broker identity, command state=DISPATCHING`，提交。
4. 最后同步安全检查后调用 official SDK `placeOrder`；不得在HTTP handler或模型tool里调用。
5. 无论SDK返回什么，保持等待broker回执。返回成功不等于accepted、filled或保护已建立。
6. socket write抛异常/进程在边界崩溃：`UNKNOWN/UNCERTAIN`，保留风险预算，禁止自动换UUID重发。
7. 只有能证明命令未发送的明确本地拒绝才能转本地CANCELLED并释放预算；证据不足一律未知。

outbox保证命令不会因DB提交后进程崩溃而丢掉，但不意味着可对DISPATCHING消息无脑重试。dispatcher必须区分“未发送”与“发送结果未知”。

## 4. 状态模型

订单状态沿用现有CREATED/PENDING_SUBMIT/SUBMITTED/PARTIALLY_FILLED/FILLED/CANCEL_PENDING/CANCELLED/REJECTED/EXPIRED/UNKNOWN，并增加必要字段而非把每种broker文本做成状态。

独立跟踪：

- `status`：本地生命周期。
- `broker_status_raw`：保留原文用于排查。
- `cumulative_filled`, `remaining`, `avg_fill`, `last_exec_offset`。
- `cancel_request_at`, `cancel_ack_at`；cancel不是原地把订单删除。
- `uncertainty_reason`, `reconciliation_run_id`, `version`。
- group的`protection_state`与订单status分开。

状态转移必须单写入/乐观版本保护；回调可以乱序/重复，不把已知累计成交从10改回4。broker correction是新事实，需明示修正路径，不被“单调”规则永久吞掉。

## 5. 回调映射与安全处理

| IB语义/回调类别 | 写入事实 | 注意 |
|---|---|---|
| nextValidId/managedAccounts | 连接身份和序列观察 | 两者不授予READY；账户变化使快照失效 |
| openOrder/orderStatus | 订单快照/累计状态 | status可能重复；只有匹配身份才折叠 |
| execDetails | 单笔成交事实 | execId幂等；client-local fallback；与permId冲突阻断 |
| commission report | 费用/货币/修订 | 可能晚于fill；先记持仓再补费用 |
| positions/account updates | broker观察值 | 不直接覆盖内部账本；差异走对账 |
| errors | 分类事件 | request ID与order ID可能碰撞；行情354/300等不能猜为订单拒绝 |
| marketDataType | 实际行情模式 | 冻结/延迟只展示 |
| connectionClosed/1100 | 连接失效 | 禁新单、失效quote/context/授权；保护留在broker |
| 1101/1102恢复 | 需要重新同步 | 区分数据是否丢失，不直接READY或ACTIVE |

具体SDK回调签名由已安装官方版本测试固定，不能从旧网上示例抄参数数量。raw callback进入有界队列；执行/账户关键队列溢出不得丢数据后继续READY，立即暂停并重建。行情可按instrument coalesce，但不能静默丢掉价格撤销或mode变更。

## 6. 价格、数量、TTL与请求预算

- qualify conId、exchange、currency和market rule后才能构建order。
- 最终price round到合法increment；BUY限价不能高于授权的worst_price，SELL减仓最低接受价/应急类型有独立规则。
- STOP price rounding后要重算risk distance，不把向下取整造成的风险增量藏起来。
- 整股约束、minimum size、订单类型/TIF、outsideRth都明确；默认outsideRth=false。
- entry TTL初值30秒，signal TTL10秒；TTL到后发送cancel并等待，不当作已取消。
- 请求队列优先级：cancel/protection/account/reconciliation > new entry > market subscription > historical backfill。
- pacing预算从官方文档和当前account/serverVersion配置导入；每个请求类型独立观测限流错误，不靠无限重试。
- 对price-band、tick-size、资金拒绝记录reason，不通过扩大价格或换MARKET自动规避。

## 7. 保护订单组：自动Paper的硬前置

### 7.1 不可跳过的设计决策

03中的stop_reference仅是预算参考。自动持仓需要已验证的broker-native保护，且人工理解：

- STP/MKT类保护触发后通常偏重成交，但价格可能大幅滑点。
- STP-LMT限定价格，但跳空可能完全不成交。
- 具体STK/市场/账户可用类型、触发规则、RTH行为和费用需要IB确认。

建议在Paper中分别验收两种行为，再由用户选择保护profile；默认未选择/未验收则只允许shadow，不偷偷从当前LIMIT-only扩展。

### 7.2 Group结构

`order_group_id`包含parent entry、protective stop、optional take-profit、exit replacement；字段包括desired_qty、filled_entry_qty、open_position_qty、confirmed_protected_qty、group_revision、OCA/bracket identifiers。

发送parent/children的transmit顺序必须依照实际SDK支持。不能假设bracket对部分成交会自动保护到正确数量；对每个目标order type建立Paper验证记录。

group状态：`PREPARED → ENTRY_WORKING → PARTIALLY_OPEN/OPEN_PROTECTED → EXITING → CLOSED`；任何缺口为`UNPROTECTED/UNKNOWN`并触发高优先告警。

### 7.3 保护数量不变量

- confirmed protected quantity必须覆盖已确认open long，且不能形成多余独立SELL使仓位翻负。
- entry部分成交时及时维护保护数量；更新保护前不无条件先取消旧保护。
- take-profit和stop的互斥关系必须由broker确认，不能只靠本地认为“两单不会都成交”。
- 手动flatten与已有子单并存时，由group协调并等待状态，避免market exit+stop同时卖出。
- 不明保护状态时停止所有新风险，保留已知broker保护并让人工按runbook处理；不得假称“已平仓”。

建议工程观测目标：收到fill后2秒内有保护状态确认，否则P0告警并执行预批准的应急路径。该值不是保证，也不能让无保护交易持续重试到成功。

## 8. 重连supervisor

职责位于新增`core/broker/supervisor.py`/worker编排层，而非策略。

状态：`STOPPED → CONNECTING → IDENTIFIED → SYNCING → READY_PAUSED → ACTIVE`；连接故障进入`DEGRADED/RECONNECT_WAIT`。

退避建议1/2/4/8/16/30秒加jitter、上限30秒、有限连续次数（如10）后等待人工，具体以IB维护时段和错误分类调整。不对身份/权限错误重连风暴。

每次恢复：

1. 获得唯一writer lease和fencing token；确认旧writer/socket已停止。
2. 连接并校验账户/client/nextValidId；新epoch使旧授权无效。
3. 请求账户、合约、持仓、open/completed订单、指定窗口exec/commission；窗口能力需核实。
4. 收集完整屏障，关键回调durable后再对账。
5. 重放inbox/ledger并检查UNKNOWN、风险预占、保护组。
6. 根据1101/1102和当前订阅状态恢复行情；清空旧模式/价格timestamp，不重用缓存假新鲜。
7. 完成恢复报告，状态READY_PAUSED；仅在预批准自动恢复范围内或人工resume后ACTIVE。

仅DB锁无法阻止失联旧进程仍向broker发单。MVP不做双活；进程监督确保单实例、固定client ID，lease丢失立即停止网关，发送前检查token。未来HA需证明旧socket已隔离，不能把DB fencing当broker也支持fencing。

## 9. 对账与歧义处置

| 差异 | 默认动作 | 禁止动作 |
|---|---|---|
| broker-only order | PAUSE，标记归属，人工确认 | 自动接管并认为本策略订单 |
| local pending、broker未见 | 保留UNKNOWN/预占，扩大合法查询证据 | 立刻换ID重发 |
| exec已到、本地投影缺失 | 幂等补ledger/projection并记录修复 | 直接改position数字 |
| position quantity差异 | 新风险禁止，核查manual/corporate action/fill | 用broker覆盖本地消除报警 |
| commission差异 | 补费用revision，重算PnL | 忽略不利费用 |
| 同execId不同经济字段 | conflict/reversal流程，人工审核 | 简单dedupe丢弃 |
| snapshot取消/超时 | 使连接/代次失效，重新建立完整证据 | 使用旧无request ID屏障完成下一次 |

日内增量对账和日终全量对账都必须存在。IB执行历史窗口有限时，用自有durable记录加正式账单对照，不能因broker没返回很久以前成交就删掉账本。

## 10. 测试和落点

EXEC-01 command/dispatcher、EXEC-02 order groups/protection、EXEC-03 supervisor、EXEC-04 recovery reconciliation。每项先fake transport，后明确批准的小额Paper，不写“低价所以一定不会成交”的测试。

至少测试：发送前/后crash、DB提交响应丢失、client ID碰撞、master外部成交、partial fill后断线、cancel/fill竞态、重复佣金、保护拒绝、STP-LMT跳空不成交、两条exit并发、旧epoch callback、暂停期间恢复、writer lease失效。验收矩阵见 [11](11_TEST_AND_ACCEPTANCE.md)。
