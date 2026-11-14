"""CSV input: one row per order with `name` and `color`."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass
class Order:
    idx: int  # unique within a batch (one per copy)
    name: str
    color: str  # normalised: trimmed, lower-case
    order_id: str = ""  # webstore Order ID (empty for plain CSV input)
    compartments: int = 5

    @property
    def letters(self) -> int:
        return sum(1 for ch in self.name if ch.isalpha())


class CsvError(ValueError):
    pass


def parse_orders(text: str) -> tuple[list[Order], list[str]]:
    """Parse CSV text. Returns (orders, warnings). Raises CsvError on fatal problems."""
    text = text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise CsvError("The CSV is empty.")

    header = [c.strip().lower() for c in rows[0]]
    if "name" in header and "color" in header:
        ni, ci = header.index("name"), header.index("color")
        data = rows[1:]
    elif "name" in header and "colour" in header:
        ni, ci = header.index("name"), header.index("colour")
        data = rows[1:]
    else:
        # No header: assume name,color
        ni, ci = 0, 1
        data = rows

    orders: list[Order] = []
    warnings: list[str] = []
    for line_no, r in enumerate(data, start=2 if data is not rows else 1):
        name = r[ni].strip() if len(r) > ni else ""
        color = r[ci].strip().lower() if len(r) > ci else ""
        if not name:
            warnings.append(f"Row {line_no}: empty name — skipped")
            continue
        if not color:
            warnings.append(f"Row {line_no}: {name!r} has no color — skipped")
            continue
        if "\n" in name or "|" in name:
            warnings.append(f"Row {line_no}: {name!r} — names are single-line only; using it as typed")
        if not any(ch.isalpha() for ch in name):
            warnings.append(f"Row {line_no}: {name!r} has no letters")
        orders.append(Order(idx=len(orders), name=name, color=color))
    if not orders:
        raise CsvError("No valid rows found (need columns: name, color).")
    return orders, warnings
