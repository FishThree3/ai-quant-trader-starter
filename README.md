# AI Quant Trader

IBKR **模拟账户优先、默认只读**的 Python 交易基础设施。包含行情、风控、订单状态机、持久化、对账和控制 API。

**这不是已完成的 AI 交易算法。** `core/strategy` / `core/portfolio` 尚未实现可运行策略和组合管理；没有收益、回测或自动交易能力的承诺。当前 worker 只采集/对账，不自动下单。

## 安全边界

- 仅支持 `TRADING_MODE=PAPER`、`ALLOW_LIVE_TRADING=false`；实盘配置一律拒绝。
- `IBKR_READ_ONLY=true` 默认在适配器层同时禁止下单与撤单。
- 等待 `nextValidId` **及** `managedAccounts`，验证 DU 模拟账户；拒绝实盘账户、已知实盘端口和未明确选择的多账户会话。端口本身不是账户身份凭证。
- 握手不等于 READY；完整对账成功后才可 READY，断线会使旧对账失效。
- 新订单必须有显式 `PreTradeCheck.authorize()`。当前 worker 没有生产级风险上下文来源，因此即使关闭只读也不能自动提交。
- IB 订单号在网络发送前持久化；相同 UUID 不重复发送，内容变更拒绝；发送结果不明确则记录 UNKNOWN，不自动重试。
- 可执行行情必须是双边、非交叉、有效且及时的实时价格。成交量/最新成交不会刷新旧买卖报价；延迟/冻结行情仅用于展示。
- 控制开关 ACTIVE 不代表可以交易；API 还检查 broker 心跳、只读模式和提交能力。丢失数据库/Redis 可见性时不宣称可交易。

## 环境与安装

Python 3.12+。从 IBKR 官方 TWS API 下载包安装 `ibapi`，不要从 PyPI 随意替换成旧版第三方分发。本地验证的 SDK 为 1051.1。

```bash
python -m pip install -e ".[dev]"
```

复制 `.env.example` 的配置到自己的 `.env`，**不要覆盖已有凭证**。主机本地运行时设置 `IBKR_HOST=127.0.0.1`，数据库和 Redis 地址也使用本机可达地址；示例里的 `postgres` / `redis` 是 Docker 服务名。

TWS 常用模拟端口为 7497，Gateway 为 4002；以 TWS/Gateway 的 API 设置为准。启用 socket API，并保留 TWS 的只读 API 选项进行诊断。多模拟账户必须填写 `IBKR_ACCOUNT`。

### 首先只读诊断

不需要数据库或 Redis，不修改持仓、订单或交易开关，不输出账户号码：

```bash
python -m apps.doctor --snapshot
```

可选读取行情（可能需要行情授权与开市时段；延迟行情不可用于交易）：

```bash
python -m apps.doctor --snapshot --quotes AAPL SPY --duration 10 --market-data-type 3
```

输出分别说明连接、账户验证、快照完整性、行情是否收到及是否可执行。`diagnostics_ok=true` 只说明连接/可选快照通过，**不代表有行情或可以交易**。

### API 与只读 worker

启动 PostgreSQL、Redis（Docker Desktop 必须可用）：

```bash
docker compose up -d postgres redis
```

先备份自己的数据库，再对目标数据库执行迁移；本次新增订单 `time_in_force` 列，旧记录回填 DAY：

```bash
python -m alembic upgrade head
```

```bash
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

```bash
python -m apps.worker.main
```

- `GET /health`：进程/配置，不是交易就绪检查。
- `GET /api/system/status`：控制开关、broker 心跳和有效提交状态。
- `GET /api/market/AAPL` / `/ws/market?symbol=AAPL`：缓存行情和价格新鲜度。
- `/api/system/kill`、`pause`、`resume`：需要 `CONTROL_API_TOKEN`，默认拒绝未配置凭证的修改。
- 当前只支持一个账户、一个固定 client ID、一个 worker 写入同一个数据库；不支持多写入进程或跨账户共用订单表。

## 验证

默认测试不连接 IB：

```bash
python -m pytest tests/unit -q
```

```bash
python -m ruff check . --select E4,E7,E9,F
```

迁移测试使用新建临时 SQLite 数据库，不接触用户数据库。IB 只读集成测试需要显式环境变量，见 [集成验证说明](tests/integration/README.md)。原本会发送低价订单的测试已替换；“价格很低”不是绝不会成交的保证。

## 架构与限制

- `apps/`：进程组装、只读诊断、HTTP 控制/查询。
- `core/broker/ibkr/`：官方 SDK 回调、账户和连接边界。
- `core/market/`：订阅共享、报价与可执行新鲜度。
- `core/risk/`：确定性检查；不调用 broker 交易接口。
- `core/execution/`：风控授权入口、持久化订单标识、单写入者状态机。
- `core/reconciliation/`：broker 与本地事实比较；UNKNOWN 保持阻断。
- `database/`：持久化适配器；`alembic/`：迁移。

尚未完成：账户 NAV/购买力实时快照与挂单风险预占、完整成交→持仓投影、持仓感知的减仓/退出风控、确定性策略/历史回放/费用滑点回测、断线自动恢复 supervisor，以及 AI 研究层。禁止通过注入“始终通过”的生产风控来绕过这些缺口。账户前缀保护是额外防误配置措施，不能取代 TWS 模拟登录及只读权限。

本次审计、验证结果与剩余边界见 [重构报告](docs/REFACTOR_REPORT.md)。`docs/CODEX_PROMPTS.md` 是历史规划，不是已实现能力清单。
