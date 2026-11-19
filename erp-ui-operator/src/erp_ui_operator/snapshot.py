"""Compact, normalized page snapshots for the LLM fallback.

The browser side (``COLLECT_JS``) lists the visible interactive elements in the regions
the task may touch and marks each with ``data-uiop-idx`` so an index the model picks can
be resolved back to the element. The Python side (``normalize``) turns that raw list into
a stable text form:

* no ZK component ids (they change on every page load);
* dates, times, long numbers and run identifiers replaced by placeholders;
* whitespace collapsed and keyboard-shortcut suffixes removed from titles;
* a fixed element cap.

Two runs that reach the same UI state therefore produce the same snapshot text, which is
what lets cassette replay work: the prompt hash depends on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_ELEMENTS = 90
MAX_TEXT = 60
INPUT_ROLES = frozenset({"textbox", "password", "combobox", "date", "number"})

# Runs in the page. Returns raw element records; never returns ZK ids.
COLLECT_JS = r"""
(maxEls) => {
  for (const e of document.querySelectorAll('[data-uiop-idx]')) e.removeAttribute('data-uiop-idx');
  const visible = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const text = (e) => (e ? (e.innerText || e.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const dialogs = [...document.querySelectorAll('.z-window-highlighted, .z-window-modal, .z-messagebox-window')]
    .filter(visible);
  const dialog = dialogs.length ? dialogs[dialogs.length - 1] : null;
  const root = dialog || document.body;
  // Confirm boxes carry their intent ("Delete record?") in the body, not the title.
  const dialogText = dialog ? text(dialog).slice(0, 2000) : '';
  const EXCLUDE = '.desktop-left-column, .desktop-right-column, .desktop-east, .desktop-user-panel, '
    + '.desktop-tabbox > .z-tabs, .window-container-toolbar-btn, .z-west, .z-east';
  const SEL = 'input:not([type=hidden]), textarea, button:not(.z-focus-a), a[role=button], a.z-toolbarbutton, '
    + 'li.z-tab, li.z-comboitem, tr.z-listitem, td.z-treecell';
  const regionOf = (e) => {
    if (dialog) {
      const title = text(dialog.querySelector('.z-window-header')) || 'dialog';
      return 'dialog: ' + title;
    }
    if (e.closest('[class*="login"]')) return 'login';
    if (e.closest('.z-bandbox-popup, .z-bandbox-pp')) return 'search-results';
    if (e.closest('.desktop-header')) return 'global-search';
    if (e.closest('.adwindow-detailpane')) return 'detail-pane';
    if (e.closest('.adwindow-toolbar')) return 'window-toolbar';
    if (e.closest('.adwindow-breadcrumb')) return 'breadcrumb';
    if (e.closest('.adtab-form')) return 'form';
    if (e.closest('.z-combobox-popup')) return 'dropdown';
    return 'page';
  };
  const roleOf = (e) => {
    const c = (e.className || '').toString();
    const t = e.tagName.toLowerCase();
    if (t === 'input') {
      if (e.type === 'checkbox') return 'checkbox';
      if (e.type === 'password') return 'password';
      if (c.includes('z-combobox-input') || c.includes('z-bandbox-input')) return 'combobox';
      if (c.includes('z-datebox-input')) return 'date';
      if (c.includes('z-decimalbox') || c.includes('z-numberbox') || c.includes('z-intbox')) return 'number';
      return 'textbox';
    }
    if (t === 'textarea') return 'textbox';
    if (t === 'li' && c.includes('z-tab')) return 'tab';
    if (t === 'li') return 'option';
    if (t === 'tr') return 'row';
    if (t === 'td') return 'menuitem';
    return 'button';
  };
  const labelOf = (e) => {
    const cell = e.closest('td.z-cell');
    if (cell && cell.previousElementSibling) {
      const l = cell.previousElementSibling.querySelector('.form-label .z-label');
      if (l) return text(l);
    }
    const lrow = e.closest('tr');
    if (lrow) {
      const l = lrow.querySelector(':scope > td.login-label');
      if (l) return text(l);
    }
    if (e.type === 'checkbox') {
      const c = e.parentElement && e.parentElement.querySelector('.z-checkbox-content');
      if (c && text(c)) return text(c);
    }
    const wrap = e.closest('[title]');
    return e.getAttribute('aria-label') || e.title || (wrap && wrap !== document.body ? wrap.title : '')
      || e.placeholder || '';
  };
  const out = [];
  for (const e of root.querySelectorAll(SEL)) {
    if (out.length >= maxEls) break;
    if (!visible(e) || e.closest(EXCLUDE)) continue;
    const role = roleOf(e);
    const isInput = ['textbox', 'password', 'combobox', 'date', 'number', 'checkbox'].includes(role);
    let value = '';
    if (isInput && role !== 'checkbox') value = e.value || '';
    if (role === 'password' && value) value = '***';
    const c = (e.className || '').toString();
    const rec = {
      role,
      label: labelOf(e),
      text: isInput ? '' : text(e),
      value,
      region: regionOf(e),
      disabled: !!(e.disabled || e.readOnly || c.includes('-disabled')),
      checked: role === 'checkbox' ? !!e.checked : null,
      selected: c.includes('-selected') || c.includes('z-tab-selected'),
      context: dialogText,
    };
    e.setAttribute('data-uiop-idx', String(out.length));
    out.push(rec);
  }
  return out;
}
"""

_SHORTCUT = re.compile(r"\s+(Shift\+)?(Alt|Ctrl)\+\S+$")
_DATE = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/\d{2,4}(?!\d)|(?<!\d)\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+)?(?!\d)")
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?(?!\d)")
_LONG_NUMBER = re.compile(r"(?<![\d.,])\d{5,}(?![\d.,])")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Element:
    index: int
    role: str
    label: str
    text: str
    value: str
    region: str
    disabled: bool = False
    checked: bool | None = None
    selected: bool = False
    # Untruncated label, text and enclosing dialog text, for safety checks only; never
    # rendered into prompts.
    full_text: str = field(default="", compare=False, repr=False)

    @property
    def name(self) -> str:
        """The most human-readable name: label, else visible text."""
        return self.label or self.text

    def render(self) -> str:
        parts = [f"[{self.index}] {self.role}", f'"{self.name}"' if self.name else '""']
        if self.label and self.text and self.text != self.label:
            parts.append(f'text="{self.text}"')
        if self.role in INPUT_ROLES:
            parts.append(f'value="{self.value}"')
        if self.checked is not None:
            parts.append("checked" if self.checked else "unchecked")
        if self.selected:
            parts.append("selected")
        if self.disabled:
            parts.append("disabled")
        parts.append(f"@{self.region}")
        return " ".join(parts)


@dataclass(frozen=True)
class Snapshot:
    elements: tuple[Element, ...]
    truncated: bool = False
    volatile: tuple[str, ...] = field(default=(), compare=False)

    def get(self, index: int) -> Element | None:
        if 0 <= index < len(self.elements):
            return self.elements[index]
        return None

    def render(self) -> str:
        lines = [e.render() for e in self.elements]
        if self.truncated:
            lines.append(f"(list capped at {len(self.elements)} elements)")
        return "\n".join(lines) if lines else "(no interactive elements visible)"


def clean_text(value: Any, volatile: tuple[str, ...] = ()) -> str:
    """Normalize one string: drop run identifiers, dates, times, long numbers, shortcuts."""
    s = _WS.sub(" ", str(value or "")).strip()
    for token in volatile:
        if token:
            s = s.replace(token, "<run>")
    s = _SHORTCUT.sub("", s)
    s = _DATE.sub("<date>", s)
    s = _TIME.sub("<time>", s)
    s = _LONG_NUMBER.sub("<n>", s)
    if len(s) > MAX_TEXT:
        s = s[: MAX_TEXT - 1] + "…"
    return s


def normalize(raw: list[dict[str, Any]], *, volatile: tuple[str, ...] = (), cap: int = MAX_ELEMENTS) -> Snapshot:
    """Build a Snapshot from COLLECT_JS output. Indexes stay aligned with data-uiop-idx."""
    elements: list[Element] = []
    for i, rec in enumerate(raw[:cap]):
        checked = rec.get("checked")
        elements.append(
            Element(
                index=i,
                role=clean_text(rec.get("role"), volatile) or "unknown",
                label=clean_text(rec.get("label"), volatile),
                text=clean_text(rec.get("text"), volatile),
                value=clean_text(rec.get("value"), volatile),
                region=clean_text(rec.get("region"), volatile) or "page",
                disabled=bool(rec.get("disabled")),
                checked=None if checked is None else bool(checked),
                selected=bool(rec.get("selected")),
                full_text=_WS.sub(" ", " ".join(str(rec.get(k) or "") for k in ("label", "text", "context"))).strip(),
            )
        )
    return Snapshot(elements=tuple(elements), truncated=len(raw) > cap, volatile=volatile)
