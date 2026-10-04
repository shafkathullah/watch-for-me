"""Warehouse inventory ledger: stock levels, reservations and reorder planning."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

LOW_STOCK_RATIO = 0.35
MAX_BATCH_UNITS = 480
LEAD_TIME_DAYS = {"local": 3, "regional": 9, "overseas": 41}
SKU_PREFIXES = ("BLT", "NUT", "WSH", "GSK")


class LedgerError(Exception):
    """Raised when a movement would leave the ledger inconsistent."""


@dataclass
class Item:
    sku: str
    name: str
    on_hand: int = 0
    reserved: int = 0
    reorder_point: int = 60
    supplier: str = "local"
    unit_cost: float = 0.0
    history: list[tuple[date, int]] = field(default_factory=list)

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved

    def is_low(self) -> bool:
        threshold = self.reorder_point * (1 + LOW_STOCK_RATIO)
        return self.available <= threshold


def validate_sku(sku: str) -> str:
    sku = sku.strip().upper()
    prefix, _, number = sku.partition("-")
    if prefix not in SKU_PREFIXES:
        raise LedgerError(f"unknown prefix {prefix!r} in {sku}")
    if not number.isdigit() or len(number) != 5:
        raise LedgerError(f"bad serial in {sku}: expected 5 digits")
    return sku


class Ledger:
    def __init__(self, today: date | None = None) -> None:
        self.items: dict[str, Item] = {}
        self.today = today or date(2024, 3, 18)
        self.audit: list[str] = []

    def add_item(self, sku: str, name: str, **kwargs: object) -> Item:
        sku = validate_sku(sku)
        if sku in self.items:
            raise LedgerError(f"{sku} already exists")
        item = Item(sku=sku, name=name, **kwargs)
        self.items[sku] = item
        self._log(f"add {sku} {name}")
        return item

    def receive(self, sku: str, units: int) -> int:
        item = self._get(sku)
        if units <= 0 or units > MAX_BATCH_UNITS:
            raise LedgerError(f"receive {units}: outside 1..{MAX_BATCH_UNITS}")
        item.on_hand += units
        item.history.append((self.today, units))
        self._log(f"receive {sku} +{units}")
        return item.on_hand

    def reserve(self, sku: str, units: int) -> None:
        item = self._get(sku)
        if units > item.available:
            short = units - item.available
            raise LedgerError(f"{sku}: short by {short} units")
        item.reserved += units
        self._log(f"reserve {sku} {units}")

    def release(self, sku: str, units: int) -> None:
        item = self._get(sku)
        item.reserved = max(0, item.reserved - units)
        self._log(f"release {sku} {units}")

    def ship(self, sku: str, units: int) -> float:
        item = self._get(sku)
        if units > item.reserved:
            raise LedgerError(f"{sku}: only {item.reserved} reserved")
        item.reserved -= units
        item.on_hand -= units
        item.history.append((self.today, -units))
        self._log(f"ship {sku} -{units}")
        return round(units * item.unit_cost * 1.07, 2)

    def daily_usage(self, sku: str, window_days: int = 28) -> float:
        item = self._get(sku)
        since = self.today - timedelta(days=window_days)
        shipped = sum(-n for day, n in item.history if n < 0 and day >= since)
        return shipped / window_days

    def days_of_cover(self, sku: str) -> float:
        usage = self.daily_usage(sku)
        if usage == 0:
            return float("inf")
        return self._get(sku).available / usage

    def reorder_plan(self) -> list[dict[str, object]]:
        plan = []
        for item in sorted(self.items.values(), key=lambda i: i.sku):
            if not item.is_low():
                continue
            lead = LEAD_TIME_DAYS[item.supplier]
            usage = self.daily_usage(item.sku)
            target = item.reorder_point * 2 + round(usage * lead)
            units = min(MAX_BATCH_UNITS, max(0, target - item.available))
            if units == 0:
                continue
            plan.append(
                {
                    "sku": item.sku,
                    "units": units,
                    "arrives": (self.today + timedelta(days=lead)).isoformat(),
                    "cost": round(units * item.unit_cost, 2),
                }
            )
        return plan

    def stock_value(self) -> float:
        return round(sum(i.on_hand * i.unit_cost for i in self.items.values()), 2)

    def export_csv(self, path: Path) -> int:
        rows = 0
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["sku", "name", "on_hand", "reserved", "available"])
            for sku in sorted(self.items):
                item = self.items[sku]
                writer.writerow(
                    [sku, item.name, item.on_hand, item.reserved, item.available]
                )
                rows += 1
        return rows

    def save(self, path: Path) -> None:
        payload = {
            "today": self.today.isoformat(),
            "items": [
                {
                    "sku": i.sku,
                    "name": i.name,
                    "on_hand": i.on_hand,
                    "reserved": i.reserved,
                    "reorder_point": i.reorder_point,
                    "supplier": i.supplier,
                    "unit_cost": i.unit_cost,
                }
                for i in self.items.values()
            ],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: Path) -> Ledger:
        raw = json.loads(path.read_text())
        ledger = cls(today=date.fromisoformat(raw["today"]))
        for entry in raw["items"]:
            sku = entry.pop("sku")
            name = entry.pop("name")
            ledger.add_item(sku, name, **entry)
        return ledger

    def _get(self, sku: str) -> Item:
        try:
            return self.items[validate_sku(sku)]
        except KeyError:
            raise LedgerError(f"{sku} is not in the ledger") from None

    def _log(self, message: str) -> None:
        self.audit.append(f"{self.today.isoformat()} {message}")


def demo() -> Ledger:
    ledger = Ledger(today=date(2024, 3, 18))
    ledger.add_item("BLT-00417", "M8 hex bolt", reorder_point=120, unit_cost=0.19)
    ledger.add_item("NUT-00932", "M8 lock nut", reorder_point=90, unit_cost=0.11)
    ledger.add_item("GSK-01588", "pump gasket", supplier="overseas", unit_cost=2.45)
    ledger.receive("BLT-00417", 400)
    ledger.receive("NUT-00932", 150)
    ledger.receive("GSK-01588", 75)
    ledger.reserve("BLT-00417", 260)
    ledger.ship("BLT-00417", 245)
    ledger.reserve("GSK-01588", 31)
    return ledger


if __name__ == "__main__":
    book = demo()
    for line in book.reorder_plan():
        print(line)
    print("stock value:", book.stock_value())
