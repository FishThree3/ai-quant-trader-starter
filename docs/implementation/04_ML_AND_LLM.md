# 04｜机器学习、大模型与模型治理

版本 1.0｜2026-11-07｜目标设计｜关联：[02 数据](02_DATA_ENGINEERING.md)、[03 策略](03_STRATEGY_SPECIFICATION.md)、[05 研究](05_BACKTEST_AND_RESEARCH.md)、[12 模板](12_DECISIONS_TEMPLATES_REFERENCES.md)

## 1. AI 的三种角色与实施顺序

| 层级 | 输入 | 输出 | 首次交付 | 禁止事项 |
|---|---|---|---|---|
| 确定性基准 | OHLCV/指标 | 候选 Signal | 先于 AI | 无规则的手工主观调参 |
| 监督学习 filter | 决策时点可见的数值特征 | 成功概率/预期净收益/不确定度 | 离线 OOS，再 shadow | 直接绕过风控调 quantity |
| LLM 文本事件 | 授权新闻/公告和有限上下文 | 有证据的结构化事件 | 抽取评测，再增量实验 | broker/凭证/代码执行/改风险 |

强化学习、深度序列大模型、在线自动重训不是 MVP。树模型都未验证出稳定增益时，增加模型规模通常只会扩大研究自由度和运维成本。

## 2. 首个 ML 任务：候选交易的 meta-label filter

### 2.1 训练单位

每条基准策略候选 Signal 为一个样本，保存 `signal_key, instrument_id, decision_at, label_start/end, feature_snapshot_id, hypothetical_order_config`。必须保留所有候选，包括最后因 filter 不执行的候选，否则只对实际成交样本训练会产生选择偏差。

经济约束拒绝（无购买力/停牌）和预测 filter 拒绝分开记录。标签基于预先冻结的仿真执行规则，不能拿不可能买到的 close 作为真实入场价。

### 2.2 第一版标签

先做两个阶段，减少标签含糊：

1. fillability：在订单 TTL 和限价规则下是否可成交；无成交的样本 `fill_status=NO_FILL`，不把它们武断标成亏损。
2. 在模拟可成交条件下，按 03 的止损/止盈/时间退出规则计算 round-trip `net_return_after_cost`；分类标签 `y=1(net_return_after_cost>0)`。

同时保存 continuous target 和最大不利/有利偏移。若使用 triple-barrier：upper=+2R、lower=-1R、vertical=12 bar；同根碰两边使用保守下界或标 AMBIGUOUS，不能选有利结果。实际基准包含趋势失效/flatten 时，标签也必须使用同一退出逻辑，不能训练目标和交易目标不一致。

标签结束时间是实际假想退出时间，最长覆盖后续 12 根 bar；训练折与测试折重叠区间要 purge。label 必须在 label_end 已知后才可用于训练。

## 3. 特征字典（第一版建议）

| 特征组 | 具体例子 | 防泄漏要求 |
|---|---|---|
| 收益/趋势 | 1/3/6/12 bar return、EMA20-EMA50/price、EMA slope | 仅 finalized bar；缩放器只 fit train |
| 波动 | ATR14/price、过去 20/60 bar realized vol | 不用当日结束才知道的 full-day vol |
| 突破 | (close-H_prev)/ATR、距当日已知 high、突破次数 | 当日 high 仅截至当前时间 |
| 成交量 | volume/此前 20 bar mean、与历史同时间槽对比 | 不能使用今日完整总量作为分母 |
| 流动性 | spread bps、top-of-book size、报价龄 | 研究缺 quote 时显式 missing；不伪造 |
| 市场背景 | 同时点指数 return/vol、相对强弱 | join 用 available_at，不能最近邻取未来 |
| 时间 | 开盘后分钟、距收盘分钟、周内日 | 日历版本固定，包含早收盘 |
| 事件（后期） | 财报/指引/回购/诉讼分类、证据支持强度 | 文本 first_seen 与 LLM 完成时刻均已知 |

所有 rolling 操作声明窗口、边界、min_periods、单位、缺失政策。特征集版本改变需要重新生成 lineage 和重评模型；不能给旧模型临时补一列或用 0 代替未知。

## 4. 模型路线与实验预算

1. Dummy/常数预测：验证 pipeline 和 class balance。
2. Logistic regression + train-only preprocessing：低自由度、可解释基线。
3. 一种 gradient-boosted tree（例如 LightGBM 或 sklearn HistGradientBoosting，二选一）：非线性对照。
4. 只有前述表现稳定，才立项序列模型；不同时测试十种网络后只报告最好者。

建议首批每类 ≤24 个预注册超参数配置，总 trial 数跨 notebook/脚本累计登记。随机种子固定并重复若干 seeds，置信区间按时间块统计，不只看最幸运 seed。

训练分割详见 05。缺失处理、标准化、特征选择、阈值、概率校准都在 inner train/validation 内拟合；outer test 只执行一次。

## 5. 概率校准与交易阈值

AUC 高不等于策略赚。输出至少评估：log loss、Brier score、reliability bins、precision/coverage、收益与成本。模型 score 不当概率；未校准值不能带入期望收益公式。

简化说明：`EV=p*avg_win-(1-p)*avg_loss-cost`，其中 win/loss/cost 的单位必须一致，全部来自训练/验证时点，而非未来全样本统计。阈值只在验证集选择，记录交易覆盖率与总风险变化。

MVP filter 可以仅做接受/拒绝，不按概率放大头寸。若以后按概率 sizing，需要单独验证校准漂移、风险估计误差以及 06 的硬上限，模型没有权调高上限。

## 6. ML 晋级协议

必须同时比较：

- 原确定性基准（相同候选、成本、资金）。
- 基准 + 随机过滤（匹配保留比例），排除“少交易所以回撤小”的假增益。
- 基准 + 简单阈值过滤（如趋势/波动），排除复杂模型重复简单规则。
- 基准 + ML filter。
- ML + LLM 特征与无 LLM 的 ablation（后期）。

强制报告净收益、最大回撤、日收益 Sharpe、成本、交易数量、持仓时间、单行业/单标的贡献、coverage、期间分布。统计与经济阈值见 05；样本不足为 INCONCLUSIVE，不改阈值。

## 7. LLM 模块：做证据抽取，不做交易员

### 7.1 第一版任务

从一份已授权文本中抽取：事件类型、涉及发行人、方向性事实、量化变化、时间跨度、不确定性、证据字符区间。输出供研究人员/特征管道审核，不输出 `BUY/SELL/quantity/order`。

目标 provider boundary：`TextEventExtractor.extract(document, as_of, schema_version) -> ExtractionResult`。先采用单请求/单 schema，而不是开放式多 agent 工具循环。

### 7.2 输出 JSON 契约

```json
{
  "schema_version": "text_event.v1",
  "document_id": "doc-id",
  "issuer_ids": ["issuer-id"],
  "event_type": "GUIDANCE_CHANGE",
  "direction": "DOWN",
  "horizon": "NEXT_QUARTER",
  "facts": [{
    "metric": "revenue_guidance",
    "old_value": null,
    "new_value": null,
    "unit": "USD",
    "evidence_start": 125,
    "evidence_end": 204,
    "evidence_hash": "sha256-of-source-span"
  }],
  "uncertainty": "HIGH",
  "abstain": false,
  "reason_codes": ["SOURCE_EXPLICIT", "MISSING_NUMERIC_RANGE"]
}
```

facts 的数字未知就 null，不猜；evidence span 必须在原文中实际存在且 hash 一致。发行人归一化由内部主表验证，LLM 字符串不能创建可交易合约。schema 合法≠事实正确，必须做第二层事实/单位/时间校验。

### 7.3 Prompt 规范

系统说明只定义抽取任务、输出 schema、缺失值/abstain、证据范围；用户内容承载一份文本和允许的元数据。网页中的“忽略上文”“立刻买入”等全部是待分析数据，不是指令。禁止向模型提供账户号码、现金余额、broker tokens、完整私有交易历史或任意本机文件。

不能要求模型披露隐藏思维链；记录外显字段、证据、简短 reason codes 和版本即可。数值变换、货币换算、订单金额由代码计算。

### 7.4 API 接入建议

本项目是 Python，若采用 Claude，使用官方 `anthropic` SDK，建议起始模型配置 `claude-opus-5-5`，实现时以组织可用模型列表和官方兼容性为准；本轮不调用付费模型、不安装依赖、不上传文档。供应商是可替换适配器，不把模型 ID 写进策略类。

必须固定并记录：`provider, requested_model, served_model, SDK_version, prompt_hash, schema_version, retrieval_snapshot, token_usage, latency, cost_estimate, fallback_path, request_id`。

- 简单分类使用有输出上限的结构化结果；长文本使用 SDK streaming，检查最终 stop_reason。
- 设置整体任务 deadline、有限重试与成本预算；429/网络错误可有限重试，schema/权限错误不无限重试。
- 缓存键包含 content_hash、prompt/schema/model 版本，不能只按标题缓存。
- 若启用服务端 fallback，必须记录实际 served_model；未通过同一评测的 fallback 结果只能 shadow，不能成为生产特征。
- refusal/max_tokens/非法 JSON/证据校验失败：返回 ABSTAIN/UNAVAILABLE，不解析半个交易建议。
- 不使用可写 MCP、Bash、computer-use、远程代码执行或 broker tool 作为此模块的工具。

这里是设计要求，不宣称当前代码已实现某个 API binding；供应商版本变化按 12 的参考表复核。

## 8. 一个特别危险的泄漏：模型预训练记忆

即使给 LLM 的新闻日期是过去，模型也可能在预训练中见过后续事实、市场反应、回顾文章。把当前大模型对历史事件的答案当成“当时可用预测”可能产生难以察觉的未来泄漏。

因此：

- 历史 LLM 事件实验只能当探索证据，不单独作为可交易 alpha 的证明。
- 强制 evidence-only extraction，禁止问“随后股价会如何”；去掉检索到的后续总结。
- 建立模型/提示冻结后的 **前瞻 shadow 文本数据集**；只用当时收到的文档和当时生成的结果做正式增量评估。
- 训练/测试分组按事件、发行人和重复报道聚类；同一公告转载不能横跨 train/test。
- 记录 publication、first_seen、extraction_completed，特征 available_at 为完成且校验通过时间，不回填到 publication。

## 9. 抽取评测集与指标

建议起步人工标注 300–500 份授权文档，按事件分组分割；20% 双人复核以估计标注分歧。涵盖：上调/下调/维持、同比/环比、美元/百万美元、否定语句、旧新闻转载、传闻/事实、表格、缺失数据、恶意指令、修订公告。

先验建议验收：schema 100% 合法或明确 abstain；无证据数字输出 0 容忍；entity mapping precision ≥99%；关键事件方向 precision ≥95%；abstain rate 必须报告不能隐匿。阈值为工程初值，需根据标注一致性与损失函数确认，未满足不接入交易特征。

LLM 自评不是唯一评判者；另一个 LLM 可做辅助，但人工 gold 和程序化证据校验仍必须保留。抽取准确不等于收益增益，后者依照 05 单独评估。

## 10. 模型生命周期

`DRAFT → TRAINED → VALIDATED → SHADOW → APPROVED_PAPER → ACTIVE_PAPER → RETIRED/QUARANTINED`。

registry artifact 包括：训练数据 manifest、代码 SHA、环境锁、超参、特征集、校准器、阈值、评测报告、已知局限、签字、SHA256。载入前验证完整性和模型格式，不能反序列化不可信 pickle；模型文件是供应链输入。

只在 session 前切换 bundle；交易时段若模型出错，默认暂停新开仓。允许 fallback 至确定性基准仅当该 fallback 路径本身预先注册/回测/获准，不可临场决定。持仓的保护和退出独立于模型进程继续运行。

重训 cadence：先每月离线评估，不自动部署；可按漂移报警触发候选训练，但每次部署仍过 gate。不能每天对昨天亏损做自动过拟合修正。

## 11. 监控与工作拆分

监控：特征缺失/范围漂移、PSI/KS 等分布指标（阈值需基于基准样本校准）、prediction 分布、校准、延迟、abstain、模型/served-model 变化、输入 token/成本、feature freshness、训练数据和生产数据差异。

任务：ML-01 候选/标签 → ML-02 数值基线与 walk-forward → ML-03 registry/shadow → LLM-01 文本契约/gold → LLM-02 受限抽取/缓存 → LLM-03 前瞻增量评估。详细验收与依赖见 [10](10_ROADMAP_AND_BACKLOG.md)、[11](11_TEST_AND_ACCEPTANCE.md)。
