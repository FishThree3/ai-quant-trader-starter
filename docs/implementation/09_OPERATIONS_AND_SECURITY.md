# 09｜部署、权限、安全、监控与运行手册

版本 1.0｜2026-11-07｜目标设计｜关联：[07 恢复](07_EXECUTION_AND_RECOVERY.md)、[10 预算](10_ROADMAP_AND_BACKLOG.md)、[11 验收](11_TEST_AND_ACCEPTANCE.md)

## 1. 环境拓扑

MVP分三个逻辑环境：

- development：fake broker、临时数据库、合成测试数据，不读实际账户凭证。
- research/replay：历史版本数据、研究DB、无broker网络权限。
- paper-runtime：固定IB模拟账户、独立交易DB、Redis、单worker、只读监控/API与受控操作入口。

不要以不同.env文件名代替权限隔离：研究进程不能访问交易DB写入凭证或IB socket；LLM进程只能读获准文本对象。

部署先选择一个可维护主机（Windows本机TWS或受支持的Linux/Gateway部署均需验收），不在第一次上线就做跨区双活。API/worker/data/research可独立进程，但交易writer只能一个。Docker用于依赖服务和可复现环境；官方IB SDK安装来源、TWS/Gateway版本和登录维护需要单独管理。

## 2. 配置与Secrets

配置冻结到runtime_run：trading/profile/account internal id/client id、data modes、universe/calendar版本、risk limits、TTL、保护类型、model bundle、代码/依赖hash、user approval expiry。

Secrets来源为环境注入或系统secret store；不写git、不放README截图、不发LLM、不进入结构化日志。敏感字段日志redaction基于allowlist，异常消息也可能含账号/订单，应清洗后对外展示。

当前`CONTROL_API_TOKEN`共享secret可用于本机MVP，但上线应迁移到有身份与role的认证。API默认仅127.0.0.1；如远程使用，需TLS、VPN/受控入口和身份认证，不把裸uvicorn的控制API公开到互联网。

依赖锁版本、官方来源和hash；不得加载陌生pickle模型。运行container非root、最小文件写权限，用户上传/下载文件在隔离目录解析，禁止从不可信目录运行解释器/构建器。

## 3. 权限矩阵

| 能力 | researcher/LLM | viewer | operator | approver |
|---|---|---|---|---|
| 读脱敏指标/研究结果 | 允许按scope | 允许 | 允许 | 允许 |
| broker账号/凭证 | 禁止 | 禁止 | 最小必要 | 最小必要 |
| 创建候选实验 | 允许 | 禁止 | 可选 | 可选 |
| 修改生产risk profile | 禁止 | 禁止 | 提案 | 审批后版本发布 |
| kill/pause | 禁止自动调用 | 禁止 | 允许 | 允许 |
| resume/运行授权 | 禁止 | 禁止 | 在批准profile内请求 | 批准 |
| cancel/reduce-only | 禁止 | 禁止 | 依应急profile | 依应急profile |
| 发布active model | 提交候选 | 禁止 | 不可自批 | 审批并下个session切换 |

任何mutating操作记录authenticated actor、reason、before/after、config hash、request/idempotency key。AI生成的理由不是人工批准。

## 4. 监控指标与告警

本文件定义指标语义，不设计dashboard样式。

| 类别 | 指标 | 建议初始告警 | 动作 |
|---|---|---|---|
| broker | connection state/epoch、last callback age | 非READY且有风险 | 停新单、P1；无保护升P0 |
| 行情 | 每侧quote age、mode、bar gaps | BBO>10s/延迟/冻结 | 禁该标的新入场，不冒用last |
| 账户 | snapshot age/complete/revision | >15s或缺必要字段 | 全账户禁新风险 |
| 订单 | UNKNOWN数、pending ACK age、cancel age | 任何UNKNOWN | 保留预占、暂停与人工排查 |
| 保护 | open_qty−confirmed_protected_qty | >0且确认超2s | P0、执行预批准应急流程 |
| 风控 | daily loss、gross、reservations、gate epoch | 超配置/预算冲突 | 阻断、告警、审计 |
| worker | heartbeat age、lease、queue depth、event-loop lag | lease失效/关键队列满 | 停止网关，不能丢关键事件 |
| 数据库 | write latency/errors、replication/backup age | 无法durable写 | 禁send、保留broker保护 |
| 模型 | input drift、missing、served model、latency | 未批准版本/输入不合法 | quarantine，按bundle降级 |
| LLM成本 | token/day、cost/day、abstain/error | 达预算80%/100% | 预警/停止新文本任务 |
| 对账 | blocking findings、last success age | 任一阻断 | PAUSED，不自动修成0 |

值是初始配置，需要在paper观察后校准；关键安全门禁与告警发送失败要分开：通知坏了不能让交易gate保持开。

日志必须可按run_id→signal→risk decision→command→order→execution→ledger串联。关键审计不可采样；大量market ticks日志可以汇总，原始数据有独立存储。

## 5. 每日运行SOP

### 5.1 开盘前（按交易所时间，至少提前30分钟）

1. 确认模拟登录、API端口、只读/可写profile与本次批准范围。
2. 检查数据库/Redis/磁盘/时钟偏差/备份；读取完整configuration hash。
3. 验证唯一writer、client ID、账户；刷新合约和日历，包括半日市。
4. 同步账户/持仓/订单/成交/费用，完成对账；未知差异不得跳过。
5. 加载已批准策略/模型bundle；artifact hash和特征版本一致。
6. 获取实时BBO和bar warmup，检查mode/授权/数据缺口。
7. 生成preflight report。所有门禁绿且操作者确认后，才能按批准profile ACTIVE。

### 5.2 盘中

持续监控暴露/保护/quote/account freshness、异常订单与队列。任何人工操作通过command API或有审计的TWS应急步骤；发生TWS手动交易后须暂停并重新对账。禁止一边运行一边临时改代码或model文件。

### 5.3 收盘前/收盘后

`close-35m`停止新入场候选；`close-10m`进入flatten目标。退出尝试必须不超卖、处理剩余limit未成交。close时如仍有持仓，明确记录OVERNIGHT_EXCEPTION并告警，不能把程序“想平仓”当成已经平。

收盘后：等候最终回调/费用→对账→生成日损益和异常报告→冻结session数据manifest→备份→停止策略/保持必要监控→人工签运行卡。RTH结束不代表所有故障都能关闭工单。

## 6. 故障Runbook

### RB-01 IB断线

停止新单；不要取消broker-native保护；所有本地授权失效；保留未完成命令/预占；监督重连并完整对账，恢复到READY_PAUSED；无法恢复且有风险通知人工TWS接管。重连次数耗尽不不停重试。

### RB-02 DB不可写

停止一切新增风险发送；不要认为内存有记录就能继续。已成交/保护状态继续尽可能观测；将必要紧急操作走预批准应急路径并后补正式审计，明确操作证据。数据库恢复先重建ledger/订单，不直接resume。

### RB-03 Redis不可用

Redis只是cache/通知；关键风控从durable事实读取。当前gate依赖Redis健康时保持PAUSED，不能为“恢复服务”注入AlwaysActive。恢复后检查consumer offset和quote freshness。

### RB-04 UNKNOWN订单

先确认account/client/orderRef/permId/执行窗口与raw events；查询broker当前/完成订单和成交证据；保留预算。无法证明未send，不重发。人工修复必须提交resolution action与证据，而不是SQL把status改REJECTED。

### RB-05 未保护/超卖风险

P0，暂停全部新入场；查看已确认仓位和所有sell子单；按group协调取消/替换，无法确认时转人工TWS。不得盲目再发一个SELL“保险”。应急订单类型和价格policy在事故前批准。

### RB-06 数据异常/模型漂移

隔离标的或bundle；保留原始数据；不自动修补为零或改feature阈值。存量仓位由独立退出逻辑维护。研究复现异常后发布新版本，而非在线改模型。

### RB-07 主机重启/双worker

默认PAUSED；确认旧socket/writer已停止；固定client、lease/fencing与单实例锁；完整重放/对账。数据库lease过期不等于旧进程不再发单，未能隔离旧writer则禁止启动新writer。

## 7. 备份、恢复与RTO/RPO

建议工程目标：交易DB启用连续WAL归档或等价恢复能力，日备份和加密异地副本；研究immutable数据按manifest保留；密钥单独备份。开发SQLite备份不能代表PostgreSQL恢复验收。

初值RPO目标≤1分钟、人工协助RTO≤30分钟，用恢复演练验证而不是承诺。对外订单在灾难窗口内仍可能发生，恢复后broker对账必做；即使RPO=0也不等于与broker完全一致。

每月至少一次恢复到独立隔离环境：校验账户余额/ledger hash/订单状态/运行历史，禁止恢复环境自动连接broker。记录实际耗时、丢失窗口、发现问题和负责人。

数据保留按许可与隐私要求：关键ledger/order/audit长期保存，具体法定期限由专业审核；market/news原文按供应商许可；LLM输入避免不必要个人/账户信息，保留policy需与provider契约一致。

## 8. CI/CD与变更

CI：格式/lint→类型/架构依赖检查→单元/property→SQLite快速集成→PostgreSQL事务集成→replay golden→migration→artifact/signature→安全扫描。默认CI无broker/付费LLM访问；外部paper验收独立需审批。

部署：PAUSE→确认未完成订单/保护→备份→迁移→部署固定artifact→重放/对账→preflight→人工resume。回滚应用版本前检查schema兼容；交易事实不随代码回滚抹掉，重大ledger问题前向修复。

当前分支已有未提交重构；此轮文档不替用户commit。后续开始实现需先明确基线、独立branch/小PR和review，避免再次堆一个无法审查的巨型变更。

## 9. 安全与合规核查

核查数据许可、账户类型与交易权限、税务/结算、所在辖区自动交易/投资服务规则、市场操纵防线。自用模拟研究不等于获准替第三方管理资产。禁止spoofing、wash trading、规避交易限制或隐瞒自动化来源的设计。

US结算/日内交易规则及IB费用/权限可能变化，本次外部文档获取受限，没有宣称已核实某个具体数字；上线前填写12参考复核表，并由适合的专业人士确认适用要求。

## 10. 验收

OPS-01部署与secrets、OPS-02监控/审计、OPS-03备份/恢复与runbook；QA-03做故障演练。交付preflight、日运行卡、incident演练记录、备份恢复实测与权限测试。无人能按手册恢复时，不进入PAPER_AUTOMATED。
