# 02｜数据工程、行情与时点一致性

版本 1.0｜2026-11-07｜目标设计｜关联：[03 策略](03_STRATEGY_SPECIFICATION.md)、[05 回测](05_BACKTEST_AND_RESEARCH.md)、[08 数据契约](08_SCHEMA_API_AND_EVENTS.md)

## 1. 本模块交付目标

建立一套研究和运行共用的数据契约，让系统回答：某笔交易做出决定时，哪些数据已经可见、数据来自哪里、经过什么变换、是否有缺口、能否复现。

现有 `MarketDataService` 仅处理部分实时 tick/BBO，不是历史数据平台；`ContractResolver` 主要做字符串校验，不是 broker 合约资格确认。二者保留并扩展，不把目录存在当成完成。

## 2. 数据源分工与购买前检查

| 数据 | MVP 用途 | 推荐来源类别 | 选择前必须确认 |
|---|---|---|---|
| 实时 bid/ask/last/size | 入场、执行质量、保护校验 | IB 已授权实时行情 | 专业/非专业身份、交易所许可、数据线限制 |
| 1m OHLCV | 回放/特征、聚合 5m | 经许可历史 provider；IB 可用于小范围校验 | 历史覆盖、修订、退市标的、拆分、授权保存期限 |
| quote/trade 历史 | 成交模拟校准 | 可提供逐笔或 quote 的授权 provider | 时间精度、条件码、bid/ask 范围与费用 |
| 交易日历 | 开盘/收盘、半日市、DST | 版本化交易所日历资料/成熟库 | 特殊休市公告如何更新 |
| corporate actions | 拆股、分红、symbol 变化 | 有生效/公布时间的行动数据 | ex-date、record/pay-date、修订、逆向调整 |
| 证券主表/成分 | 合约唯一识别、避免幸存者偏差 | broker contract details + 历史主表 | delisted/inactive 记录、conId 历史 |
| 新闻/公告 | LLM 结构化事件，后期可选 | 官方公告/获授权新闻服务 | 原文使用权、发布/首次采集/修订时间、再分发限制 |
| IB 账户/成交/费用 | 真实会计与风控 | IB API；必要时账单/Flex 对照 | 回调字段、窗口限制、时区与币种 |

禁止把展示网页价格截图作为回测主数据；禁止为了快速跑通用随机行情代替验收证据。可以用合成数据做单元测试，但必须标明 SYNTHETIC，不能出现在 performance report 中。

供应商选型输出一张 `data_vendor_evaluation`：覆盖资产、时间跨度、是否包含退市、时区、调整方式、延迟/实时、版本政策、保存权、费用、试样 SHA256、质量问题、联系人。未经用户批准不付费购买或上传数据。

## 3. 时间语义：至少四个时间

| 字段 | 定义 | 用途 |
|---|---|---|
| event_at | 交易所事件/文档原发布时间（若可信） | 经济事件排序 |
| received_at | 本系统首次接收时间 | 网络延迟与本地可见性 |
| available_at | 所有必要处理完成、可供当时策略读取的时间 | 防未来函数的唯一 as-of 边界 |
| ingested_at | 本次入库时间 | ETL 运行审计，不可替代 available_at |

另外保存 source_timezone、time_precision、timestamp_quality。UTC aware 存储；本机 monotonic clock 只测超时和耗时，不用于长期持久化事件时间。

不变量：`available_at <= decision_at` 才能读入策略；bar 的 `bar_end <= decision_at`；事件发布时间早但晚到本系统的新闻必须等 received_at 之后才可用。若仅有历史日终修订值，则标为 `availability_uncertain`，不得用于高频新闻回测。

例：09:35 ET 结束的 5m bar，09:35:01.4 才完成聚合；09:35:00 的决策不能看到它。更正于次日的数据不能无版本覆盖昨日使用的 bar。

## 4. 合约与白名单

### 4.1 合约资格确认流程

1. 输入 symbol + secType=STK + currency=USD + primaryExchange（如有）。
2. 调用 broker contract-details 类请求；等待明确完成，限定时间与请求预算。
3. 返回 0 条：UNRESOLVED；多条无法唯一选择：AMBIGUOUS；都禁止交易。
4. 保存内部 instrument_id、IB conId、localSymbol、primaryExchange、currency、minTick、marketRule IDs、tradingHours/liquidHours、有效期。
5. 使用所需价格区间的市场价格增量规则，不认为所有股票永久为 0.01。
6. 到新 session 检查映射是否变化；变化需失效旧 quote/feature，必要时人工确认。

目标文件：`core/data/contracts.py`, `database/instrument_repository.py`；适配器延续现有 `core/market/resolver.py`，不在策略里临时拼 Contract。

### 4.2 Universe 规则

- 先使用 5–10 个显式白名单标的测试数据，不是投资推荐。
- 研究 universe 保存 `effective_from/to` 和纳入原因。
- 若后期做流动性排名，使用上个交易日及之前已知数据，不用未来全年成交量选股。
- 固定 ETF 白名单可以降低幸存者偏差问题，但仍要说明列表是在何时/为何选择，不能声称完全消除选择偏差。
- 停牌、合约不明确、价格调整异常、最近数据缺失：从当前 session 可交易集合排除，不删除历史样本。

## 5. 分层与不可变存储

推荐 `raw → normalized → curated → features`，每层保留输入 manifest 与转换版本。

### 5.1 目录布局（新增设计）

```text
data/
  raw/provider=<name>/dataset=<type>/date=<yyyy-mm-dd>/part-<id>.parquet
  curated/bars/timeframe=1m/instrument=<id>/session=<date>/rev=<n>.parquet
  features/set=<version>/session=<date>/part-<id>.parquet
  manifests/<dataset_id>.json
  quarantine/<run_id>/...
```

Parquet 推荐 PyArrow；分析用 DuckDB/Polars 按需引入，交易关键路径不依赖大型研究 dataframe。敏感账户数据不放公开对象存储。raw 为 append-only；已发布 manifest 不覆盖，只发布新 revision。

### 5.2 Manifest 最小字段

`dataset_id, schema_version, provider, license_id, source_query, instrument_ids, start/end, source_timezone, bar_size, adjustment_mode, timestamp_quality, row_count, checksums, parent_ids, transform_code_sha, created_at, quality_status, known_gaps, revision`。

回测/训练运行必须传 dataset_id，不接受默认“最新 data/”目录；否则同一个命令隔天会产生不同结果。

## 6. 1m/5m Bar 契约与聚合

字段：`instrument_id, session_id, timeframe, bar_start, bar_end, open, high, low, close, volume, trade_count?, vwap?, source, revision, available_at, finalized, quality_flags`。

规则：

- 时间区间左闭右开 `[bar_start, bar_end)`，避免边界 tick 同时属于两根 bar。
- bar_start 按交易 session 对齐，不按 UTC 零点盲目整除。
- 5m 用五根完整 1m 聚合：open=首根 open、high=max、low=min、close=末根 close、volume=sum。
- 不把缺失 1m bar 插值当成真实成交；区分“交易所确认零成交”与“采集丢失”。
- close 不能用于该 bar 内的成交决策；完成信号后的下一事件/下一 bar 才可进入成交模拟。
- bar 从 finalized 变更需产生 revision event；运行中不回溯重发已经作出的历史交易。
- 初值：bar_end 后 2 秒完成数据等待窗口，缺失则标 incomplete 并跳过决策；晚到处理与延迟统计保留，不把窗口延長成看未来。

实时源只提供快照而无逐笔 volume 时，不能假装得到精确成交量 bar。使用 broker/provider 的 bar 服务或有质量说明的聚合；记录数据类型是 TRADES、MIDPOINT 还是 BID_ASK，三者不得混用成交量研究。

## 7. 数据质量规则

| 编号 | 规则 | 处置 |
|---|---|---|
| DQ-01 | price/size/volume 都有限；合法空值有明确类型 | 非有限隔离；不替换为 0 |
| DQ-02 | high ≥ max(open,close)，low ≤ min(open,close)，low ≤ high | 整根 bar 隔离 |
| DQ-03 | bid>0、ask>0、ask≥bid；每侧时间正确 | 不可执行；记录 reason |
| DQ-04 | 同主键/同 revision 内容一致 | 不一致阻断发布 manifest |
| DQ-05 | session bar 覆盖度可核算 | 缺失明细，不用均值掩盖 |
| DQ-06 | 序列乱序/回退不能覆盖更晚事件 | 排序重放或 quarantine |
| DQ-07 | 疑似拆股/异常跳变需对照行动数据 | 标记而非简单 winsorize 原始价格 |
| DQ-08 | 价格/量单位、币种、multiplier 明确 | 单位未知不进入风控 |
| DQ-09 | 可见时间不晚于消费时点；未来 tick 拒绝 | 研究作废/运行不出信号 |
| DQ-10 | 未知行情模式、冻结/延迟 | 展示可以，开仓不可以 |

建议数据发布门槛：关键字段缺失 0；非有限 0；重复冲突 0；拟交易 session 的必需窗口无缺口。全库覆盖率 99.9% 可以作为观测值，但不能替代单次交易窗口完整性检查。

## 8. 复权、分红与新闻修订

保留原始可成交价格和研究调整序列两套。账本/委托/保护价使用当日原始价格与真实持仓数量；长期收益评估可用 total-return 序列，但必须显式计分红，避免分红算两次。

拆股事件至少含 `announced_at, effective_at, ratio, source_revision`。持仓数量与成本按行动变更；未完成订单按 broker 实际事件对账，不假设自动调整。对于隔夜 hold，分红/拆股必做；MVP 日内空仓也需避免历史特征跨拆股断点。

新闻新增 `document_id, published_at, first_seen_at, revision_at, content_hash, source_uri, license_id, language`。更正新闻作为新 revision，历史实验通过 as-of 选择当时可见版本。

## 9. 采集与回填任务

### D1 历史采集

输入：universe version、时间范围、granularity、provider config、预算。分片按 instrument/session；checkpoint 包含已完成查询和 hash；失败只重试该片，不重复计费整段。

限流器按 provider/请求类型分桶，遵从具体 API 版本限制；优先保留撤单/账户/恢复带宽，不与历史回填共享无限队列。IB 精确 pacing/并发值本次未联网核实，配置必须来自上线时官方文档和小样测试，不硬编码猜测的数字。

### D2 持续采集

会话前资格检查 → 订阅 → 接收质量校验 → bar finalized → manifest/quality event → session close 校验。掉线造成的区间标为 gap；回填与实时合并必须去重，并保持原决策所见数据版本。

### D3 跨源核对

每天采样对照 IB 与历史 provider 的 OHLC/时间/分红规则。差异要有阈值及分类，不强行“以 IB 为真”覆盖所有历史来源。输出 `source_comparison_report`，解释 auction、条件码、adjustment 差异。

## 10. 接口草案

```python
class HistoricalProvider(Protocol):
    async def fetch_bars(self, query: BarQuery) -> BarBatch: ...

class MarketCalendar(Protocol):
    def session(self, venue: str, date: str) -> TradingSession: ...

class DatasetRepository(Protocol):
    def open_version(self, dataset_id: str) -> DatasetView: ...

class FeatureReader(Protocol):
    def as_of(self, instrument_id: str, decision_at: datetime,
              feature_set_version: str) -> FeatureSnapshot: ...
```

这些是新增目标接口，不是已有 SDK binding。provider/日历依赖锁版本，示例类的真实类型在 08 落地。

## 11. 测试、任务与验收

必测：DST 春/秋切换、半日市、午后停牌、五根中缺一根、相同时刻重复 tick、晚到更正、拆股前后、先收到 revision 2 再收到 revision 1、时区错误、延迟模式变更、provider rate-limit、重跑回填幂等。

交付文件：数据源评估表、两周小样、至少一个完整历史版本 manifest、日历快照、质量报告、回填恢复演练。G1 数据门禁定义见 [11](11_TEST_AND_ACCEPTANCE.md)；任务 DATA-01…DATA-04 见 [10](10_ROADMAP_AND_BACKLOG.md)。

**不通过示例**：能够下载 CSV，却无法证明列的时区/复权方式/可见时间；或仅有今日仍上市标的历史，却声称是无偏全市场回测。
