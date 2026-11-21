"""The single-file page must declare the helpers its Slicing page uses (lost once in 0.22.22: the
profiles card stayed on "Loading…" and Save could not work)."""
import re

from snapstl import server

# "//" that starts a comment: not in a URL (http://), not right after a quote, backtick or backslash
_COMMENT = r"(?<![:'\x22\x60\x5c/])//"


def test_slicing_page_helpers_are_declared():
    page = (server.STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("SLPR", "SLP", "slName", "SLOTS", "SRC_LABEL", "CLOUD_WHY", "putJSON"):
        assert re.search(rf"\b(?:let|const)\s+(?:\w+=0,)?{name}\b", page), f"{name} is used but never declared"


def test_print_page_globals_are_declared():
    """0.28.1 went out with a `// comment` that swallowed the rest of its line (MKSEL): Print showed no plates."""
    page = (server.STATIC / "index.html").read_text(encoding="utf-8")
    script = page[page.index("<script>"):]
    for name in ("MKSEL", "OPTLBL", "MKGOING", "MKFAST", "MKSTEP", "MERGESEL", "MKBSUB"):
        hit = re.search(rf"\b(?:let|const)\s+(?:[\w$]+=[^;]*?,)?{name}\b", script)
        assert hit, f"{name} is used but never declared"
        line = script[script.rfind("\n", 0, hit.start()) + 1:hit.start()]
        assert not re.search(_COMMENT, line), f"{name} is declared after a // comment on its line"
    # code glued to the end of a // comment (no line break in between)
    glued = re.findall(_COMMENT + r"[^\n]*?[a-z.)](?:const|let) [A-Za-z_$]+\s*=", script)
    assert not glued, glued
