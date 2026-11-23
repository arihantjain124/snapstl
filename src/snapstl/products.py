"""Products SnapSTL makes (0.29): which webstore SKU is which product, and what kind of product it is.

* **organizer** — the personalised name desk organizer (`Magicians_35`, and orders typed in by hand, `Adhoc`):
  a base and a separate name, printed apart and glued (everything before 0.29).
* **stand** — a name stand (GTA, Harry Potter, Barbie style): one piece in two colours made on MakerWorld's
  parametric model maker (no license password, no watermark), printed in one go on a printer with an AMS:
  the coloured block at the bottom, the raised letters on top (one filament change).
  Their size, font settings and colours are in Settings → Name stands (`settings.StandSettings`).

Adding a product = one entry here (and its defaults in `settings.default_stand_products`). Static 3MF products
(the same file arranged on a plate) will be a third kind.

0.29 (step 1 of the name stands): stand orders are read from the report and listed on the Orders page.
0.30 (step 2): stand orders can be batched (one product per batch) and their models downloaded by the helper.
A stand order reaches the shared records only once it's taken into a batch (`shared_sql`): a computer before
0.29 would take a *ready* one for an organizer, and claims must be shared so no stand is made twice.
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
    # the owner's links: these designs are protected — without protected/unikey the page shows its code panel
    # instead of the settings form (checked 2026-11-24)
    Product("gta", "GTA", "stand", ("Magicians_174",), "2737384",
            "https://makerworld.com/en/makerlab/parametricModelMaker?designId=2737384&from=model_page"
            "&modelName=Name+Sign+-+GTA+v1.1.scad&protected=true&unikey=0d144621-721f-4385-b196-38c349c1bc44",
            font_features=True),
    Product("harry_potter", "Harry Potter", "stand", ("Magicians_156",), "2737377",
            "https://makerworld.com/en/makerlab/parametricModelMaker?designId=2737377&from=model_page"
            "&modelName=Name+Sign++-+Harry+Potter+v1.0.scad&protected=true&unikey=ea4c925e-3741-41fb-87b6-e53b9843bfff"),
    Product("barbie", "Barbie", "stand", ("Magicians_117",), "3135510",
            "https://makerworld.com/en/makerlab/parametricModelMaker?designId=3135510&from=model_page"
            "&modelName=Name+Sign+-+Retro+Doll+v1.0.scad&protected=true&unikey=55113949-2b25-491a-8aed-2a9e130f9f13"),
)
BY_KEY = {p.key: p for p in PRODUCTS}
_BY_SKU = {s.lower(): p for p in PRODUCTS for s in p.skus}
STANDS = tuple(p for p in PRODUCTS if p.kind == "stand")

# what can be batched (0.30: every product) and what the shared records take in (0.29 computers ignore stands)
MAKEABLE = tuple(p.key for p in PRODUCTS)
SHARED = tuple(p.key for p in PRODUCTS)
BUILDABLE = (ORGANIZER.key,)  # plates (0.30: the organizer; name stands in the next update)

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


def shared_sql(t: str = "orders") -> str:
    """SQL: this order may go to the shared records — an organizer always, a name stand once it's in a batch
    (or already there)."""
    return (f"({organizer_sql(t + '.sku')} OR {t}.state IN ('in_batch','bundled','printed') "
            f"OR COALESCE({t}.in_remote,0)=1)")


def stand_job(name: str, product: "Product", st) -> dict:
    """The MakerWorld form for one name stand (helper 2.4): label on the page → value. Settings not listed
    keep the model's default. Back thickness = total thickness − raised letters."""
    fields = [["text input", "text", name],
              ["font size", "number", _num(st.font_size)],
              ["letter spacing", "number", _num((st.products.get(product.key) or {}).get("letter_spacing", 0))],
              ["back thickness", "number", _num(st.thickness - st.letter_protrusion)],
              ["letter protrusion", "number", _num(st.letter_protrusion)],
              ["brim offset", "number", _num(st.brim_offset)],
              ["flat bottom cut", "number", 0.5],
              ["Place in printing position", "check", True]]
    if product.font_features:
        fields.append(["Use Font Features", "check", True])
    return {"product": product.key, "design": product.design_id, "url": product.url, "fields": fields}


def _num(v):
    v = float(v)
    return int(v) if v.is_integer() else round(v, 2)


def public() -> list[dict]:
    """For the page: every product with its kind."""
    return [{"key": p.key, "label": p.label, "kind": p.kind, "skus": list(p.skus), "makeable": p.key in MAKEABLE,
             "buildable": p.key in BUILDABLE}
            for p in PRODUCTS]
