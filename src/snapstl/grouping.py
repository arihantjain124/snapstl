"""Group orders into plates.

Names: same colour only; 3 per plate, dropping to 2 then 1 when they don't fit.
Bases (all white): 3 per plate. First, each colour's full groups of 3 (so base
plates line up with name colours); then leftovers are pooled into mixed plates
of 3. Only the very last plate may have fewer.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .orders import Order
from .packing import PackResult, Part, pack
from .settings import PlateSettings

MAX_COMBOS = 30  # bounded search for a fitting subset within a colour


@dataclass
class Plate:
    kind: str  # "names" | "bases"
    orders: list[Order]
    parts: list[Part]
    result: PackResult
    warnings: list[str] = field(default_factory=list)

    @property
    def colors(self) -> list[str]:
        return [o.color for o in self.orders]


def _colors_in_order(orders: list[Order]) -> list[str]:
    seen: list[str] = []
    for o in orders:
        if o.color not in seen:
            seen.append(o.color)
    return seen


def _try_pack(subset: list[Order], parts: dict[int, Part], cfg: PlateSettings) -> PackResult:
    return pack([parts[o.idx] for o in subset], cfg)


def group_names(
    orders: list[Order], name_parts: dict[int, Part], cfg: PlateSettings
) -> tuple[list[Plate], list[str]]:
    plates: list[Plate] = []
    errors: list[str] = []
    for color in _colors_in_order(orders):
        queue = [o for o in orders if o.color == color and o.idx in name_parts]
        while queue:
            first, rest = queue[0], queue[1:]
            placed = None
            for k in range(min(cfg.max_names_per_plate, len(queue)), 0, -1):
                for n, others in enumerate(itertools.combinations(rest, k - 1)):
                    if n >= MAX_COMBOS:
                        break
                    subset = [first, *others]
                    res = _try_pack(subset, name_parts, cfg)
                    if res.ok:
                        placed = (subset, res)
                        break
                if placed:
                    break
            if not placed:
                res = _try_pack([first], name_parts, cfg)
                errors.append(f"{first.name} ({color}) name does not fit on a plate: {res.reason}")
                queue = rest
                continue
            subset, res = placed
            warn = []
            if len(subset) < min(cfg.max_names_per_plate, len(queue)):
                warn.append(f"Only {len(subset)} {color} name(s) fit on this plate")
            plates.append(Plate("names", subset, [name_parts[o.idx] for o in subset], res, warn))
            ids = {o.idx for o in subset}
            queue = [o for o in queue if o.idx not in ids]
    return plates, errors


def group_bases(
    orders: list[Order], base_parts: dict[int, Part], cfg: PlateSettings
) -> tuple[list[Plate], list[str]]:
    n = cfg.bases_per_plate
    plates: list[Plate] = []
    errors: list[str] = []
    pool: list[Order] = []
    avail = [o for o in orders if o.idx in base_parts]

    # Pass 1: full groups within a colour
    for color in _colors_in_order(avail):
        same = [o for o in avail if o.color == color]
        full = len(same) // n * n
        for i in range(0, full, n):
            pool_back = _place_bases(same[i : i + n], base_parts, cfg, plates, errors)
            pool.extend(pool_back)
        pool.extend(same[full:])

    # Pass 2: mixed leftovers
    pool.sort(key=lambda o: o.idx)
    while pool:
        chunk, pool = pool[:n], pool[n:]
        # _place_bases always consumes at least one order, so this terminates
        pool = _place_bases(chunk, base_parts, cfg, plates, errors) + pool
    return plates, errors


def _place_bases(chunk, base_parts, cfg, plates, errors) -> list[Order]:
    """Pack as many of `chunk` as fit (largest first); return the ones left over."""
    for k in range(len(chunk), 0, -1):
        subset = chunk[:k]
        res = _try_pack(subset, base_parts, cfg)
        if res.ok:
            warn = []
            if k < len(chunk):
                warn.append(f"Only {k} bases fit on this plate (wanted {len(chunk)})")
            plates.append(Plate("bases", subset, [base_parts[o.idx] for o in subset], res, warn))
            return chunk[k:]
    errors.append(f"Base for {chunk[0].name} does not fit on a plate")
    return chunk[1:] if len(chunk) > 1 else []
