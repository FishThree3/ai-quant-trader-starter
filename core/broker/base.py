from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from core.reconciliation.models import (
    BrokerSnapshot,
    LocalSnapshot,
    ReconciliationResult,
)


class BrokerState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    SYNCING = "SYNCING"
    READY = "READY"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, slots=True)
class BrokerStatus:
    state: BrokerState
    connected: bool
    account: str | None = None
    next_order_id: int | None = None
    last_error: str | None = None


class Broker(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def status(self) -> BrokerStatus: ...

    @abstractmethod
    async def collect_snapshot(self) -> BrokerSnapshot:
        """Authoritative broker state, with each barrier's completion recorded."""
        ...

    @abstractmethod
    def apply_reconciliation(
        self, snapshot: BrokerSnapshot, local: LocalSnapshot
    ) -> ReconciliationResult:
        ...

    @abstractmethod
    async def sync_state(self, local: LocalSnapshot) -> ReconciliationResult:
        """Reconcile broker truth against local records.

        Returning the result keeps the caller able to persist the broker view and
        report which invariants failed, without inspecting private state.
        """
        ...
