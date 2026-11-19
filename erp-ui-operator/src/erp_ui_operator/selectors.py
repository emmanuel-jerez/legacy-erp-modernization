"""Selector map: stable locators for the iDempiere ZK web UI.

ZK assigns component ids such as ``wFTB10`` that change on every page load, so no
locator here uses an ``id``. Targets are found by what a person sees and by attributes
iDempiere sets deliberately:

* form fields by their label cell (``td.z-cell`` with ``.form-label``), followed by the
  editor cell;
* login fields by the ``td.login-label`` text of their row;
* toolbar buttons by their ``title`` (iDempiere's tooltip text, e.g. "Save Changes");
* buttons, tabs and checkboxes by visible text or accessible role and name.

Postconditions (see ``plan.py``) read state through a different path, mostly the
``instancename`` attribute iDempiere derives from the application dictionary
(``C_Order0C_BPartner_ID``). A renamed label therefore breaks the action locator, which
is what drift mode simulates, without breaking the check that the action worked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from playwright.sync_api import Locator, Page


@dataclass(frozen=True)
class Target:
    """One selector-map entry. ``kind`` picks the locator strategy; ``name`` is the visible text."""

    kind: str
    name: str
    editor: str = "input"

    def describe(self) -> str:
        return f"{self.kind} {self.name!r}"


def _css_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _exact(text: str) -> re.Pattern[str]:
    return re.compile(r"^\s*" + re.escape(text) + r"\s*$")


def resolve(page: Page, target: Target) -> Locator:
    """Build the Playwright locator for a target. Does not wait; callers check count()."""
    name = _css_text(target.name)
    if target.kind == "login_field":
        row = page.locator(f'tr:has(> td.login-label span.z-label:text-is("{name}"))')
        return row.locator(target.editor).filter(visible=True)
    if target.kind == "form_field":
        cell = page.locator(f'td.z-cell:has(> div.form-label > span.z-label:text-is("{name}")) + td').filter(visible=True)
        return cell.locator(target.editor).filter(visible=True)
    if target.kind == "toolbar":
        return page.locator(f'.adwindow-toolbar a.toolbar-button[title^="{name}"]').filter(visible=True)
    if target.kind == "button":
        return page.get_by_role("button", name=target.name, exact=True).filter(visible=True)
    if target.kind == "checkbox":
        return page.locator(f'label.z-checkbox-content:text-is("{name}")').filter(visible=True)
    if target.kind == "tab":
        return page.locator(f'li.z-tab:has(span.z-tab-text:text-is("{name}"))').filter(visible=True)
    if target.kind == "global_search":
        return page.locator(".desktop-header input.z-bandbox-input").filter(visible=True)
    if target.kind == "menu_new":
        row = page.locator(".z-bandbox-popup tr.z-listitem").filter(visible=True).filter(
            has=page.locator("div.z-listcell-content", has_text=_exact(target.name))
        )
        return row.locator('a[title="New"]')
    if target.kind == "dialog_row":
        row = page.locator(".info-panel tr.z-listitem").filter(visible=True).filter(
            has=page.locator("td.z-listcell div.z-listcell-content", has_text=_exact(target.name))
        )
        return row.locator("td.z-listcell").nth(2)
    if target.kind == "dialog_button":
        return page.locator(f'.info-panel button[title="{name}"]').filter(visible=True)
    raise ValueError(f"unknown target kind {target.kind!r}")


# The deterministic layer's selector map. Keys are referenced by plan steps.
SELECTOR_MAP: dict[str, Target] = {
    "login.user": Target("login_field", "User"),
    "login.password": Target("login_field", "Password"),
    "login.select_role": Target("checkbox", "Select Role"),
    "login.ok": Target("button", "OK"),
    "role.role": Target("login_field", "Role", "input.z-combobox-input"),
    "role.org": Target("login_field", "Org", "input.z-combobox-input"),
    "role.warehouse": Target("login_field", "Warehouse", "input.z-combobox-input"),
    "role.ok": Target("button", "OK"),
    "menu.search": Target("global_search", "Search"),
    "menu.new_po": Target("menu_new", "Purchase Order"),
    "po.vendor": Target("form_field", "Business Partner"),
    "po.description": Target("form_field", "Description", "textarea"),
    "po.reference": Target("form_field", "Order Reference"),
    "po.date_promised": Target("form_field", "Date Promised", "input.z-datebox-input"),
    "po.save": Target("toolbar", "Save Changes"),
    "po.new_record": Target("toolbar", "New"),
    "po.lines_tab": Target("tab", "PO Line"),
    "line.product": Target("form_field", "Product"),
    "line.product_row": Target("dialog_row", "{sku}"),
    "line.product_ok": Target("dialog_button", "OK"),
    "line.qty": Target("form_field", "Quantity"),
}

# Drift mode: simulated UI changes applied to the selector map, not to the UI.
DRIFT_PROFILES: dict[str, dict[str, Target]] = {
    "drifted": {
        # A renamed field label ("Business Partner" relabelled "Vendor").
        "po.vendor": Target("form_field", "Vendor"),
        # A broken button locator (the toolbar tooltip changed).
        "po.save": Target("toolbar", "Save Record"),
        # A renamed quantity label on the order line.
        "line.qty": Target("form_field", "Order Quantity"),
    },
}


def selector_map(drift: str | None = None) -> dict[str, Target]:
    """The selector map, optionally perturbed by a named drift profile."""
    mapping = dict(SELECTOR_MAP)
    if drift:
        if drift not in DRIFT_PROFILES:
            raise ValueError(f"unknown drift profile {drift!r}; choose from {sorted(DRIFT_PROFILES)}")
        mapping.update(DRIFT_PROFILES[drift])
    return mapping


def bind(target: Target, **values: str) -> Target:
    """Fill ``{placeholders}`` in a target name (e.g. the SKU of a dialog row)."""
    return replace(target, name=target.name.format(**values)) if "{" in target.name else target


def healed_target(original: Target, label: str) -> Target | None:
    """A replacement target using the label the model's chosen element really has.

    Only same-kind replacements for label- or title-based targets are produced, so a
    healed entry can never point at a different kind of control than the map intended.
    """
    label = label.strip()
    if not label or label == original.name:
        return None
    if original.kind in ("form_field", "login_field", "toolbar", "button", "tab", "checkbox"):
        return replace(original, name=label)
    return None
