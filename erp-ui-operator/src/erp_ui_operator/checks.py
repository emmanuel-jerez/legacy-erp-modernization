"""Evaluate plan postconditions against the live page."""

from __future__ import annotations

import re
import time
from typing import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .plan import Post

_LINES = re.compile(r"^\s*(No Lines|(\d+) Lines?)\b")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "").strip())
    except ValueError:
        return None


def _first_visible_value(page: Page, css: str) -> str | None:
    loc = page.locator(css).filter(visible=True)
    if loc.count() == 0:
        return None
    return loc.first.input_value()


def evaluate(page: Page, post: Post, db_probe: Callable[[Post], bool] | None = None) -> bool:
    """One non-waiting evaluation of a postcondition. ``db_*`` kinds go to ``db_probe``."""
    if post.kind.startswith("db_"):
        if db_probe is None:
            raise ValueError(f"postcondition {post.kind!r} needs a database probe")
        return db_probe(post)
    try:
        if post.kind == "all":
            return all(evaluate(page, p, db_probe) for p in post.parts)
        if post.kind == "any":
            return any(evaluate(page, p, db_probe) for p in post.parts)
        if post.kind == "visible":
            return page.locator(post.arg("css")).filter(visible=True).count() > 0
        if post.kind == "visible_text":
            exact = re.compile(r"^\s*" + re.escape(post.arg("exact")) + r"\s*$")
            return page.locator(post.arg("css")).filter(visible=True).filter(has_text=exact).count() > 0
        if post.kind == "hidden":
            return page.locator(post.arg("css")).filter(visible=True).count() == 0
        if post.kind == "value":
            got = _first_visible_value(page, post.arg("css"))
            if got is None:
                return False
            got, expected, mode = _norm(got), _norm(post.arg("expected")), post.arg("mode")
            if mode == "equals":
                return got == expected
            if mode == "startswith":
                return got.startswith(expected)
            if mode == "nonempty":
                return got != ""
            if mode == "empty":
                return got == ""
            if mode == "positive":
                return (_number(got) or 0) > 0
            if mode == "number":
                return _number(got) is not None and _number(got) == _number(expected)
            raise ValueError(f"unknown value mode {mode!r}")
        if post.kind == "saved":
            status = page.locator(".adwindow-status-docstatus").filter(visible=True)
            info = page.locator(".adwindow-status-docinfo").filter(visible=True)
            if status.count() == 0 or info.count() == 0:
                return False
            if "Record saved" not in status.first.inner_text():
                return False
            m = _LINES.match(info.first.inner_text())
            if not m:
                return False
            lines = 0 if m.group(1) == "No Lines" else int(m.group(2))
            if lines != post.arg("lines"):
                return False
            docno = page.locator('[instancename="C_Order0DocumentNo"]')
            if docno.count() and docno.first.is_visible() and docno.first.input_value().startswith("<"):
                return False
            return True
    except PlaywrightError:
        return False
    raise ValueError(f"unknown postcondition kind {post.kind!r}")


def wait_until(page: Page, post: Post, timeout_ms: int = 6000, poll_ms: int = 250) -> bool:
    """Poll a postcondition until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if evaluate(page, post):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(poll_ms)
