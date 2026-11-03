# Codex implementation prompts

Run these sequentially. Do not ask Codex to implement all phases in one giant pass.

## Prompt 0 — Repository audit

You are working on `ai-quant-trader`, a paper-first automated trading system.

Read the entire repository before changing code. Produce a concise architecture audit covering:
- module boundaries,
- unsafe couplings,
- missing tests,
- live-trading escape paths,
- IBKR lifecycle risks,
- persistence/idempotency risks.

Do not implement features in this step. Do not weaken `ALLOW_LIVE_TRADING=false`.

## Prompt 1 — Foundation hardening

Implement and verify the application foundation:
- Python 3.12,
- FastAPI lifespan lifecycle,
- SQLAlchemy 2 async engine/session,
- Alembic initialization and first migration,
- async redis client with explicit cleanup,
- structured JSON logging,
- `/health`, `/api/system/status`,
- graceful worker shutdown,
- unit tests.

Constraints:
- modular monolith,
- no broker calls from HTTP handlers,
- no live-money support,
- tests must pass without IBKR installed.

Acceptance:
- `docker compose up postgres redis api` starts cleanly,
- DB migration succeeds,
- health endpoint returns 200,
- pytest passes.

## Prompt 2 — IBKR connection supervisor

Implement the IBKR Paper connection supervisor using the official `ibapi` TWS API.

Requirements:
- EClient/EWrapper callback architecture,
- states DISCONNECTED, CONNECTING, SYNCING, READY, DEGRADED,
- do not enter READY before `nextValidId`,
- handle connectionClosed,
- classify IBKR informational vs connectivity error codes,
- 1100/2110 => DEGRADED and block new orders,
- 1101/1102 => SYNCING, not READY,
- reconnect with bounded exponential backoff + jitter,
- no reconnect storm,
- expose broker status through service layer,
- persist broker state events,
- do not log account secrets.

Do not place any orders.

## Prompt 3 — Reconciliation barrier

Implement broker reconciliation.

On startup/reconnect:
1. request managed accounts,
2. request open orders,
3. request positions,
4. request executions for the relevant session window,
5. wait for each corresponding end/barrier callback,
6. reconcile local DB against broker state,
7. if any unexplained mismatch exists set DEGRADED and block trading,
8. only transition to READY after reconciliation succeeds.

Requirements:
- idempotent execution persistence by broker execId,
- stable mapping using broker permId where available,
- never create a new order merely because a local record is missing,
- tests for restart with existing broker order and partial fills.

## Prompt 4 — Market data service

Implement stock market data first.

Requirements:
- contract resolver for US STK via SMART/USD,
- request/cancel market data,
- cache Bid/Ask/Last/Volume/timestamps,
- calculate mid and spread percentage,
- market-data staleness detector,
- reference-counted subscriptions so multiple strategies share one IBKR subscription,
- reject new trades when required data is stale,
- handle max ticker line errors without retry loops,
- REST read endpoint and websocket event publishing.

Do not add options yet.

## Prompt 5 — Signals and strategy boundary

Implement `MomentumBreakoutV1` as a signal generator only.

Signal must include:
- strategy version,
- symbol,
- side,
- score,
- entry reference,
- stop reference,
- take-profit reference,
- structured reason JSON,
- market-data timestamp.

It must never call Broker or Execution directly.

Add replayable unit tests using deterministic market bars.

## Prompt 6 — Risk engine

Implement deterministic pre-trade risk evaluation.

At minimum enforce:
- broker READY,
- not killed/paused,
- fresh market data,
- max daily loss,
- max open positions,
- max position percentage of NAV,
- max gross exposure,
- max trade risk,
- max bid/ask spread,
- positive buying power.

Every decision must be persisted with rule, reason, and snapshot.
Risk rejection must be fail-closed.

## Prompt 7 — Order manager (Paper only)

Implement Paper-only LIMIT order submission through IBKR.

Critical requirements:
- refuse LIVE mode at runtime,
- unique internal order UUID,
- broker orderId allocated only when broker READY,
- persist CREATED before send,
- state machine for SUBMITTED / PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED / UNKNOWN,
- process openOrder + orderStatus + error + execDetails callbacks,
- deduplicate executions by execId,
- no MARKET order support in first implementation,
- do not infer FILLED from successful `placeOrder()` return,
- idempotent cancellation,
- audit logs.

## Prompt 8 — Kill switch and failure injection

Implement PAUSED / ACTIVE / KILLED system states and failure-injection tests.

Test:
- stale data,
- TWS disconnect,
- 1100/2110 connectivity event,
- order reject,
- duplicate callback,
- duplicate execDetails,
- process restart,
- DB temporarily unavailable,
- Redis temporarily unavailable.

Loss of Redis must not permit unsafe trading.
Loss of DB write ability must block new order submission.

## Prompt 9 — End-to-end paper test

Build one opt-in integration scenario:
Market Data -> Signal -> Risk -> LIMIT Order -> Order callbacks -> Fill -> Position -> Exit -> Reconciliation.

Safety:
- Paper account only,
- environment flag `ENABLE_PAPER_ORDER_TESTS=true` required,
- tiny predefined max notional,
- no options,
- no market orders,
- print a final invariant report proving broker/local positions and fills agree.

## Prompt 10 — Options phase gate

Do not implement options until Prompt 0-9 acceptance criteria pass.
When they pass, create a design PR only for:
- option chain discovery,
- Greeks/IV,
- DTE filters,
- liquidity/spread/OI filters,
- expiration guard,
- exercise/buying-power guard,
- mandatory close policy for short-dated long options.

No option live trading in this prompt.
