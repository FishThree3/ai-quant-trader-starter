# 11｜测试策略、阶段门禁与Paper验收

版本 1.0｜2026-11-07｜验收规范｜关联：[10 路线](10_ROADMAP_AND_BACKLOG.md)、[12 模板](12_DECISIONS_TEMPLATES_REFERENCES.md)

## 1. 当前证据与未来验收分开

现有重构已跑出482 passed、3项外部集成默认skip，显式只读Paper连接/快照1项通过；这只覆盖现有功能，不代表本套规划通过。未来新增测试必须绑定code/config/data版本，不能在报告中沿用旧数字。

测试结果类型：PASS、FAIL、SKIPPED_BY_POLICY、BLOCKED_ENVIRONMENT、INCONCLUSIVE。被跳过和环境阻塞不等于通过；策略证据不足不等于代码bug，但都不能自动晋级。

## 2. 测试层级与执行环境

| 层级 | 环境 | 验证内容 | 外部副作用 |
|---|---|---|---|
| unit | fake clock/transport，内存 | 算法、规则、状态折叠、单位 | 无 |
| property | 随机合法/非法事件序列 | 账本/风险/幂等不变量 | 无 |
| DB integration | 临时SQLite+临时PostgreSQL | 锁/事务/唯一约束/迁移 | 只写临时库 |
| contract | 固定官方SDK对象/序列化样本 | 回调签名、conId/price/qty/字段 | 无socket |
| replay golden | immutable历史/合成fixtures | 同输入同决策/账本/事件hash | 无 |
| fault injection | fake故障点、可控进程重启 | crash、超时、乱序、断网、重复 | 无真实订单 |
| read-only IB | 显式启用 | 账户/行情/快照/SDK兼容 | 无订单mutation |
| supervised Paper | 用户逐范围批准 | 小额实际Paper订单/保护/撤单 | 有模拟账户副作用 |
| soak | 已批准Paper profile | 多日可靠性/运营/模型表现 | 有模拟账户副作用 |

默认pytest/CI只跑无账户副作用层。不能在普通pytest因为.env里一个旧ENABLE变量就意外发单；外部订单测试需独立入口+明确flag+批准run profile+账户确认。

## 3. 不变量测试矩阵

| 测试ID | 场景 | 预期 | 对应模块 |
|---|---|---|---|
| TEST-DATA-01 | t bar尚未available | 不可产生依赖该bar的signal | data/features |
| TEST-DATA-02 | 五根1m缺一根 | 5m incomplete，不插值成交 | bar builder |
| TEST-DATA-03 | future/NaN/delayed BBO | 开仓拒绝，reason明确 | market/risk |
| TEST-DATA-04 | 同事件新revision晚到 | 历史as-of不被改写 | data repository |
| TEST-ACCT-01 | 新cash+旧NAV+旧positions | 不构造完整context | account service |
| TEST-ACCT-02 | account/epoch不一致 | 拒绝授权 | risk |
| TEST-LEDGER-01 | 同exec重复N次 | 经济效果一次 | inbox/ledger |
| TEST-LEDGER-02 | fee晚到/修正 | 费用及PnL恰好修订一次 | ledger |
| TEST-LEDGER-03 | 部分卖出/拆股/分红 | 成本/现金/数量守恒 | projection |
| TEST-LEDGER-04 | rebuild checkpoint | 与全量重放hash一致 | projection |
| TEST-RISK-01 | 并发共享资金 | 总预占不超预算 | authorization |
| TEST-RISK-02 | authorize两次同key | 一份reservation | authorization |
| TEST-RISK-03 | 超日损但合法减仓 | reduce-only可执行，不穿零 | risk |
| TEST-RISK-04 | UNKNOWN已发送订单TTL到 | 不释放预算、不重发 | risk/execution |
| TEST-RISK-05 | 已有持仓+挂单+新单 | 按合计单标的/gross限制 | risk |
| TEST-EXEC-01 | send前/后crash | 可证明未send才可发；否则UNKNOWN | dispatcher |
| TEST-EXEC-02 | cancel与fill乱序 | 不少记fill、不超卖 | state machine |
| TEST-EXEC-03 | foreign client同orderId | 不错归属 | broker adapter |
| TEST-EXEC-04 | 保护拒绝/部分保护 | P0和禁新风险 | order group |
| TEST-EXEC-05 | 退出+stop同时触发 | 所有可成交SELL不超long | group/coordinator |
| TEST-REC-01 | 取消snapshot后晚屏障 | 不完成下一epoch | connection |
| TEST-REC-02 | 1100与READY竞态 | 故障不能被覆盖 | connection |
| TEST-REC-03 | 旧writer lease仍有socket | 隔离前不启动新writer | supervisor |
| TEST-AI-01 | text含恶意交易指令 | 只抽取，不能调用broker | text worker |
| TEST-AI-02 | fabricated数字/证据越界 | abstain/reject | schema validator |
| TEST-AI-03 | unexpected served_model | shadow/quarantine | registry |
| TEST-AI-04 | 模型失败/付费预算耗尽 | 禁新模型信号，保护继续 | runtime |
| TEST-OPS-01 | DB/Redis/告警故障 | 不假READY，不丢关键审计 | worker/API |
| TEST-OPS-02 | backup restore | 隔离恢复、不自动连接IB | operations |
| TEST-API-01 | 幂等key同body/异body | 原响应/409 | command API |
| TEST-API-02 | viewer发resume/cancel | 403且无mutation | auth |

Property testing建议随机生成重复/乱序/有限延迟事件，限定合法broker语义与明确的invalid cases。测试不能对任意荒谬事件序列要求“总能继续交易”；不确定时正确结果是阻断。

## 4. 崩溃点矩阵

至少在以下边界主动终止进程再恢复：

1. risk决策计算后、事务提交前。
2. reservation/command提交后、outbox发布前。
3. broker ID持久化后、socket write前。
4. socket write后、任何ACK前。
5. fill收到后、inbox写入前（broker恢复窗口用于弥补，不能证明零丢失）。
6. inbox提交后、ledger投影前。
7. ledger/projection提交后、ACK/通知前。
8. cancel发送后、确认前。
9. 保护replacement期间。
10. model bundle切换/数据manifest发布的原子边界。

每个边界记录：恢复后订单、现金、持仓、预占、保护状态、是否可新开仓、所用证据。测试只校验最终数量不够，还需校验没有第二次外部新单。

## 5. 阶段Gate G0–G6

### G0：基线、范围与权限

必须：已批准scope/profile、明确账户与操作者、环境锁、代码基线、单writer方案、数据许可清单、所有当前回归通过。对已有库的迁移有备份/批准流程。

不得：把“IB已经打开”视为自动下单授权；因为要快而关闭只读/风控。

### G1：数据/合约/账户可信

必须：可复现manifest、日历/合约qualification、无未来/单位未知、完整AccountSnapshot契约、实时BBO在真实交易时段验证、质量报告和数据许可。

未获得实时行情时BLOCKED，不用延迟数据冒充；可以继续离线研究，但不可进Paper自动交易。

### G2：安全交易闭环工程正确

必须：Decimal ledger、购买力/预占、inbox/outbox、订单身份、保护组、恢复/对账、reduce-only、权限隔离、上述故障矩阵全部通过。PostgreSQL竞争测试必需，不能只拿SQLite成功代替。

受控Paper机械测试可在此后申请，用于证实order group/保护语义；批准范围与策略上线批准分离。

### G3：确定性策略研究证据

必须：03规则冻结，05的outer OOS≥252日且≥200 round trip、成本/压力/基准/多重尝试报告、无泄漏，评审明确PASS。收益/置信区间/回撤门槛按05的预注册值。

不满足为FAIL或INCONCLUSIVE；若改策略，必须新版本/新试验登记。G3不是未来收益保证。

### G4：AI增量与模型治理

数值ML：与基准/随机过滤/简单规则比较、概率校准、配对OOS改善证据、registry/rollback/漂移测试。LLM：抽取gold质量与权限安全先通过；历史回测仅探索，前瞻冻结后的数据才支持正式增益主张。

允许数值ML先完成G4，LLM保持DISABLED/SHADOW；此时不得宣传“LLM已贡献收益”。若AI无增益，可发布非AI基准工程，但不是完整AI增强验收。

### G5a：Shadow parity

至少10个完整交易日（观察下限），全部候选、拒绝、模拟command、预测、账户/quote lineage保存；无broker mutation；与同输入离线重放的决策一致。日历特殊情况需合成replay补测，不能为了等一年DST而放弃测试。

检查数据漂移、真实输入缺失、latency、信号过期与模型fallback。异常修复后重新积累足够无重大事故证据；shadow未成交不用于冒充真实fill统计。

### G5b：有人值守Paper与soak

第一批额外帽子：每个entry≤1整股且≤500USD名义额，总风险限额更严者生效；所有保护/退出类型必须预批准。高于500USD的标的跳过，不把帽子升高。

随后任何扩大Paper范围都更新profile审批。最低观察：30完整交易日且100完成round trip，两者都满足；同时零未解决P0/UNKNOWN/账本不平/无保护暴露。出现交易低频时延长观察，不能为了凑100加单。

### G6：自动Paper发布

必须：G0–G5适用项PASS，版本冻结，完整preflight、on-call、backup restore、kill/cancel/reduce演练、模型失败/断网/DB故障演练、每日回顾机制。发行后也持续gate，不以一次验收永久允许。

**G6只授权批准范围内的模拟交易，不授权实盘。** 实盘需要独立立项、风险/法律/账户/运营核查与用户明确授权，不在本规划默认开放。

## 6. 有人值守Paper机械验收清单

操作前：确认账户为模拟、client独占、资金帽子、当前市场/行情、无未解释持仓、保护类型已批准、TWS人工接管可用。操作者阅读所有请求并确认。

案例顺序：

1. 只读连接与完整快照。
2. 明确批准的一笔微量entry，等待真实ACK，不以返回UUID成功。
3. 未成交取消并确认；价格低也可能成交，必须准备保护/退出。
4. 可控场景中验证fill、commission、ledger一致（不为强迫成交无限追价）。
5. 验证保护订单在broker侧真实存在，partial fill路径若无法安全触发则fake/replay证据+标记外部未测，不伪造PASS。
6. 减仓/关闭cycle后对账，确认无多余子单。
7. 用fake先验证断线/重启；真实故障演练需再次明确批准且操作者在场。

每项保留run/config/command ids、时间、脱敏broker证据、expected/actual、结果和残余风险。不能把API日志里写“sent”当成broker确认。

## 7. 验收报告格式

```text
Gate: Gx
Code/config/data/model hashes:
Environment/account scope:
Test suite and exact command:
Results: PASS / FAIL / BLOCKED / INCONCLUSIVE
Skipped or untested cases:
Safety invariants violated: 0 or detailed list
Economic evidence and uncertainty:
Open incidents:
Approver and approval expiry:
Allowed next actions:
Explicitly forbidden next actions:
```

截图可作为辅助证据，但不能代替machine-readable订单/账本事件。记录真实错误，不“修复报告数字”。

## 8. Release NO-GO条件

无实时行情、账户必要数据旧/缺失、无人处理告警、未知订单、保护状态未确认、生产里存在AllowRisk、交易writer重复、模型/数据版本不明确、研究泄漏、付费数据无许可、备份恢复未测、当前profile无用户批准——任何一条即不发布。

## 9. 自动化建议

新增`acceptance manifest`列每项证据URI/hash与状态，CI生成gate report但不替人批准。离线测试使用参数化随机种子，失败seed保存为永久回归。外部integration单独marker和runner，默认network-deny/credentials-absent；paid LLM eval有独立预算批准，不在PR测试中偷偷计费。
