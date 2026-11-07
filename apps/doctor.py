"""Read-only IBKR paper diagnostics: python -m apps.doctor --help.

Never changes the trading gate, database, orders or positions. Account identifiers
are intentionally omitted from the report. A quote entitlement failure is not a
connection failure and is reported independently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import structlog

from config.settings import settings
from core.broker.ibkr.broker import IBKRBroker
from core.broker.ibkr.connection import IBKRConnectionConfig
from core.market.resolver import ContractResolver
from core.market.service import MarketDataService


async def diagnose(args: argparse.Namespace) -> dict:
    broker = IBKRBroker(
        IBKRConnectionConfig.from_settings(
            settings, read_only=True, client_id_offset=args.client_id_offset
        )
    )
    service = MarketDataService(broker, stale_seconds=settings.ibkr_stale_data_seconds)
    broker.add_order_listener(service)
    resolver = ContractResolver()
    instruments = [resolver.resolve(symbol) for symbol in args.quotes]
    report = {"mode": "PAPER", "read_only": True, "orders_sent": 0, "connected": False}
    try:
        await broker.connect()
        status = await broker.status()
        report.update(
            connected=status.connected,
            account_verified=status.account is not None,
            state=status.state.value,
            next_order_id_received=status.next_order_id is not None,
        )
        if args.snapshot:
            snapshot = await broker.collect_snapshot()
            report["snapshot"] = {
                "complete": snapshot.is_complete,
                "missing_barriers": snapshot.missing_barriers,
                "open_orders": len(snapshot.orders),
                "positions": len(snapshot.positions),
                "executions": len(snapshot.executions),
            }
        if instruments:
            broker.set_market_data_type(args.market_data_type)
            for instrument in instruments:
                await service.subscribe(instrument, "doctor")
            await asyncio.sleep(args.duration)
            report["quotes"] = {
                instrument.symbol: {
                    "received": (quote := service.quote(instrument)) is not None,
                    "bid": quote.bid if quote else None,
                    "ask": quote.ask if quote else None,
                    "delayed": quote.delayed if quote else None,
                    "executable_fresh": quote.is_fresh(
                        settings.ibkr_stale_data_seconds, service.clock()
                    )
                    if quote
                    else False,
                }
                for instrument in instruments
            }
        report["diagnostics_ok"] = bool(
            report["connected"]
            and report.get("account_verified")
            and (not args.snapshot or report["snapshot"]["complete"])
        )
    except Exception as exc:
        # SDK error text can contain account/order identifiers; omit it from JSON.
        report.update(
            diagnostics_ok=False,
            error_type=type(exc).__name__,
            hint="Check paper login, socket/API permissions, host/port and unique client ID",
        )
    finally:
        for instrument in instruments:
            await service.unsubscribe(instrument, "doctor")
        await broker.disconnect()
        report["disconnected"] = not (await broker.status()).connected
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Read-only IBKR Paper connection diagnostics")
    result.add_argument("--snapshot", action="store_true", help="Read orders/positions/executions")
    result.add_argument("--quotes", nargs="*", default=[], help="Optional US stock symbols")
    result.add_argument(
        "--duration", type=float, default=5, help="Quote collection seconds (0..60)"
    )
    result.add_argument("--market-data-type", type=int, choices=(1, 2, 3, 4), default=1)
    result.add_argument(
        "--client-id-offset", type=int, default=100, help="Avoid the worker client ID"
    )
    return result


def main() -> int:
    cli = parser()
    args = cli.parse_args()
    if not 0 <= args.duration <= 60 or args.client_id_offset <= 0:
        cli.error("duration must be 0..60 and client-id-offset must be positive")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    report = asyncio.run(diagnose(args))
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["diagnostics_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
