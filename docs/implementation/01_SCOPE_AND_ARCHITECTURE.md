# 01｜目标、范围与系统架构

版本 1.0｜2026-11-07｜类型：目标设计｜上级：[总索引](README.md)

## 1. 成功定义：分开工程正确性与策略有效性

“完整 AI 交易算法”至少满足四种完整性：

1. **研究完整**：有经济/行为假设、时点正确数据、明确的训练/验证/测试边界；有费用、滑点、基准、否证结果。
2. **交易完整**：从信号、仓位目标、风险审批、预占、下单到部分成交、费用、退出、账本、对账可闭环。
3. **运营完整**：断网/重启/数据失效/模型失败可安全降级；有人知道系统为何暂停、如何恢复。
4. **治理完整**：每个运行版本绑定代码、参数、模型、数据、权限及审批；没有“AI 自己更新自己然后继续交易”。

工程完整不等于策略有正收益；Paper 有正收益不等于实盘可复制。研究证据不足时，正确结果就是不晋级、不交易。

## 2. 产品范围

### 2.1 MVP 必做

- 账户：一个 IB 模拟账户，一个固定 client ID，单执行写入者。
- 资产：美国上市、USD 计价、可明确解析 conId 的高流动性股票/ETF；白名单由配置维护。
- 方向：只做多；卖出只允许减少已确认的多头，不允许穿零做空。
- 交易时段：RTH，使用市场日历；不开盘前/盘后仓位。
- 频率：1m 数据构建 5m 策略；不做 HFT、做市、tick 级抢单。
- 策略：一条确定性基准策略，然后一个 ML filter，再做 LLM 增量特征实验。
- 操作：人工启动一次 session，系统可在该批准范围内运行；重大故障后需要人工恢复。
- 证据：可重放事件日志、可核算日损益、研究/模型/运行卡及验收包。

### 2.2 明确不做

期权/期货、杠杆/融券、多币种、多账户资管、高频延迟竞赛、自动新闻爬取绕过授权、在线强化学习自动改仓位、生产自改代码、自动实盘切换。后续扩展逐项 ADR，不得借用“通用接口”默默扩大风险范围。

### 2.3 非功能目标（建议起始 SLO，不是现有实测）

| 维度 | 初始目标 | 失败处置 |
|---|---|---|
| 身份/状态正确性 | 重放不得重复外部发送；成交不得重复记账 | 任何违规 P0、阻断全部新单 |
| 时点一致性 | 特征 available_at 不晚于 decision_at | 该实验作废/该信号拒绝 |
| 本地决策延迟 | bar finalized 后至 command persisted，p95 ≤1s | 过期信号拒绝，不追单 |
| 风控运算 | 数据已在本地时 p95 ≤100ms | 超预算暂停开仓；不牺牲校验 |
| 持久化 | 外部发送前 durable intent/reservation/ID | DB 写失败绝不发送 |
| 可恢复性 | 新进程必须先完整对账 | 恢复失败保持 PAUSED |
| 可解释性 | 每个订单能还原全部输入版本和拒绝原因 | 缺失 lineage 不晋级 |

供应商回调延迟、市场无报价和网络故障不能承诺硬实时；这些 SLO 是观察目标，交易入口使用显式 TTL 而非平均延迟。

## 3. 现有能力与缺口

| 领域 | 现有代码落点 | 当前实际能力 | 下一步 |
|---|---|---|---|
| 配置 | `config/settings.py` | PAPER-only、只读默认、有限数值检查 | 配置版本/运行快照、profile 审批 |
| IB 连接 | `core/broker/ibkr/connection.py` | 账户/回调/屏障、连接锁与拒绝边界 | supervisor、请求预算、代次和恢复编排 |
| 行情 | `core/market/models.py`, `service.py` | BBO、按侧时间、延迟/冻结拒绝 | 合约资格、bars、历史采集、日历和质量 |
| 风控 | `core/risk/engine.py` | 基础规则与 stop-risk | 账户购买力/持仓/预占/减仓/决策落库 |
| 订单 | `core/execution/order_manager.py` | UUID/IB ID 幂等、状态折叠 | command dispatcher、保护订单组、金额和账户命名空间 |
| 对账 | `core/reconciliation/` | 不明状态拒绝放行 | event replay、成交修正、局部投影修复 |
| 表模型 | `database/models.py` | signal/risk/order/execution/position 等 | 08 中的版本、账户、账本、预占、outbox |
| 策略/组合 | `core/strategy`, `core/portfolio` | 目前基本为空 | 03/06 的确定性域逻辑 |
| API/运维 | `apps/api`, `apps/worker` | 控制/只读查询、心跳 | command 审批/运行注册/告警/恢复手册 |
| AI/回测 | 无完整模块 | 不存在经过验证的闭环 | 04/05，从离线开始 |

基线来自当前未提交工作树，不能用原 `master` commit 代表全部重构。实施第一步应在获准后保存明确代码基线与测试证据，文档编写本身不提交代码。

## 4. 推荐架构：模块化单体 + 独立研究进程

### 4.1 进程职责

| 进程 | 可以读 | 可以写 | 明确禁止 |
|---|---|---|---|
| API/control | status、审计、运行记录 | 带审批的 command、控制状态 | 直接实例化 broker 发单 |
| trading worker | 合约、行情、账户快照、版本化策略 | 订单/预占/账本/outbox；唯一 broker writer | 自行训练、修改 active model |
| data worker | 经许可供应商输入 | raw/curated/manifest、质量报告 | 下单、修改账本 |
| research worker | 指定版本数据、费用模型 | 实验、预测、候选 model artifact | production DB 写入、broker 凭证 |
| text/LLM worker | 授权文本、脱敏元数据 | 结构化事件、评测记录 | 下单、风险参数、任意 URL/脚本 |
| monitor | 心跳、指标、脱敏日志 | 告警事件 | 用“重启一切”恢复交易 |

V1 不部署 Kafka/Kubernetes，不以微服务数量衡量成熟度。PostgreSQL 保存交易事实；Redis 做报价缓存/通知，不保存唯一风险事实；本地/对象存储保存 immutable 研究数据。单 worker 所有写入通过明确的应用服务协调，域层不依赖 HTTP 或 SDK。

### 4.2 目标新增模块

- `core/contracts/`：共享类型、事件 envelope、Decimal 单位与版本。
- `core/data/`：日历、历史 provider、raw/curated、质量、bar builder。
- `core/strategy/`：纯函数策略、signals、调度/冷却、目标仓位。
- `core/features/`：时间可见性检查、同源特征图、在线状态。
- `core/research/`：实验规范、walk-forward、评估与 registry。
- `core/backtest/`：replay clock、事件队列、sim broker、成本模型。
- `core/portfolio/`：账户读模型、持仓/现金账本投影、目标与 reservations。
- `core/risk/`：纯评估器 + 有事务的 authorization service。
- `core/execution/`：commands、dispatcher、orders、groups、protection。
- `apps/research/`, `apps/data/`, `apps/text/`：进程入口；只是规划路径。

不得一次全建空类。按 10 的切片先让一个垂直闭环有用，再抽象。

## 5. 主交易时序（目标）

1. data/market 接收原始信息，标注 event_at、received_at、available_at、source 和 revision。
2. bar builder 只将结束且完整的 5m bar 标为 finalized；有缺口则不给该标的出信号。
3. feature service 用 decision_at 可见数据生成 immutable snapshot。
4. Strategy.evaluate 返回 Signal 或明确 NoSignal；两者都记录版本和 reason code。
5. ML/LLM 结果只在已注册 bundle 的条件内影响 score/filter；缺失按该 bundle 的固定降级策略处理。
6. portfolio 把 Signal 转换成 RequestedTrade/Target，判断是新增风险还是 reduce-only。
7. RiskAuthorizationService 在账户级事务锁下，读取新鲜快照、未完成订单和所有有效 reservations，审批并预占。
8. 同一事务写 OrderCommand、授权、outbox；提交之后 dispatcher 才可发送。
9. 发送前重新检查授权期限、账户 revision、gate generation、quote ID 与 broker 状态；失效则拒绝或重新完整审批，不就地放宽。
10. broker 回调首先 durable 入 inbox；投影器更新订单/成交/费用/持仓/预占，提交后发布通知。
11. 保护单组根据已确认累计成交维护保护数量；不能确认保护就暂停新开仓并告警。
12. 日内及恢复时对账；收盘结束生成运行记录、残余暴露和人工待办。

Signal、授权、命令、broker receipt、execution 是不同对象，不以“submit 返回 UUID”代替成交或接受确认。

## 6. 运行模式与状态

`RESEARCH`（离线，无 broker）→ `REPLAY`（仿真 broker）→ `SHADOW`（真实数据，只产生意图，不发）→ `PAPER_SUPERVISED`（用户批准的小额模拟单）→ `PAPER_AUTOMATED`（满足保护/恢复/运营门槛）。

这些是目标 RuntimeProfile，不修改当前 TradingMode 枚举以假装已有实现。**LIVE 不属于本阶段状态图。** 若未来立项，必须有独立凭证、独立库、单独 risk profile、明确用户授权和司法辖区检查。

运行健康维度分开保存：

- broker：DISCONNECTED / SYNCING / READY / DEGRADED。
- gate：PAUSED / ACTIVE / KILLED。
- data：MISSING / DELAYED / STALE / HEALTHY。
- risk context：INCOMPLETE / STALE / CONSISTENT。
- model：DISABLED / SHADOW / ACTIVE / QUARANTINED。
- effective_can_open = 所有开仓条件为真；effective_can_reduce 独立且不能穿零。

不要用一个绿色 health 指示替代这些条件。API 展示的是事实、证据时间、拒绝原因集合，不推测健康。

## 7. 核心不变量

- INV-01：无完整新鲜授权、持久化订单/预占与有效运行权限，不得发新风险订单。
- INV-02：同一 command_key 最多发起一次**新的**订单意图；不承诺网络 exactly-once，歧义只能对账。
- INV-03：每个 broker execId/revision 对账本只生效一次；修正用 reversal，不改历史。
- INV-04：UNKNOWN/CANCEL_PENDING 不释放未确认风险；取消请求不是取消完成。
- INV-05：策略/模型/新闻不得直接访问执行网关。
- INV-06：所有特征和文本证据在 decision_at 之前确实可用；历史下载时间不能冒充发布时间。
- INV-07：重启先重放/对账后恢复；不自动 ACTIVE；失去 writer lease 后不能发送。
- INV-08：reduce-only 永不增加方向暴露，不因总仓位/日损上限机械阻止必要减仓。
- INV-09：保护订单不等于价格保证；不能把计划中的止损当成 broker 已挂的止损。
- INV-10：模型增益未知时保持基准/shadow，不降低门槛。

## 8. ADR 与阶段验收

本文件冻结范围与依赖；08 冻结公共字段；03 冻结策略。任何跨模块更改需 12 的 ADR：原因、替代方案、迁移、回滚、测试和负责人。

交付 A1：一份可执行 RuntimeProfile schema、一个无 broker 的端到端 event replay，以及一份“没有哪些能力就不能 PAPER_AUTOMATED”的机器可读 gate report。验收案例和阶段 G0–G6 在 [11](11_TEST_AND_ACCEPTANCE.md)，任务在 [10](10_ROADMAP_AND_BACKLOG.md)。
