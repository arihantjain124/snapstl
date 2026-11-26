"""App settings: model-maker parameters, scaling table, plate/fit options.

Persisted as JSON in the user's Application Support folder. The license
passwords are stored here per MakerWorld model (license_passwords, keyed by designId); the file is
written readable by this user only.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .platform_os import app_dir

# SNAPSTL_HOME lets tests/dev runs use a separate settings + cache folder.
# Mac: ~/Library/Application Support/SnapSTL · Windows: %LOCALAPPDATA%\SnapSTL
APP_DIR = app_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
_DEV = bool(os.environ.get("SNAPSTL_HOME"))
DEFAULT_OUTPUT_DIR = (APP_DIR / "output") if _DEV else Path.home() / "SnapSTL" / "output"
# Downloaded MakerWorld models: a visible folder the user can browse/clean in Finder (cache.py).
DEFAULT_DOWNLOADS_DIR = (APP_DIR / "downloads") if _DEV else Path.home() / "SnapSTL" / "Downloads"

MAKERWORLD_URL = (
    "https://makerworld.com/en/makerlab/parametricModelMaker?designId=2600234"
    "&from=model_page&modelName=Custom_Name_Desk_Organizer_v2.34.scad"
    "&protected=true&unikey=70189420-efcd-4593-a07c-771fe72d52b3"
)

# Choices exactly as offered by the MakerWorld model (Custom_Name_Desk_Organizer_v2.34.scad).
FONTS = ["Chewy", "Luckiest Guy", "Titan One", "DynaPuff", "Cherry Bomb One", "Akaya Kanadaka"]
ROTATE_TEXT = ["no", "yes"]
NUMBER_RANGES = {
    "Font_size": (30, 120),
    "Thickness": (30, 120),
    "Outline": (4, 15),
}
COMPARTMENT_RANGE = (1, 10)


@dataclass
class ModelParams:
    """Static model-maker parameters applied to every name in a batch."""

    Font: str = "Chewy"
    Font_size: int = 60
    Thickness: int = 50
    Outline: int = 6
    Rotate_text: str = "no"
    # Compartments are NOT a global option: they come from the scaling table (by letter count).

    def validate(self) -> list[str]:
        errs = []
        if self.Font not in FONTS:
            errs.append(f"Unknown font {self.Font!r}")
        if self.Rotate_text not in ROTATE_TEXT:
            errs.append(f"Rotate text must be one of {ROTATE_TEXT}")
        for k, (lo, hi) in NUMBER_RANGES.items():
            v = getattr(self, k)
            if not (lo <= int(v) <= hi):
                errs.append(f"{k} must be between {lo} and {hi}")
        return errs


@dataclass
class ScaleBand:
    min_letters: int
    max_letters: int  # inclusive; use 999 for "and up"
    length: float  # target organizer length (X), mm
    height: float  # target organizer height (Z), mm
    compartments: int = 5  # model-maker "Compartments" for names in this band


def default_bands() -> list[ScaleBand]:
    # Scaling Rules v1.1 — a default pick inside each agreed range.
    return [
        ScaleBand(1, 3, 195, 90, 4),
        ScaleBand(4, 4, 200, 90, 4),
        ScaleBand(5, 5, 210, 89, 5),
        ScaleBand(6, 7, 222, 87.5, 5),
        ScaleBand(8, 10, 232, 86.5, 6),
        ScaleBand(11, 999, 240, 86, 6),
    ]


@dataclass
class ScalingSettings:
    bands: list[ScaleBand] = field(default_factory=default_bands)
    base_y_scale: float = 1.0  # Base Y stays 100 %
    name_z_scale: float = 0.5  # Name Z fixed 50 %
    min_height: float = 85.0
    max_height: float = 90.0
    max_length: float = 240.0


@dataclass
class PlateSettings:
    bed_x: float = 256.0
    bed_y: float = 256.0
    bed_z: float = 256.0
    margin: float = 5.0  # clearance from bed edge, mm
    gap: float = 5.0  # min clearance between parts, mm
    max_names_per_plate: int = 3
    bases_per_plate: int = 3
    rotations: list[int] = field(default_factory=lambda: [0, 180])  # degrees about Z only


@dataclass
class StandBand:
    """Name stands (0.29): size by letter count (Settings → Name stands)."""
    min_letters: int
    max_letters: int  # inclusive; 999 = "and up"
    length: float  # X, mm
    height: float  # letter height when it stands (Y on the bed), mm


def default_stand_bands() -> list[StandBand]:
    # starting values (lengths as the organizer's): the owner checks them on the first prints
    return [StandBand(1, 3, 195, 40), StandBand(4, 4, 200, 40), StandBand(5, 5, 210, 40),
            StandBand(6, 7, 222, 40), StandBand(8, 10, 232, 40), StandBand(11, 999, 240, 40)]


def default_stand_products() -> dict:
    """Per product: letter spacing (MakerWorld's default) and the two filament colours (owner, 0.29)."""
    return {"gta": {"letter_spacing": 25, "base_color": "black", "letter_color": "white"},
            "harry_potter": {"letter_spacing": 0, "base_color": "yellow", "letter_color": "black"},
            "barbie": {"letter_spacing": 0, "base_color": "hot pink", "letter_color": "white"}}


@dataclass
class StandSettings:
    """Name stands (GTA, Harry Potter, Barbie): one block of settings for all of them (owner, 0.29).
    MakerWorld settings not here stay at the model's default (place in printing position on, flat bottom
    cut 0.5, units per em, preview colours)."""
    bands: list[StandBand] = field(default_factory=default_stand_bands)
    thickness: float = 40.0        # total, mm: back thickness = thickness − letter protrusion
    letter_protrusion: float = 3.0  # raised letters, mm (the top colour)
    font_size: float = 40.0
    brim_offset: float = 6.0        # how far the coloured block grows around the letters, mm
    products: dict = field(default_factory=default_stand_products)

    def validate(self) -> list[str]:
        errs = []
        if not (10 <= self.thickness <= 80):
            errs.append("Name stands: thickness must be 10–80 mm")
        if not (0.5 <= self.letter_protrusion <= 10) or self.letter_protrusion >= self.thickness:
            errs.append("Name stands: letter protrusion must be 0.5–10 mm and less than the thickness")
        if not (10 <= self.font_size <= 200):
            errs.append("Name stands: font size must be 10–200")
        if not (0 <= self.brim_offset <= 10):
            errs.append("Name stands: brim offset must be 0–10 mm")
        if not self.bands:
            errs.append("Name stands: the size table is empty")
        for b in self.bands:
            if not (20 <= b.length <= 246 and 10 <= b.height <= 120):
                errs.append(f"Name stands: {b.min_letters}+ letters — length 20–246 mm, height 10–120 mm")
                break
        for k, v in (self.products or {}).items():
            if not isinstance(v, dict) or not (-200 <= float(v.get("letter_spacing", 0)) <= 200) \
                    or not str(v.get("base_color", "")).strip() or not str(v.get("letter_color", "")).strip():
                errs.append(f"Name stands: {k} needs a letter spacing (−200–200) and both colours")
        return errs


def default_messages() -> dict:
    """Ready-made replies on the logistics page (0.25). {customer} {order_id} {name} {colour} {tracking} {address}
    {needed_by} {courier} {delivered} are filled in from the order. The page offers each one only for the
    statuses it fits (`MSGFOR` in index.html); messages the owner adds are offered for every order."""
    return {
        "Sorry for the wait": "Hello {customer}, we're sorry your name organizer \"{name}\" (order {order_id}) is "
                              "taking longer than usual. We have a lot of orders right now and are making them "
                              "in order. Yours is in the queue and we'll send you the tracking link as soon as it "
                              "ships. Thank you for your patience!",
        "Printing now": "Hello {customer}, good news: your \"{name}\" name organizer in {colour} (order {order_id}) "
                        "is being printed now. We'll send you the tracking link as soon as it ships.",
        "Packed, ships soon": "Hello {customer}, your \"{name}\" name organizer (order {order_id}) is made and "
                              "packed. It goes to the courier soon and we'll send you the tracking link.",
        "Shipped – tracking": "Hello {customer}, your order {order_id} is on its way with {courier}. "
                              "Track it here: {tracking}",
        "Delivered – review": "Hello {customer}, your order {order_id} was delivered{delivered}. We hope you love "
                              "your \"{name}\" name organizer! A short review or a photo would mean a lot to us.",
        "Couldn't deliver": "Hello {customer}, the courier couldn't deliver order {order_id}. Please reply with "
                            "your full address and a phone number that works, so we can send it again.",
        "Confirm COD order": "Hello {customer}, thank you for ordering a name organizer \"{name}\" in {colour} "
                             "(order {order_id}, cash on delivery). Please reply YES to confirm, and we'll make it.",
        "Express": "Hello {customer}, we've marked order {order_id} as express. We'll do our best to "
                   "deliver it by {needed_by}.",
        "Confirm address": "Hello {customer}, please confirm the delivery address for order {order_id}: {address}",
    }


def old_default_messages() -> dict:
    """The messages before 0.25: replaced by the new ones when the owner never changed them."""
    return {
        "Being made": "Hello {customer}, your name organizer \"{name}\" (order {order_id}) is being made. "
                      "We'll send you the tracking link as soon as it ships.",
        "Shipped": "Hello {customer}, your order {order_id} has been shipped. Track it here: {tracking}",
        "Delivered": "Hello {customer}, your order {order_id} has been delivered. We hope you love it!",
        "Express": "Hello {customer}, we've marked order {order_id} as express. We'll do our best to "
                   "deliver it by {needed_by}.",
        "Confirm address": "Hello {customer}, please confirm the delivery address for order {order_id}: {address}",
    }


@dataclass
class LogisticsSettings:
    """The logistics page (label and customer-care team), 0.23."""
    # the webstore's own tracking page; {awb} and {order_id} are filled in (owner's ClickPost link)
    tracking_url: str = "https://shopdeck.clickpost.ai/en?waybill={awb}&cp_id=105"
    late_days: int = 7          # red when not yet in Label done this many days after the order date
    express_red_days: int = 3   # Express turns red this many days before the needed-by date
    stuck_days: int = 5         # On the way longer than this = stuck (red)
    messages: dict = field(default_factory=default_messages)


COURIERS = ("BLUEDART_AIR", "BLUEDART_SURFACE", "DELHIVERY_SURFACE", "XPRESSBEES")  # owner's order, 2026-11-24


@dataclass
class LabelSettings:
    """Shipping labels from ShopDeck (0.33, labels.py): the SnapSTL Helper changes the courier and presses
    Label in the person's signed-in Chrome; SnapSTL checks the PDF and prints it."""
    enabled: bool = False
    printer: str = "iDPRT SP410BT"       # the label printer's name in Windows / macOS (4x6 labels)
    wait_after_logistics_s: int = 60    # ShopDeck may hand out the old label right after a courier change
    wait_after_regenerate_s: int = 15   # 0.36.2: after Regenerate → Confirm, before looking in Print again
    pack_wait_s: int = 60               # 0.36.2: the label PDF had the old AWB → wait, then download it from Pack
    pack_tries: int = 3                 # 0.36.2: … at most this often, then the job stops
    couriers: list = field(default_factory=lambda: list(COURIERS))  # first one offered wins
    one_at_a_time: bool = True          # one Print label press = one order (False: a selection, one after another)
    regenerate_timeout_s: int = 180     # Expired AWB → Regenerate: how long to wait for the order to come back
    pdf_timeout_s: int = 120            # after Label: how long to wait for the PDF in Downloads
    pickup_timeout_s: int = 30          # 0.33.2: no ShopDeck tab took the job by then → stopped
    stall_timeout_s: int = 60           # 0.33.2: the ShopDeck tab went quiet (in the background, closed) → stopped
    sumatra_path: str = ""              # SumatraPDF.exe (Windows); empty = look in the usual places


@dataclass
class Settings:
    model: ModelParams = field(default_factory=ModelParams)
    scaling: ScalingSettings = field(default_factory=ScalingSettings)
    plate: PlateSettings = field(default_factory=PlateSettings)
    logistics: LogisticsSettings = field(default_factory=LogisticsSettings)
    stands: StandSettings = field(default_factory=StandSettings)
    labels: LabelSettings = field(default_factory=LabelSettings)
    output_dir: str = str(DEFAULT_OUTPUT_DIR)
    downloads_dir: str = str(DEFAULT_DOWNLOADS_DIR)  # downloaded models, kept until deleted in Finder
    makerworld_url: str = MAKERWORLD_URL  # the model-maker page the helper must be on (its designId is checked)
    lan_access: bool = False  # enable the logistics page on port 8002 (needs a password)
    license_passwords: dict = field(default_factory=dict)  # MakerWorld license password per designId

    @property
    def design_id(self) -> str:
        return design_id(self.makerworld_url) or ""

    @property
    def license_password(self) -> str:
        """License password of the model currently set in makerworld_url."""
        return str(self.license_passwords.get(self.design_id, ""))
    delay_between_downloads_s: float = 3.0
    daily_target: int = 0  # Stats: orders to make per day (0 = not set)
    report_import: bool = True  # import new Order Reconciliation Reports from the Downloads folder by itself
    help_access: bool = False  # the customer help page on port 8003 (its own password)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        if "model" in d:
            s.model = ModelParams(**_known(ModelParams, {**asdict(s.model), **d["model"]}))
        if "scaling" in d:
            sc = d["scaling"]
            bands = [ScaleBand(**_known(ScaleBand, b)) for b in sc.get("bands", [])] or default_bands()
            rest = {k: v for k, v in sc.items() if k != "bands"}
            s.scaling = ScalingSettings(bands=bands, **_known(ScalingSettings, {**_scalar_fields(s.scaling), **rest}))
        if "plate" in d:
            s.plate = PlateSettings(**_known(PlateSettings, {**asdict(s.plate), **d["plate"]}))
        if isinstance(d.get("logistics"), dict):
            s.logistics = LogisticsSettings(**_known(LogisticsSettings, {**asdict(s.logistics), **d["logistics"]}))
            if not isinstance(s.logistics.messages, dict) or not s.logistics.messages \
                    or s.logistics.messages == old_default_messages():
                s.logistics.messages = default_messages()
        if isinstance(d.get("stands"), dict):
            st = d["stands"]
            bands = [StandBand(**_known(StandBand, b)) for b in st.get("bands") or []] or default_stand_bands()
            prods = {**default_stand_products(), **{k: {**default_stand_products().get(k, {}), **v}
                                                     for k, v in (st.get("products") or {}).items()
                                                     if isinstance(v, dict)}}
            s.stands = StandSettings(bands=bands, products=prods, **{k: v for k, v in _known(StandSettings, st).items()
                                                                      if k != "products"})
        if isinstance(d.get("labels"), dict):
            s.labels = LabelSettings(**_known(LabelSettings, {**asdict(s.labels), **d["labels"]}))
        for k in ("output_dir", "downloads_dir", "makerworld_url", "lan_access", "delay_between_downloads_s",
                  "license_passwords", "daily_target", "report_import", "help_access"):
            if k in d:
                setattr(s, k, d[k])
        return s

    def validate(self) -> list[str]:
        errs = self.model.validate() + self.stands.validate()
        if design_id(self.makerworld_url) is None:
            errs.append("MakerWorld model page link must be a makerworld.com …/makerlab/parametricModelMaker?designId=… link")
        p = self.plate
        if p.margin < 0 or p.gap < 0:
            errs.append("Margin and gap must be ≥ 0")
        if not p.rotations or any(r % 90 for r in p.rotations):
            errs.append("Rotations must be multiples of 90°")
        if not (1 <= p.max_names_per_plate <= 3):
            errs.append("Names per plate must be 1–3")
        if not (1 <= p.bases_per_plate <= 3):
            errs.append("Bases per plate must be 1–3")
        lg = self.logistics
        if lg.tracking_url and not (lg.tracking_url.startswith("https://")
                                    and ("{awb}" in lg.tracking_url or "{order_id}" in lg.tracking_url)):
            errs.append("Tracking link must start with https:// and contain {awb} or {order_id}")
        for k in ("late_days", "express_red_days", "stuck_days"):
            v = getattr(lg, k)
            if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 60):
                errs.append("Logistics day limits must be whole numbers 0–60")
                break
        if not isinstance(lg.messages, dict) or len(lg.messages) > 20 or not all(
                isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 40 and len(v) <= 1000
                for k, v in lg.messages.items()):
            errs.append("Messages: at most 20, each with a name (up to 40 letters) and text (up to 1000)")
        lb = self.labels
        if not isinstance(lb.printer, str) or len(lb.printer) > 200 or (lb.enabled and not lb.printer.strip()):
            errs.append("Labels: choose the label printer")
        for k, lo, hi in (("wait_after_logistics_s", 0, 600), ("regenerate_timeout_s", 30, 1800), ("pdf_timeout_s", 20, 600),
                          ("pickup_timeout_s", 10, 600), ("stall_timeout_s", 20, 600),
                          ("wait_after_regenerate_s", 0, 600), ("pack_wait_s", 0, 600), ("pack_tries", 0, 10)):
            v = getattr(lb, k)
            if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                errs.append(f"Labels: {k.replace('_', ' ')} must be a whole number {lo}–{hi}")
        if not isinstance(lb.couriers, list) or not lb.couriers or not all(
                isinstance(c, str) and 0 < len(c.strip()) <= 40 for c in lb.couriers):
            errs.append("Labels: list at least one courier, as ShopDeck names it (e.g. BLUEDART_AIR)")
        bands = sorted(self.scaling.bands, key=lambda b: b.min_letters)
        if not bands:
            errs.append("Scaling table is empty")
        lo, hi = COMPARTMENT_RANGE
        for b in bands:
            if not (lo <= int(b.compartments) <= hi):
                errs.append(f"Compartments for {b.min_letters}+ letters must be {lo}–{hi}")
        return errs


def design_id(url: str) -> str | None:
    """designId of a MakerWorld Parametric Model Maker link, or None if it isn't one."""
    from urllib.parse import parse_qs, urlparse

    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return None
    if u.scheme != "https" or u.hostname not in ("makerworld.com", "www.makerworld.com"):
        return None
    if "/makerlab/parametricModelMaker" not in u.path:
        return None
    d = (parse_qs(u.query).get("designId") or [""])[0]
    return d if d.isdigit() else None


def _known(cls, d: dict) -> dict:
    names = set(cls.__dataclass_fields__)
    return {k: v for k, v in d.items() if k in names and k != "bands"}


def _scalar_fields(sc: ScalingSettings) -> dict:
    d = asdict(sc)
    d.pop("bands")
    return d


def load_settings() -> Settings:
    try:
        return Settings.from_dict(json.loads(SETTINGS_PATH.read_text()))
    except FileNotFoundError:
        return Settings()
    except (json.JSONDecodeError, TypeError, ValueError):
        return Settings()


def save_settings(s: Settings) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(SETTINGS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds license passwords
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(s.to_dict(), indent=2))
    os.chmod(SETTINGS_PATH, 0o600)
