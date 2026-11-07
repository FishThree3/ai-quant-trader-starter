"""Opt-in read-only Paper lifecycle verification.

The old penny-priced order test assumed an order could never fill. Price is not
a safety guarantee. Until production account-risk sourcing/reservations and an
explicit approved intent are wired, integration tests must not send orders.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ENABLE_PAPER_CONNECTION_TESTS") != "true",
    reason="set ENABLE_PAPER_CONNECTION_TESTS=true for read-only Paper verification",
)


async def test_paper_connection_and_snapshot_are_read_only():
    from config.settings import settings
    from core.broker.ibkr.broker import IBKRBroker
    from core.broker.ibkr.connection import IBKRConnectionConfig

    broker = IBKRBroker(
        IBKRConnectionConfig.from_settings(settings, read_only=True, client_id_offset=101)
    )
    try:
        await broker.connect()
        status = await broker.status()
        assert status.connected
        assert status.account is not None and status.account.startswith("DU")
        assert not broker.is_ready(), "Handshake alone must never grant trading readiness"
        snapshot = await broker.collect_snapshot()
        assert snapshot.is_complete, snapshot.missing_barriers
        # No local writes/adoption or READY grant. This test only observes the account.
    finally:
        await broker.disconnect()
    assert not (await broker.status()).connected
