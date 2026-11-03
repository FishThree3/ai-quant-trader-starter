# AI Quant Trader — Starter v0.1

Paper-first automated trading architecture for IBKR.

## Safety posture

- `TRADING_MODE=PAPER` by default.
- `ALLOW_LIVE_TRADING=false` is a hard configuration gate.
- Strategy cannot call Broker directly.
- New order IDs are unavailable until IBKR `nextValidId` arrives.
- Connectivity loss transitions the broker away from READY.
- After connectivity restoration, the intended production path is SYNCING -> reconciliation -> READY.
- Executions are intended to be persisted idempotently by IBKR `execId`.

## Why the classic EClient/EWrapper path?

IBKR introduced a synchronous wrapper in TWS API 10.40+, but the vendor describes it as Beta and currently limited in scope. The critical execution path therefore uses the mature asynchronous callback model.

## Official IBKR API installation

Obtain the current TWS API package from Interactive Brokers' official TWS API download and install the Python `ibapi` package into the environment used by the worker.

Do not silently substitute an unofficial old PyPI build.

The rest of the application and unit tests are intentionally able to start without `ibapi` installed.

## TWS configuration

1. Log in to the IBKR **Paper Trading** session.
2. Enable API/socket clients in TWS settings.
3. Set the socket port to your configured `IBKR_PORT`.
4. Keep `ALLOW_LIVE_TRADING=false`.
5. Copy `.env.example` to `.env`.

The current IBKR documentation examples list TWS paper 7947 / live 7946 and IB Gateway paper 4002 / live 4001. Verify your own TWS configuration rather than assuming defaults.

## Run infrastructure

```bash
cp .env.example .env
docker compose up postgres redis api
```

Then visit:

```text
http://localhost:8000/health
```

Run the broker worker only after the official IBKR Python API is installed in that runtime.

## Development sequence

See `docs/CODEX_PROMPTS.md` and execute prompts in order.

Do not skip reconciliation, idempotency, or failure-injection phases to reach strategy work faster.
