"""The unit of work inside a batch: one organizer to make (one per copy of an order)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Order:
    idx: int  # unique within a batch (one per copy; quantity 2 → two Orders)
    name: str  # text printed on the organizer
    color: str  # normalised colour of the name piece
    order_id: str = ""  # webstore Order ID
    compartments: int = 5  # from the scaling table (by letter count)

    @property
    def letters(self) -> int:
        return sum(1 for ch in self.name if ch.isalpha())
