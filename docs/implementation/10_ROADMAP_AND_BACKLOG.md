# 10｜分阶段路线、工作包、依赖与预算

版本 1.0｜2026-11-07｜计划不是承诺工期｜关联：[11 门禁](11_TEST_AND_ACCEPTANCE.md)、[12 执行模板](12_DECISIONS_TEMPLATES_REFERENCES.md)

## 1. 推荐建设顺序

**先保存安全基线 → 真实数据/账户契约 → 账本/预占/仿真 → 确定性策略与样本外研究 → 受控AI候选 → shadow → 小额有人值守Paper → 自动Paper运营。**

AI研究可与交易基础建设并行，但AI结果不能绕过尚未完成的安全门禁。外部真实成交测试不是默认开发步骤，始终需要用户明确批准。

## 2. 人员角色与估算口径

- E：Python/异步/数据库/交易执行工程。
- Q：量化研究/统计/时点数据。
- M：机器学习/LLM评测。
- O：运营/基础设施/安全。
- A：用户/独立审阅者，负责风险范围/验收批准。

一个人可以兼任，但关键审批不由代码自批。下表单位为**理想工程人日**，含实现与单元测试，不含等待供应商、购买数据、人工标注采购、外部Paper观察和重大返工。AI辅助可加速写代码，不自动缩短金融样本积累时间。

## 3. 工作包总表

每个工作包必须有：输入、输出、错误分支、最小回归、文档更新、review证据。目标新增路径都只是计划。

| ID | 工作包/文件落点 | 依赖 | 人日 | 交付与完成判据 |
|---|---|---|---:|---|
| BASE-01 | 保存基线、环境锁、architecture依赖测试；`pyproject.toml`, CI | 无 | 2–3 | 可重建环境；当前482测试证据；不改保护默认 |
| BASE-02 | RuntimeProfile/config version；`core/contracts`, `config` | BASE-01 | 2–3 | 模式/额度/版本schema，审批与未知字段拒绝 |
| DB-01 | MIG-A账户/运行身份；`database`, `alembic` | BASE-02 | 3–5 | 可审计回填；多账户/namespace冲突测试 |
| DATA-01 | 合约资格与日历；`core/data/contracts.py`, `calendar.py` | BASE-01 | 3–5 | conId歧义拒绝、tick rule、DST/半日市测试 |
| DATA-02 | 历史provider/manifest/checkpoint；`core/data/providers`, `apps/data` | DATA-01 | 4–7 | 限流恢复、版本化小样、许可记录 |
| DATA-03 | bars/quality/修订；`core/data/bars.py`, `quality.py` | DATA-02 | 4–6 | 1m→5m一致、无插值伪成交、as-of测试 |
| DATA-04 | 实时与历史cross-check、特征可见性 | DATA-03 | 2–4 | 缺口/复权/延迟报告，G1证据 |
| ACCT-01 | AccountSnapshot采集、完整性/TTL；broker+portfolio | DB-01 | 4–6 | 真实必要字段、epoch、cash/margin区别，fake先行 |
| LEDGER-01 | Decimal迁移与不可变ledger；`database`, `core/portfolio` | DB-01 | 5–8 | 买卖/费用/分红/拆股/reversal恒等式 |
| LEDGER-02 | 持仓/现金/PnL投影与重建 | LEDGER-01, ACCT-01 | 4–6 | crash重放hash一致，broker差异可解释 |
| RISK-01 | 完整RiskContext、纯规则、reduce-only | ACCT-01, LEDGER-02, DATA-01 | 4–6 | 购买力/现有敞口/相关群组/账户龄规则 |
| RISK-02 | 事务预占/授权/幂等；commands+reservations | RISK-01, DB-01 | 5–8 | 并发竞争、两次authorize不双占、UNKNOWN保留 |
| EXEC-01 | inbox/outbox/dispatcher、durable ID | RISK-02 | 4–6 | commit/send崩溃矩阵，无歧义重发 |
| EXEC-02 | 保护order groups/partial fill/exit协调 | EXEC-01, LEDGER-02 | 6–10 | stop/TP/OCA/手动reduce不超卖，保护缺口告警 |
| EXEC-03 | supervisor/lease/重连/订阅恢复 | EXEC-01, ACCT-01, DATA-04 | 4–7 | 断线不READY、旧socket隔离、退避上限 |
| EXEC-04 | 恢复/日终对账、佣金/修正链路 | EXEC-02, EXEC-03 | 4–7 | UNKNOWN证据流程、完整recovery report |
| STRAT-01 | features与纯策略指标 | DATA-03, BASE-02 | 3–5 | 无未来函数、warmup/窗口精确 |
| STRAT-02 | MomentumBreakoutV1/cursor/cooldown | STRAT-01 | 3–5 | 全部03规则、信号key/NoSignal幂等 |
| STRAT-03 | PortfolioPlanner/执行意图映射 | STRAT-02, RISK-02 | 3–5 | 多候选排序与同账户预算，不直接broker |
| BT-01 | ReplayClock/事件引擎/sim broker | DATA-03, BASE-02 | 5–8 | 决策后事件才能成交，生命周期合同兼容 |
| BT-02 | fill/cost/partial/cancel模型 | BT-01, LEDGER-02 | 4–7 | bar歧义保守、限价不突破、费用不双算 |
| BT-03 | 组合指标/实验manifest/folds | BT-02, STRAT-03 | 4–6 | 复现包、purge/embargo、所有trial登记 |
| BT-04 | 基准OOS/敏感性/否证报告 | BT-03, DATA-04 | 4–8 | 达G3或明确NO-GO/INCONCLUSIVE |
| ML-01 | 候选dataset/标签/特征版本 | BT-03, STRAT-02 | 3–5 | 无fill标签分开、label_end purge |
| ML-02 | 数值基线/校准/filter ablation | ML-01 | 5–8 | 配对OOS与随机过滤对照，成本后增益 |
| ML-03 | registry/serving/shadow/漂移 | ML-02, BASE-02 | 4–6 | 批准bundle/hash、模型失败不影响保护 |
| LLM-01 | 文本许可、gold规范/事件schema | DATA-02, BASE-02 | 3–5 | 事件去重、证据offset、人工标签协议 |
| LLM-02 | 受限SDK抽取、缓存、budget、校验 | LLM-01 | 3–5 | 无broker工具、拒绝/超时/注入/数字证据测试 |
| LLM-03 | 抽取评测与前瞻增量研究 | LLM-02, ML-03 | 4–7 | gold质量+prospective shadow；不混淆历史记忆 |
| API-01 | v1 commands/审批/status/审计 | EXEC-01, BASE-02 | 3–5 | role、idempotency、202不是成交，权限测试 |
| OPS-01 | 部署/secrets/单实例/备份 | DB-01, BASE-01 | 3–5 | 隔离profile、锁版本、恢复环境不连broker |
| OPS-02 | 指标/告警/trace/preflight | EXEC-03, API-01 | 3–5 | P0/P1覆盖、gate与告警不互相掩盖 |
| OPS-03 | 日SOP/incident/backup restore演练 | OPS-01, OPS-02 | 3–5 | 实测恢复报告、操作者可执行手册 |
| QA-01 | property/数据库事务/故障fixtures | LEDGER-01, BASE-01 | 4–6 | PostgreSQL竞争与crash不变量测试 |
| QA-02 | replay/shadow parity与negative test | EXEC-04, BT-04, ML-03 | 4–6 | 同输入相同决策，禁止路径全部覆盖 |
| QA-03 | shadow/Paper演练与验收包 | QA-02, OPS-03, API-01 | 5–8 | G5证据、无自动实盘/无未批准send |

注：BT-01等在本文件是工作包ID；05中BT测试编号为“测试场景”，实现时统一改为`TEST-BT-xx`避免工单混淆。任务执行后不得自动把依赖尚未通过的门禁标绿。

## 4. 时间与关键路径

关键路径通常是 `BASE → DB/ACCT → LEDGER → RISK预占 → EXEC保护/恢复 → parity → Paper运营`。另一条是 `DATA → STRAT → BT → OOS → ML`；两条在QA-02/03汇合。

按上表 36 个工作包累计为 **133–217 理想工程人日**。这不是按日历可直接承诺的交付时间：

- 单人全职且熟悉交易/统计/数据库：约7–11个理想工作月，另留25–40%集成/审查缓冲，并有至少30交易日Paper观察；首次做这类系统可能更久。
- 2–3人互补团队可并行数据/研究与执行，但事务/接口冻结、外部数据和市场观察是串行瓶颈；不把总人日除人数当固定工期。
- “安全Paper基准闭环”可暂不做ML/LLM，减少初期范围；“完整AI增强系统”仍需完成ML/LLM相应门禁。
- 30交易日是约6个市场周且假期会延长；100个round trip未达到时继续观察，不为凑数增加交易。

资金/数据不足、研究无增益、人工无法运营，都允许NO-GO；计划不能保证最终形成有正收益的策略。

## 5. 阶段计划与交付包

| 阶段 | 工作包 | 交付包 | 进入下一阶段的硬门槛 |
|---|---|---|---|
| P0 基线 | BASE、DB-01、OPS-01初版 | 基线/权限/环境/迁移报告 | G0 |
| P1 数据与账户 | DATA、ACCT | 合格manifest、合约/日历、真实账户契约 | G1 |
| P2 安全仿真闭环 | LEDGER、RISK、EXEC、BT01/02、QA01 | 可重放的买→部分成交→保护→退出→对账 | G2 |
| P3 基准研究 | STRAT、BT03/04 | 预注册OOS/成本/否证结果 | G3 |
| P4 AI候选 | ML、LLM | model/文本卡、配对增益、registry | G4；无增益留shadow |
| P5 操作验收 | API、OPS02/03、QA02/03 | shadow/Paper日志、事件/恢复证据 | G5a/G5b |
| P6 自动Paper | 固定已批准bundle，持续观测 | 每日运行/审计/报警闭环 | G6 release checklist |

工程性的1股Paper测试可以在G2后由用户单独批准，用于验证broker行为，不构成策略G3或自动运行授权。研究失败不影响保留安全基础设施，但不得自动继续用失败策略交易。

## 6. 前10个工作日的可执行安排（建议）

### Day 1–2：基线/范围/事实

- 用户确认MVP profile、可接受的模拟预算、数据使用权与操作者。
- 在获准后保存代码基线和依赖锁；运行已有测试，不抹掉当前未提交重构。
- 创建任务板与ADR；明确哪些路径是新增、哪些是已有。
- 输出 `baseline-report`、`runtime-profile schema`、`permission map`。

### Day 3–4：账户数据与表迁移设计

- 枚举IB账户必要字段、币种、屏障、更新频率，fake回调测试。
- 设计accounts/namespace/MIG-A；临时库回填和冲突演练。
- 输出AccountSnapshot契约和两个负例：缺NAV、旧epoch混入。

### Day 5–6：历史小样与日历

- 选择一个授权provider的小样，不直接购买全量。
- qualify 2–3个白名单合约；验证早收盘/DST。
- 下载/读入两周样本并生成manifest、quality report。
- 输出数据许可检查项；未知字段不填0。

### Day 7–8：账本垂直切片

- 在临时PostgreSQL/SQLite实现BUY fill→fee→partial SELL→cash/position/PnL。
- 写重复exec、晚佣金、crash/replay和reversal测试。
- 输出ledger invariant报告，先不连broker发单。

### Day 9–10：预占竞争与演示

- 单账户两并发候选争5000现金，证明不会一起批准6000。
- 两次authorize同command不双占；cancel_pending不释放。
- fake broker完成一个完整round trip并对账。
- 更新计划/估算，列出实际数据与账户阻塞；没有权限或必要字段继续做离线，不降低标准。

这10天的结果是可验证的最小切片，不是完整AI系统上线。

## 7. 成本预算模型

### 7.1 固定/可变支出

| 成本项 | 如何估算 | 批准前必须知道 |
|---|---|---|
| 实时行情授权 | 交易所/package/月费 | 账户身份、非专业资格、数据线、再分发 |
| 历史数据 | 年份×标的×粒度×license | 是否含quotes/退市/corporate actions |
| 主机/DB/备份 | CPU/RAM/存储/出站和恢复副本 | 是否自有设备，市场时段可用性 |
| ML训练 | CPU时长+实验次数+存储 | 基线树模型无需默认购GPU |
| LLM | 文档数量×input/output tokens×实时单价 | provider合约、retention、缓存/fallback、重试 |
| 人工标注/审核 | 样本数×每份分钟+双标复核 | gold质量与来源授权 |
| 运营 | 每日preflight/review与on-call | 谁响应P0、谁能TWS接管 |

不在未联网核价时写虚假的固定美元报价。

LLM月成本公式：`N*(T_uncached*P_in + T_cached*P_cache + T_output*P_out)/1e6 + retries/fallback/附加服务成本`。例：每交易日100篇、20天、每篇4000 input和500 output，即约8M input与1M output（未含缓存/重试），把实际报价代入再审批；不是请求现在运行2000次API。

数据容量粗估：10标的×390根1m/RTH日×252日≈982,800 bars/年，不含半日市、quotes与多revision；quote体量可能高几个数量级，先测实际采样再规划存储。

### 7.2 预算控制

付费历史回填/LLM批处理要有dry-run token/数据量估计、月/日预算、人工确认、运行ID。达到硬预算后停止新研究任务，不影响存量交易保护；绝不把账户资金与API预算混为一类。

## 8. 每个PR的完成定义

- 小而垂直：一个可独立验证行为，不混策略调参与数据库迁移。
- 有接口/拒绝分支/幂等/rollback或前向修复策略。
- 有至少一个“旧实现失败，新实现通过”的回归。
- domain层无broker/HTTP秘密依赖；新生产代码无AlwaysActive/AllowRisk捷径。
- 变更包含schema/配置/文档/metric对应更新。
- 本地与CI证据、未完成事项、风险和review人清楚。
- 不自动commit/push/发单/迁移用户库；执行这些动作必须取得相应授权。

任务交给其他AI时使用12的任务模板，限制文件范围，禁止让它先创建大量空抽象或一口气“实现全部阶段”。
