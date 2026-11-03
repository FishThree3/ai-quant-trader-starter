from core.broker.base import Broker, BrokerStatus
from core.broker.ibkr.connection import IBKRConnectionConfig, IBKRConnectionManager
from core.reconciliation.models import (
    BrokerSnapshot,
    LocalSnapshot,
    ReconciliationResult,
)


class IBKRBroker(Broker):
    def __init__(self, config: IBKRConnectionConfig) -> None:
        self.connection = IBKRConnectionManager(config)

    async def connect(self) -> None:
        await self.connection.connect()

    async def disconnect(self) -> None:
        await self.connection.disconnect()

    async def status(self) -> BrokerStatus:
        return await self.connection.status()

    async def collect_snapshot(self) -> BrokerSnapshot:
        return await self.connection.collect_snapshot()

    def apply_reconciliation(
        self, snapshot: BrokerSnapshot, local: LocalSnapshot
    ) -> ReconciliationResult:
        return self.connection.apply_reconciliation(snapshot, local)

    def degrade(self, reason: str) -> None:
        self.connection.degrade(reason)

    def is_ready(self) -> bool:
        return self.connection.is_ready()

    def place_limit_order(self, intent) -> int:
        return self.connection.place_limit_order(intent)

    def cancel_order(self, broker_order_id: int) -> None:
        self.connection.cancel_order(broker_order_id)

    def add_order_listener(self, listener) -> None:
        self.connection.add_order_listener(listener)

    def subscribe(self, instrument, req_id: int) -> None:
        self.connection.subscribe(instrument, req_id)

    def unsubscribe(self, req_id: int) -> None:
        self.connection.unsubscribe(req_id)

    def set_market_data_type(self, mode: int) -> None:
        self.connection.set_market_data_type(mode)

    async def sync_state(self, local: LocalSnapshot) -> ReconciliationResult:
        return await self.connection.sync_state(local)
