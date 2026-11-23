"""Products SnapSTL makes (0.29): which webstore SKU is which product, and what kind of product it is.

* **organizer** — the personalised name desk organizer (`Magicians_35`, and orders typed in by hand, `Adhoc`):
  a base and a separate name, printed apart and glued (everything before 0.29).
* **stand** — a name stand (GTA, Harry Potter, Barbie style): one piece in two colours made on MakerWorld's
  parametric model maker (no license password, no watermark), printed in one go on a printer with an AMS:
  the coloured block at the bottom, the raised letters on top (one filament change).
  Their size, font settings and colours are in Settings → Name stands (`settings.StandSettings`).

Adding a product = one entry here (and its defaults in `settings.default_stand_products`). Static 3MF products
(the same file arranged on a plate) will be a third kind.

0.29 (step 1 of the name stands): stand orders are read from the report and listed on the Orders page
(Product chips), but not made yet — batches take Organizer orders only (`MAKEABLE`), and stand orders stay on
this computer (`SHARED`: not sent to the shared records, where a computer before 0.29 would take them for
organizers).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    key: str            # short id used by the app ("organizer", "gta", …)
    label: str          # shown on the Orders page, packing page …
    kind: str           # organizer | stand
    skus: tuple[str, ...]
    design_id: str = ""  # MakerWorld parametric model (stands)
    url: str = ""
    font_features: bool = False  # the model's "Use Font Features" box (GTA's joined letters)


ORGANIZER = Product("organizer", "Organizer", "organizer", ("Magicians_35", "Adhoc"), "2600234")
PRODUCTS: tuple[Product, ...] = (
    ORGANIZER,
    Product("gta", "GTA", "stand", ("Magicians_174",), "2737384",
            "https://makerworld.com/en/makerlab/parametricModelMaker?designId=2737384", font_features=True),
    Product("harry_potter", "Harry Potter", "stand", ("Magicians_156",), "2737377",
            "https://makerworld.com/en/makerlab/parametricModelMaker?designId=2737377"),
    Product("barbie", "Barbie", "stand", ("Magicians_117",), "3135510",
            "https://makerworld.com/en/makerlab/parametricModelMaker?designId=3135510"),
)
BY_KEY = {p.key: p for p in PRODUCTS}
_BY_SKU = {s.lower(): p for p in PRODUCTS for s in p.skus}
STANDS = tuple(p for p in PRODUCTS if p.kind == "stand")

# 0.29: what can be batched and what goes to the shared records (stands come in the next steps)
MAKEABLE = (ORGANIZER.key,)
SHARED = (ORGANIZER.key,)

# the report's customisation line holding the name, per kind
NAME_FIELDS = {"organizer": ("enter your first name",), "stand": ("write your name here.", "write your name here")}


def by_sku(sku: str | None) -> Product | None:
    """The product of a report SKU (any letter case); None = a product SnapSTL doesn't make."""
    return _BY_SKU.get((sku or "").strip().lower())


def of_order(o: dict) -> Product:
    """An order's product (orders from before 0.29 have no SKU other than Magicians_35 / Adhoc)."""
    return by_sku(o.get("sku")) or ORGANIZER


def canonical_sku(sku: str) -> str:
    """The SKU as SnapSTL stores it (the spelling in PRODUCTS)."""
    p = by_sku(sku)
    if p is None:
        return sku
    return next(s for s in p.skus if s.lower() == sku.strip().lower())


def key_sql(col: str = "sku") -> str:
    """SQL: the product key of an order row (anything unknown counts as the organizer, as before 0.29)."""
    whens = " ".join(f"WHEN '{s.lower()}' THEN '{p.key}'" for p in PRODUCTS if p is not ORGANIZER for s in p.skus)
    return f"(CASE LOWER(COALESCE({col},'')) {whens} ELSE '{ORGANIZER.key}' END)"


def in_sql(keys, col: str = "sku") -> str:
    """SQL: the order row is one of these products."""
    keys = [k for k in keys if k in BY_KEY]
    return f"{key_sql(col)} IN ({', '.join(repr(k) for k in keys) or 'NULL'})"


def organizer_sql(col: str = "sku") -> str:
    return in_sql([ORGANIZER.key], col)


def public() -> list[dict]:
    """For the page: every product with its kind."""
    return [{"key": p.key, "label": p.label, "kind": p.kind, "skus": list(p.skus), "makeable": p.key in MAKEABLE}
            for p in PRODUCTS]
