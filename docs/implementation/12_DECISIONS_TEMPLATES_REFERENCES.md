# 12｜决策记录、工作模板、术语与参考资料

版本 1.0｜2026-11-07｜与整套规划共同维护｜入口：[总索引](README.md)

## 1. 重要ADR（初始建议）

ADR不是“记录AI说了什么”，而是明确系统选择了什么、为什么、代价是什么以及何时重审。

| ADR | 初始选择 | 原因/代价 | 重新讨论触发条件 |
|---|---|---|---|
| ADR-001 | 模块化单体、单交易writer | 降低分布式一致性和恢复难度；不能横向双活 | 可测容量不足或多账户正式立项 |
| ADR-002 | Paper-only，实盘单独项目 | 当前代码/运营证据不足；资金风险隔离 | 完成G6且用户另行明确实盘授权 |
| ADR-003 | 美股/ETF、USD、整股、只做多、RTH | 限定合约/结算/借券/时段复杂度 | 新资产有独立数据和风险模型 |
| ADR-004 | 数值模型先于LLM入交易特征 | 更可回放、易比较；文本有许可/时点泄漏 | 基准与ML通过G3/G4且文本gold可用 |
| ADR-005 | AI不接触broker/风控参数/active部署 | 权限边界可验证，不依赖prompt自律 | 原则不放宽，新增只读工具另审 |
| ADR-006 | PostgreSQL交易事实、Redis缓存 | durable事务/唯一约束；Redis故障不得丢真值 | 只有明确一致性收益才替换 |
| ADR-007 | Decimal账本，研究特征float | 精确现金和可审计舍入；有迁移成本 | 资产精度要求改变时扩展 |
| ADR-008 | append-only账本+reversal | 修复不抹历史，能重放；存储/投影较复杂 | 不以节省开发为理由取消 |
| ADR-009 | 保护单为自动持仓前置 | 止损预算不等于可执行保护 | 具体order type由用户批准和IB验收 |
| ADR-010 | UNKNOWN不重发、不释放预算 | 避免网络歧义造成重复风险；可能人工阻塞 | 有完整证据与审计修复才解除 |
| ADR-011 | 预注册walk-forward与lockbox | 避免隐性多重尝试/未来函数 | 数据范围/目标重大变化时新实验 |
| ADR-012 | 文本先prospective shadow | 当前LLM可能记得历史后果 | 不能以历史高分替代前瞻证据 |

### ADR模板

```text
ADR-ID / 标题 / 日期 / 状态: Proposed|Accepted|Superseded
背景与当前约束:
问题和真实失败场景:
选择:
考虑过的替代方案（至少一个）:
安全/成本/延迟/可维护性取舍:
影响文件/接口/数据/权限:
迁移与兼容:
验证计划和否决条件:
回滚或前向修复:
批准人:
重审触发条件:
关联任务和Gate:
```

## 2. 待用户在实施阶段确认的决策

本轮无需停下来回答即可交付规划，但真正执行前必须明确：

1. 是先做日内单账户Paper基准，还是另有确定资产/周期？本规划默认前者。
2. 允许的Paper名义额/每单帽子/运行时段/保护类型；真实下单前逐范围批准。
3. 当前IB账户类型、行情授权、专业数据订阅身份、可用资金字段语义。
4. 数据购买与保存许可、历史时间跨度/质量、研究和LLM预算。
5. 谁承担市场时段on-call、P0人工接管、恢复审批和每日回顾？
6. 目标是学习/研究工具、个人自动模拟系统，还是未来商业资管？后者有额外合规要求，不在当前默认范围。
7. 策略经济目标与可接受回撤；本规划1%日损/5%研究回撤等是初值不是对用户风险承受力的判断。
8. 数据出境/云模型保留/隐私限制是否允许将某类授权文本发送给供应商？

没有答复的项目使用安全默认：不付费、不上传敏感数据、不发单、不迁移已有库、只离线/只读。

## 3. 可交给实现者或其他AI的任务模板

```text
任务ID: RISK-02（示例）
目标: 实现同账户、同command_key的事务风险预占，不改策略收益参数。
基线: 精确code SHA或经用户保存的worktree patch hash。
必读: 06账户风险、08数据契约、11 TEST-RISK-01/02/04。
允许修改: 指定core/portfolio文件、repository、独立migration及对应tests。
不允许修改: broker wire、策略参数、现有.env、用户数据库、实盘门禁。
输入接口: RequestedTrade、RiskContext、PortfolioView；列出版本。
输出接口: durable OrderAuthorization/Reservation或结构化拒绝。
幂等: 同key同payload一份预占；异payload冲突；两次authorize只验证。
失败路径: DB提交失败/响应丢失、并发竞争、旧revision、TTL与UNKNOWN。
测试: 临时PostgreSQL并发+SQLite快速回归；不连真实IB、不付费调用LLM。
验收: 指定不变量与expected结果；必须提供新增回归。
交付: 实现、测试结果、migration说明、已知限制和文档变化。
权限: 不commit/push、不改branch、不下单，除非本任务另有用户明确授权。
范围限制: 不再次委派/展开多agent review；超范围先报告原因。
```

不要使用“按照所有文档一次实现完整系统”这种无法验收的任务。一次切片应能在1–3天内演示核心行为；较大工作包拆成schema、域逻辑、adapter、验证几个依赖明确的PR。

## 4. 实验预注册卡

```text
experiment_id:
research_question:
economic_hypothesis:
null_hypothesis / falsification:
universe_version / selection_at:
data_manifest_ids / license:
time_range / availability_assumptions:
strategy_version / parameter_search_space:
feature_set / label_definition / label_end:
train_validation_test_folds / purge_gap:
lockbox_period / allowed_open_count:
models / hyperparameter_trials / seeds:
fill_model / cost_model / delays:
benchmarks / ablations:
primary_metric / secondary_metrics:
statistical_method / block_length_sensitivity:
pass / fail / inconclusive thresholds:
known_leakage_or_selection_risks:
compute / LLM budget and approval:
reviewer / frozen_at:
```

结束时追加结果、全trial列表、失败实验、重现命令和artifact hash，不覆盖预注册字段。负结果也保存，避免其他AI或研究者重复走已否证路径。

## 5. 模型卡

```text
bundle_id / lifecycle_state:
intended_use / prohibited_use:
training_dataset / code / dependencies:
feature_schema / preprocessing / calibration:
label semantics / universe / horizon:
model_id / parameters / thresholds:
OOS metrics / uncertainty / trial count:
regime and instrument slices:
LLM requested_model / served_model / prompt_hash（如适用）:
prospective-vs-historical evidence:
known_failure_modes / missing-data policy:
fallback policy and tested path:
latency / cost budget:
artifact checksum / signature:
approver / effective_session / expiry:
rollback bundle / quarantine trigger:
```

模型卡不能只写“准确率90%”；必须说数据切分、基线、交易成本、样本量、误差和未覆盖场景。

## 6. 数据源评估与质量报告模板

```text
dataset/provider/license_id:
asset/time coverage / delisted support:
bar type / timezone / granularity:
raw-vs-adjusted / corporate actions:
event/received/available semantics:
revisions / duplicates policy:
missing rows / impossible prices / crossed quotes:
known gaps by session/instrument:
source comparison and unexplained differences:
checksums / row count / storage volume:
retention / permitted usage / redistribution:
quality decision: ACCEPT|QUARANTINE|REJECT
approver / limitations:
```

## 7. Paper运行批准卡

```text
run_id / date/session:
PAPER account internal ID / fixed client ID:
code/config/data/model hashes:
per-order quantity and notional cap:
gross / position / stop-risk / daily-loss caps:
allowed symbols and order types:
protection profile / reduce-only emergency policy:
start/end window and approval expiry:
operator present / TWS takeover reachable:
readiness and reconciliation evidence:
exact allowed mutation scope:
explicitly forbidden actions (LIVE / increase limits / new assets):
approver and timestamp:
end-of-run orders/positions/fees/reconciliation:
unresolved incidents / overnight exceptions:
```

即使Paper也是真实账户状态变更。不要写“不会成交所以无需批准”；价格不能保证不成交。

## 8. 事故复盘模板

时间线（UTC和市场本地时间）、发现方式、影响账户/订单/模型范围、用户可见状态、实际发生与本地推测的区别、根因/促成条件、应急操作/批准、账本和风险预占变化、为何现有测试没覆盖、回归用例、修复/缓解负责人和期限、是否需要重新走Gate。

不把人员或AI模型作为唯一根因；追问系统为何允许错误进入交易、为何检测失败、恢复为何需要猜测。

## 9. 术语表

| 词 | 本规划含义 |
|---|---|
| alpha | 相对明确基准、成本后、经不确定性检验的增益假设，不是“预测涨跌”同义词 |
| NAV | 净资产估计，有币种/时间/现金流基准 |
| gross exposure | 绝对名义暴露总和，需加未反映的入场预占 |
| reduce-only | 只减少已确认持仓，所有并发退出合计不能穿零 |
| reservation | 对尚可能成交风险的durable预算占用 |
| point-in-time/as-of | 只看到当时真正可用的版本，不是数据日期早就足够 |
| data leakage | 训练/决策偷看未来或跨边界信息，包括预训练记忆和重复新闻 |
| OOS/lockbox | 未参与拟合/选择的外层评估；lockbox使用次数受控 |
| shadow | 用真实输入生成意图但不发单，不代表真实成交 |
| Paper | 券商模拟账户交易；不证明实盘流动性/队列/市场冲击 |
| UNKNOWN | 事实不足的状态，必须保留风险，不是失败订单的别名 |
| outbox/inbox | durable业务事件发布/接收，至少一次+幂等，不创造网络exactly-once |
| fencing token | 识别过期writer的单调令牌；broker不支持时还需实际socket隔离 |
| protection | broker侧确认的保护订单组；不保证成交价或一定成交 |

## 10. 参考资料与本次核实状态

### 10.1 当前仓库：已核实

- [重构报告](../REFACTOR_REPORT.md)：现有功能、测试和只读IB结果。
- [配置](../../config/settings.py)：现有PAPER/只读和风险初值。
- [数据模型](../../database/models.py)：现有表；schema扩展不可假装已实现。
- [风控实现](../../core/risk/engine.py)：现有纯规则及缺失上下文。
- [授权接口](../../core/execution/gateway.py)：现有两阶段校验seam。
- [worker](../../apps/worker/main.py)：当前submission_enabled=false。
- [IB适配器](../../core/broker/ibkr/connection.py)：当前连接/回调/只读保护。

### 10.2 外部官方资料：上线前必须复核

**本次WebFetch无法通过域名安全/网络检查，未成功抓取下面网站。链接是实施时应核对的官方入口，不是本次已核验的引用；未以其内容宣称具体费率、限流、监管门槛。**

| 来源 | 链接 | 实施时要核对 |
|---|---|---|
| IBKR TWS API官方文档 | https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/ | SDK签名、账户字段/屏障、pacing、订单/保护语义、execution窗口、佣金/修正 |
| IBKR官方主页/账户支持 | https://www.interactivebrokers.com/ | paper与live差异、市场数据授权、费用、账户权限 |
| scikit-learn Cross-validation | https://scikit-learn.org/stable/modules/cross_validation.html | time-series split、gap、不能随机shuffle的条件 |
| scikit-learn Common pitfalls | https://scikit-learn.org/stable/common_pitfalls.html | preprocessing leakage、pipeline、随机性与复现 |
| Anthropic官方文档 | https://platform.claude.com/docs/en/intro | 当前模型/SDK结构化输出、错误/重试、数据保留、实际价格/权限 |
| 官方Python SDK | https://github.com/anthropics/anthropic-sdk-python | 所选锁版本的兼容API与安全更新 |
| SEC官方资料入口 | https://www.sec.gov/ | 适用证券结算规则与最新变化；本规划不固定其数值 |
| FINRA官方资料入口 | https://www.finra.org/ | 账户/日内交易限制及适用主体；不以网上旧门槛代替当前适用规则 |
| 交易所官方日历 | https://www.nyse.com/markets/hours-calendars | 交易日、早收盘、特殊休市，与所选库版本交叉验证 |

模型配置建议来自本次加载的Claude API参考资料，不是实际付费API调用结果。实施时须验证组织可用模型与平台、价目和数据保留条件。

### 10.3 参考复核记录模板

`reference_id, official_url, accessed_at, version/effective_date, relevant_section, verified_claim, account/jurisdiction_scope, local_snapshot_hash(if licensed), reviewer, recheck_due`。

若外网仍不可达，写BLOCKED并人工获取官方资料；不通过代理/替换域名绕过组织限制，不把“工具失败”变成凭记忆填写精确限制。

## 11. 文档维护与结束条件

本套文档的完成标准：每个模块有边界、接口、错误行为、落点、依赖和验收；公共参数/时间/身份一致；所有内部链接有效；工期预算可算；示例JSON合法；所有新增能力明确为待实施。

实施每完成一项工作包，在任务表增加实际状态/证据链接，不删除原估算与否证结果。需要改变安全策略时先更新ADR与验收，再修改代码，不以代码先行迫使文档追认。
