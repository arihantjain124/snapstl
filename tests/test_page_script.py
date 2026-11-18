"""The single-file page must declare the helpers its Slicing page uses (lost once in 0.22.22: the
profiles card stayed on "Loading…" and Save could not work)."""
import re

from snapstl import server


def test_slicing_page_helpers_are_declared():
    page = (server.STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("SLPR", "SLP", "slName", "SLOTS", "SRC_LABEL", "CLOUD_WHY", "putJSON"):
        assert re.search(rf"\b(?:let|const)\s+(?:\w+=0,)?{name}\b", page), f"{name} is used but never declared"
