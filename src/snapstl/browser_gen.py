"""Generate models on the real MakerWorld page in a Chrome window owned by SnapSTL.

Why a browser: MakerWorld only processes generate requests that come from its own
web page (the same request sent by a plain program never gets an answer). So SnapSTL
opens its own visible Chrome window, signs it in with the cookie you pasted in Settings,
fills the model-maker form exactly like a person would, presses Generate and keeps the
two model files the page downloads (plate 1 = base, plate 2 = name).

If MakerWorld shows a security check, complete it in that window — SnapSTL waits and
never tries to get around it.
"""
from __future__ import annotations

import re
import time

from .makerworld import LoginExpired, MakerWorldError, parse_cookie
from .settings import APP_DIR, MAKERWORLD_URL, ModelParams

PROFILE_DIR = APP_DIR / "chrome-profile"
LOAD_TIMEOUT_S = 120        # page load without a security check
# A security check has no time limit: SnapSTL waits until you complete it in the window.
GENERATE_TIMEOUT_S = 180

# Runs inside the MakerWorld page: fills the form and clicks Generate.
DRIVER_JS = r"""
async (p) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const TEXT = "input:not([type=range]):not([type=hidden]):not(.MuiSelect-nativeInput)";
  const field = (label) => {
    const xp = `//div[contains(@class,'MuiAccordionDetails-root')]//*[normalize-space(text())='${label}']/ancestor::div[.//input][1]`;
    const n = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    if (!n) throw new Error(`Can't find the "${label}" control (the MakerWorld page may have changed)`);
    return n;
  };
  const setNative = (input, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const menuOpen = () => [...document.querySelectorAll(".MuiMenu-root, .MuiPopover-root")]
    .some((m) => m.getAttribute("aria-hidden") !== "true" && getComputedStyle(m).visibility !== "hidden");
  async function setText(label, value) {
    const box = field(label).querySelector(TEXT);
    box.focus(); setNative(box, value); box.blur(); await sleep(80);
    if (field(label).querySelector(TEXT).value !== value) throw new Error(`Couldn't set ${label}`);
  }
  async function setNumber(label, value) {
    const box = field(label).querySelector(TEXT);
    box.focus(); setNative(box, String(value));
    for (const t of ["keydown", "keyup"]) box.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
    box.blur(); await sleep(150);
    const r = field(label).querySelector("input[type=range]");
    if (r && r.value !== String(value)) { setNative(r, String(value)); await sleep(150); }
    const r2 = field(label).querySelector("input[type=range]");
    if (r2 && r2.value !== String(value)) throw new Error(`Couldn't set ${label} to ${value}`);
  }
  async function setSelect(label, value) {
    const combo = () => field(label).querySelector("[role=combobox]");
    if (combo().innerText.trim() === value) return;
    combo().dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
    let item = null;
    for (let i = 0; i < 40 && !item; i++) {
      await sleep(100);
      item = [...document.querySelectorAll("[role=option],[role=menuitem]")].find((li) => li.innerText.trim() === value);
    }
    if (!item) throw new Error(`Option "${value}" not found for ${label}`);
    item.click(); await sleep(250);
    if (combo().innerText.trim() !== value) throw new Error(`Couldn't select ${value} for ${label}`);
  }
  await setSelect("Print version", "NO AMS");
  await setText("Name line 1", p.name);
  await setText("Name line 2", "");
  await setSelect("Font", p.font);
  await setNumber("Font size", p.font_size);
  await setNumber("Thickness", p.thickness);
  await setNumber("Outline", p.outline);
  await setNumber("Compartments", p.compartments);
  await setSelect("Rotate text", p.rotate_text);
  if (p.license_password) await setText("License password", p.license_password);
  let gen = null;
  // the page may still be busy with its own build when it first opens: wait up to 90 s
  for (let i = 0; i < 900 && !gen; i++) {
    if (!menuOpen()) gen = [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Generate" && !b.disabled) || null;
    if (!gen) await sleep(100);
  }
  if (!gen) throw new Error("The Generate button isn't available");
  gen.click();
  return true;
}
"""

ERROR_JS = r"""() => {
  for (const a of document.querySelectorAll("[role=alert]")) {
    const t = a.innerText.trim();
    if (/fail|error|invalid/i.test(t)) return t;
  }
  return "";
}"""

READY_JS = r"""() => [...document.querySelectorAll("button")].some((b) => b.textContent.trim() === "Generate")"""

BLOCKED_JS = r"""() => {
  const t = (document.title || "").toLowerCase();
  if (t.includes("just a moment") || t.includes("attention required")) return true;
  return !!document.querySelector("iframe[src*='challenges.cloudflare.com'], #challenge-form, #cf-challenge-running");
}"""


class Interrupted(MakerWorldError):
    """Downloads were paused while waiting — the current name goes back to the queue untouched."""


def cookies_for_browser(cookie_header: str) -> list[dict]:
    return [
        {"name": k, "value": v, "domain": ".makerworld.com", "path": "/", "secure": True, "sameSite": "Lax"}
        for k, v in parse_cookie(cookie_header).items()
    ]


class BrowserMakerWorld:
    """Owns one visible Chrome window. Use from a single thread (the downloader thread)."""

    def __init__(self, cookie: str, status=lambda s: None, attention=lambda on, msg="": None,
                 should_stop=lambda: False, front_requested=lambda: False):
        self.cookie = cookie
        self.status = status
        self.attention = attention          # tell the UI the user must act in the window
        self.should_stop = should_stop      # True when downloads are paused
        self.front_requested = front_requested
        self._pw = None
        self._ctx = None
        self.page = None
        self._captured: dict[str, object] = {}

    # ---------- lifecycle ----------
    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        # chromium_sandbox=True: keep Chrome's normal security sandbox (Playwright turns it off by default
        # with --no-sandbox, which is less safe and shows a warning bar).
        kw = dict(headless=False, chromium_sandbox=True, viewport={"width": 1280, "height": 860})
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(str(PROFILE_DIR), channel="chrome", **kw)
        except Exception:  # noqa: BLE001 — no Google Chrome: use Playwright's Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kw)
        self._ctx.add_cookies(cookies_for_browser(self.cookie))
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.on("response", self._on_response)
        self._load()

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._ctx = self._pw = self.page = None

    @property
    def is_open(self) -> bool:
        return self._ctx is not None

    # ---------- page ----------
    def _on_response(self, r) -> None:
        m = re.search(r"/(plate_\d+|plate_single)\.obj(\?|$)", r.url)
        if m and r.ok:
            self._captured[m.group(1)] = r

    def _tick(self, ms: int = 1000) -> None:
        """Wait a moment; handle 'show window' requests and pausing."""
        if self.front_requested():
            try:
                self.page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
        if self.should_stop():
            raise Interrupted("Downloads paused")
        self.page.wait_for_timeout(ms)

    def blocked(self) -> bool:
        try:
            return bool(self.page.evaluate(BLOCKED_JS))
        except Exception:  # noqa: BLE001 — navigating
            return False

    def wait_for_user(self) -> None:
        """MakerWorld shows a security check: wait (no time limit) until the user completes it."""
        self.attention(True, "MakerWorld security check — complete it in the SnapSTL MakerWorld window")
        self.status("Waiting for you: complete the MakerWorld security check in its window")
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        while self.blocked():
            self._tick(1500)
        self.attention(False)

    def _load(self) -> None:
        self.status("Opening MakerWorld")
        self.page.goto(MAKERWORLD_URL, wait_until="domcontentloaded", timeout=90_000)
        deadline = time.time() + LOAD_TIMEOUT_S
        while True:
            if self.blocked():
                self.wait_for_user()
                deadline = time.time() + LOAD_TIMEOUT_S
                continue
            try:
                ready = self.page.evaluate(READY_JS)
            except Exception:  # noqa: BLE001 — page navigating
                ready = False
            if ready:
                if not self._signed_in():
                    raise LoginExpired("MakerWorld didn't accept the saved cookie — paste a fresh one in Settings")
                self.status("MakerWorld ready")
                return
            if time.time() > deadline:
                raise MakerWorldError("The MakerWorld page didn't load in time")
            self.status("Waiting for the MakerWorld page to load")
            self._tick(1500)

    def _signed_in(self) -> bool:
        try:
            return bool(self.page.evaluate(
                "async () => (await fetch('/api/v1/user-service/my/message/count', {credentials: 'include'})).ok"))
        except Exception:  # noqa: BLE001
            return False

    def generate(self, name: str, model: ModelParams, compartments: int, license_password: str) -> tuple[bytes, bytes]:
        if not self.is_open:
            self.open()
        params = {"name": name, "font": model.Font, "font_size": int(model.Font_size), "thickness": int(model.Thickness),
                  "outline": int(model.Outline), "compartments": int(compartments), "rotate_text": model.Rotate_text,
                  "license_password": license_password}
        for attempt in (1, 2, 3):
            if self.blocked():
                self.wait_for_user()
                self._load()
            self._captured.clear()
            try:
                self.page.evaluate(DRIVER_JS, params)
            except Exception as e:  # noqa: BLE001
                if self.blocked():
                    continue  # security check appeared while filling: wait for the user, then redo
                msg = str(e).split("\n")[0]
                raise MakerWorldError(f"Couldn't fill the MakerWorld form for {name}: {msg}") from e
            deadline = time.time() + GENERATE_TIMEOUT_S
            while time.time() < deadline:
                if "plate_1" in self._captured and "plate_2" in self._captured:
                    return self._captured["plate_1"].body(), self._captured["plate_2"].body()
                if self.blocked():
                    break  # redo this name after the user passes the check
                err = self.page.evaluate(ERROR_JS)
                if err:
                    raise MakerWorldError(f"MakerWorld couldn't generate {name}: {err}")
                self._tick(400)
            else:
                raise MakerWorldError(f"Timed out waiting for MakerWorld to generate {name}")
        raise MakerWorldError(f"MakerWorld kept interrupting {name} with security checks")
