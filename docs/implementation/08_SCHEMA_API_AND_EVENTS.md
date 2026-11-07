# 08｜数据库迁移、领域接口、事件与控制 API

版本 1.0｜2026-11-07｜目标契约｜关联：[01 架构](01_SCOPE_AND_ARCHITECTURE.md)、[06 风控](06_PORTFOLIO_AND_RISK.md)、[07 执行](07_EXECUTION_AND_RECOVERY.md)

## 1. 约定与兼容性

本文件描述新增/演进schema，不是已经创建的表。当前已有 instruments/signals/risk_decisions/orders/executions/positions/broker_events/audit_logs/system_controls；新增时优先扩展有明确语义的表，不再建立一个含义相同但不同名的平行真值表。

公共约定：UUID内部标识；UTC-aware timestamptz；金额Numeric(28,10)、字符串JSON十进制；枚举有UNKNOWN而不是默认为成功；account_id每条交易事实必需；version用于乐观锁；JSON schema_version强制；敏感IB账户标识受控存储，对外仅内部account_id。

## 2. 目标数据表字典

### 2.1 身份与运行

| 表 | 关键字段 | 唯一/关联 | 用途 |
|---|---|---|---|
| accounts（新增） | id, broker, encrypted_account_ref, mode, base_currency, account_type, enabled | broker+受控账户指纹 | 与真实账号隔离 |
| instruments（扩展） | 现有字段+primary_exchange, lot_size, min_tick?, rules_version, active_from/to | 内部id；broker conId有效期映射 | 避免symbol重用 |
| contract_resolutions（新增） | instrument_id, con_id, details_hash, market_rules_json, resolved_at, valid_until | instrument_id+revision | 保存资格确认和变化 |
| runtime_runs（新增） | run_id, profile, account_id, client_id, code_hash, config_hash, model_bundle_id, state, started/ended_at | run_id | 一次运行的冻结上下文 |
| writer_leases（新增） | account_id, owner_run_id, fencing_token, expires_at | account_id PK | 单writer；不代替broker隔离 |
| config_versions（新增） | id, canonical_json, sha256, approved_by, approved_at, effective_at | sha256 | 不可无审计改风险 |

### 2.2 账户与账本

| 表 | 字段/重要约束 |
|---|---|
| account_snapshots | id, account_id, epoch, started_at/completed_at/available_at, complete, currency, NAV/cash/settled/available/buying_power, revisions, quality_flags；account_id+completed_at索引 |
| broker_inbox | event_id, account_id, client_id?, connection_epoch, event_type, broker_key?, received_at, payload, payload_hash, processing_state, processed_at, error；重复key但payload冲突不能静默忽略 |
| ledger_entries | id, account_id, event_id, entry_type, instrument_id?, quantity_delta, cash_delta, fee_delta, currency, effective_at, settlement_date?, reversal_of?, sequence；event_id+leg_index唯一 |
| position_lots | account_id, instrument_id, lot_id, source_exec_id, opened_at, original_qty, remaining_qty, unit_cost；是否采用lot视需求，但成本政策固定 |
| positions（扩展） | PK改为account_id+instrument_id；quantity, cost_basis, average_cost, realized/unrealized, market_price, mark_at, ledger_sequence, version |
| cash_balances | account_id+currency PK；economic_cash, settled_cash, reserved_cash, pending_settlement, ledger_sequence, version |
| projection_checkpoints | account_id+projection_name PK；last_sequence, state_hash, rebuilt_at |

为简化MVP，可先用average-cost projection加append-only账本，而不实现复杂税务lot优化；如不用position_lots应在ADR明确，不让两套成本同时作为真值。

### 2.3 信号、风险与订单

| 表 | 扩展/新增重点 | 不变量 |
|---|---|---|
| signals | signal_key、strategy_version、session/decision/source_bar/expiry、feature_snapshot_id、bundle、payload_hash | signal_key唯一；重复异payload冲突 |
| risk_decisions | command_id、account_id、intent_hash、all_rules_json、context_hash、profile_version、decision_at、expires_at | 永久保留拒绝及其证据 |
| risk_reservations | id、command_id、account_id、instrument_id、qty_remaining、cash/gross/risk_amount、slot/group、state、context_revision、version | command_id唯一；UNKNOWN不自动过期释放 |
| order_commands | id、command_key、intent_hash、authorization_id、reservation_id、runtime_run_id、state、fencing_token、created_at、expires_at | command_key唯一，网络重试必须用同key |
| orders（扩展） | account_id、client_id、broker_id_namespace、command_id、group_id、role、broker_status_raw、version、last_event_id、uncertainty_reason | namespace+client_id+order_id在非空时唯一；permId按broker/account作用域 |
| executions（扩展） | account_id、client_id、con_id、payload_hash、revision、correction_of、fee_state、ledger_event_id | execId与修订规则不可随意覆盖 |
| commission_events | account_id、exec_id、revision、currency、amount、received_at、payload_hash | 重复同内容幂等、冲突走修订 |
| order_groups | account_id、position_cycle_id、strategy_id、state、desired/open/protected_qty、group_revision、protection_profile | 同cycle保护逻辑统一管理 |
| order_links | group_id、order_id、role、parent_order_id、oca_ref | 明确entry/stop/take-profit/replacement |

broker_id_namespace用于明确的ID重置时代，不是connection_epoch；普通重连不得生成新namespace逃过唯一约束。历史数据无法确定namespace时，迁移到LEGACY_UNRESOLVED并阻断自动接管。

### 2.4 事件、研究与模型

| 表/存储 | 内容 |
|---|---|
| outbox_events | event_id、aggregate_type/id、aggregate_version、type、payload、created_at、published_at、attempts；在业务事务内写 |
| reconciliation_runs/findings | account/epoch、snapshot_refs、local_revision、complete、finding_code、severity、resolution/evidence |
| dataset_manifests | 02中的manifest及license、sha256；大数据文件在对象/文件存储 |
| feature_snapshots | set_version、decision_at、available_at、dataset_refs、payload/artifact_uri、hash |
| experiments | 完整trial/fold/成本/策略配置、run状态、metrics/artifact refs、review |
| model_bundles | 模型/特征/校准/阈值/配置版本、hash、生命周期、批准记录 |
| predictions | signal_key、model_bundle_id、generated_at、input_hash、score/probability、served_model、quality |
| text_documents/events | publication/first_seen/revision、来源许可、content hash、evidence、模型/提示/schema版本 |
| incidents | severity、detected_at、state、owner、actions/evidence、resolved_at |

高频原始quote和bars不逐tick写OLTP大JSON表；按数据分层存储并保留可定位引用。关键交易回调必须先durable，不因研究存储策略丢失。

## 3. 迁移分期与验证

### MIG-A：账户身份与运行隔离

1. 建accounts/config_versions/runtime_runs/leases。
2. 给订单、成交、positions等增加nullable account_id/client/namespace。
3. 生成回填报告：哪些记录可由已知唯一账户明确归属，哪些不确定。
4. 用户审核归属后回填；无法确认的历史行隔离，不猜DU账号。
5. 添加外键/索引/唯一约束，再改为非空。

### MIG-B：Decimal与账本

新增shadow Numeric列，按明确转换/舍入policy转换float；统计最大误差/不合法行/总余额差异；以broker账单和原始exec校验。写新旧投影的短期对比路径，验收后切主读，保留可回退backup。不可直接回滚已产生真实经济事件的账本；需要reversal或前向修复。

### MIG-C：授权、预占与outbox

扩展risk_decisions、建立reservations/commands/outbox/inbox；旧CREATED不能自动视作合法授权。部署先PAUSED，重建risk state并对账，再开放受限profile。

### MIG-D：研究/模型与文本

只添加研究表、manifest refs与registry，不接入writer权限。数据量较大时单独研究DB/schema；不让训练重查询锁住交易表。

每个迁移测试：空库升到head；从上个正式版本升级；约束冲突与未知归属拒绝；大样本转换时间；schema check；备份恢复。**迁移命令不能在代理任务里未经批准对用户现有库执行。**

## 4. 事件 Envelope

```json
{
  "schema_version": "event.v1",
  "event_id": "uuid",
  "event_type": "execution.recorded",
  "aggregate_type": "order",
  "aggregate_id": "order-uuid",
  "aggregate_version": 7,
  "account_id": "paper-account-1",
  "runtime_run_id": "run-id",
  "correlation_id": "signal-id",
  "causation_id": "broker-inbox-event-id",
  "event_at": "2026-11-09T15:01:00Z",
  "received_at": "2026-11-09T15:01:00.080Z",
  "available_at": "2026-11-09T15:01:00.100Z",
  "payload_hash": "sha256",
  "payload": {"quantity": "1", "price": "100.00", "currency": "USD"}
}
```

event_at可未知但必须quality标注；不能用received_at填补后假装是真正交易所时间。broker事件和域事件分别保存，后者不覆盖前者。消费者至少一次交付，按event_id幂等；aggregate_version防乱序覆盖，缺版本先补齐或重建。

## 5. 关键领域类型

- `SignalResult = StrategySignal | NoSignal`。
- `RequestedTrade`：signal/target refs、account、instrument、side、requested_qty、worst_price、stop/protection refs、reduce_only、expiry、intent_hash。
- `RiskEvaluation`：APPROVED/REJECTED、approved_qty、rules、snapshot refs、risk profile，不执行I/O。
- `OrderAuthorization`：持久化risk_decision/reservation/command refs、intent hash、gate generation、fencing token、expires_at。
- `ExecutionPlan`：合约资格、合法价格增量后的entry/child orders、group semantics、允许订单类型。
- `PortfolioView`：account、ledger revision、positions/cash、effective reservations、mark timestamps、quality。

现有 `LimitOrderIntent` 没有account/stop/group字段；不要把metadata任意dict塞进去绕过类型。应新增上层trade/authorization类型，再显式映射wire order。现有 `PreTradeCheck.authorize` 是校验seam，不是完整风险服务，迁移语义见06。

## 6. API：现有保留、新增单独标注

### 6.1 保留现有

`GET /health`, `GET /api/system/status`, `GET /api/market/{symbol}`, `/ws/market`, `POST /api/system/{kill,pause,resume}`。现有字段语义不能静默改成“已交易”；新增详细readiness和权限时使用versioned response或兼容增加字段。

### 6.2 新增目标端点

| 方法/路径 | 权限 | 语义 |
|---|---|---|
| GET /api/v1/readiness | viewer | 逐门禁状态、证据时间、reason codes |
| GET /api/v1/accounts/{id}/snapshot | operator/auditor | 脱敏快照，不泄露broker原始账号 |
| GET /api/v1/portfolio | operator/auditor | 持仓、预占、保护状态、数据龄 |
| GET /api/v1/orders/{id} | operator/auditor | lifecycle、group、fills、uncertainty |
| POST /api/v1/runs | operator | 创建待审运行；不隐含ACTIVE |
| POST /api/v1/runs/{id}/approve | approver | 指定profile/hash/额度/有效期审批 |
| POST /api/v1/order-commands | approved operator | 写durable command，经授权pipeline；不直接send |
| POST /api/v1/orders/{id}/cancel | operator | 写cancel command；不因kill禁用 |
| POST /api/v1/portfolio/reduce | emergency operator | 仅预批准reduce-only；不可负仓 |
| GET /api/v1/reconciliations/{id} | auditor | 差异与证据 |
| POST /api/v1/reconciliation-actions | approver | 可审计的修复请求，非直接清UNKNOWN |
| GET /api/v1/experiments/{id} | researcher | 研究manifest与结果 |
| POST /api/v1/model-bundles/{id}/approve | approver | 上线候选；不热替换运行中bundle |

reader/researcher/operator/approver角色分离。个人MVP可以同一人承担两角色，但审批步骤和审计仍分开，不能由策略调用自己的approve。

### 6.3 mutation通用契约

请求携带`Idempotency-Key`、auth、reason、expected_version；服务器由身份认证得到actor，不能相信body自报actor。相同key+相同body返回原结果；相同key+不同body返回409。

命令成功入库返回202和command_id/查询路径，不返回“已成交”。无权限401/403；状态版本冲突409；输入非法422；依赖不可用503；风控拒绝返回结构化业务拒绝而非伪装HTTP500。

kill含义为停止新风险并触发未成交入场的取消请求，**不自动卖出全部仓位，也不撤掉保护**。flatten是不同端点/权限/profile，需要明确价格与可成交性风险。

### 6.4 命令请求与异步响应示例（目标API）

请求头含`Idempotency-Key: operator-run-001-entry-001`，body只引用已批准运行和业务意图，不允许传`bypass_risk=true`：

```json
{
  "schema_version": "order_command.v1",
  "runtime_run_id": "approved-paper-run-id",
  "account_id": "paper-account-1",
  "signal_id": "signal-id",
  "instrument_id": "instrument-id",
  "side": "BUY",
  "requested_quantity": "1",
  "maximum_entry_price": "100.00",
  "reduce_only": false,
  "expected_portfolio_version": 42,
  "reason": "Approved supervised paper validation"
}
```

202只代表接收并durable排队，审批/执行的最终状态要查询：

```json
{
  "command_id": "command-id",
  "state": "RECEIVED",
  "order_id": null,
  "broker_acknowledged": false,
  "poll_url": "/api/v1/order-commands/command-id"
}
```

拒绝示例：

```json
{
  "command_id": "command-id",
  "state": "REJECTED",
  "decision": "REJECTED",
  "rules": ["ACCOUNT_SNAPSHOT_STALE", "TRADING_PAUSED"],
  "retryable_after_refresh": true,
  "automatic_resubmit": false
}
```

需同时实现`GET /api/v1/order-commands/{id}`，返回RECEIVED/AUTHORIZED/DISPATCHING/ACKNOWLEDGED/REJECTED/UNKNOWN等命令状态及对应order refs。对于已REJECTED的同key重试继续返回原拒绝；要重新评估必须明确新意图修订/新key并通过所有门禁，不暗中改变原命令含义。authorization与reservation由服务器生成，客户端不能自填一个UUID充当批准。

## 7. Outbox、缓存与故障事务

outbox发布失败可重试；Redis通知丢失不丢业务事实。缓存consumer从database checkpoint补齐，API显示cache_age/revision。不得在DB交易前先Redis宣布“订单已批准”。

权威边界：资金/风险/订单/ledger在PostgreSQL；Redis只读模型/通知；研究文件immutable版本；SDK当前内存不是历史事实的唯一来源。

关键事务测试：commit前crash、commit后publish前crash、publish后mark前crash、consumer处理后ACK前crash、版本gap、重复outbox event、同key异payload、fencing失效。

## 8. 交付与review清单

交付：迁移ADR和DDL、字段/单位字典、JSON Schema、OpenAPI快照、事件兼容测试、数据库事务集成测试、回填/恢复报告。所有新增接口有拒绝例子和permission test，不只happy path。任务落点见10，验收见11。
