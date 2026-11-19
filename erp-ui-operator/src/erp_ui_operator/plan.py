"""The deterministic step plan for "create a draft purchase order".

Each step names a selector-map key, the action and value, a natural-language goal (used
only if the LLM fallback runs), the regions and values the step may touch (its guardrail
scope) and a postcondition: a check of UI state that proves the step worked.

Postconditions deliberately read state through locators that differ from the action's:
mostly iDempiere's ``instancename`` attributes, derived from the application dictionary
(table and column names), plus login-page attributes. Drift mode perturbs the selector map
only, so a renamed label breaks the action but not the proof.

Building the plan is pure: no browser, no database.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .erpdb import RunTag
from .guardrails import ALLOWED_ACTIONS, StepScope
from .task import TaskSpec

LOGIN_USER = "GardenAdmin"
LOGIN_PASSWORD = "GardenAdmin"  # iDempiere demo default, local sandbox only
LOGIN_ROLE = "GardenWorld Admin"
LOGIN_ORG = "HQ"
LOGIN_WAREHOUSE = "HQ Warehouse"
PASSWORD_TOKEN = "<password>"  # how logs and prompts show the secret
RUN_TOKEN = "<run>"  # stands in for the run id in prompts (matches snapshot normalization)

HEADER = 'C_Order0'
LINE = 'C_OrderLine0'


def _field(instance: str, editor: str = "input") -> str:
    """CSS for the visible editor of an application-dictionary column."""
    if editor == "self":
        return f'[instancename="{instance}"]'
    return f'[instancename="{instance}"] {editor}'


@dataclass(frozen=True)
class Post:
    """A postcondition. ``kind`` is interpreted by ``checks.evaluate``."""

    kind: str
    args: tuple[tuple[str, Any], ...] = ()
    parts: tuple["Post", ...] = ()
    text: str = ""

    def arg(self, name: str, default: Any = None) -> Any:
        return dict(self.args).get(name, default)

    def describe(self) -> str:
        return self.text


def visible(css: str, text: str) -> Post:
    return Post("visible", (("css", css),), text=text)


def visible_text(css: str, exact: str, text: str) -> Post:
    """A visible element matching ``css`` whose whole text is ``exact`` (whitespace ignored)."""
    return Post("visible_text", (("css", css), ("exact", exact)), text=text)


def hidden(css: str, text: str) -> Post:
    return Post("hidden", (("css", css),), text=text)


def value(css: str, expected: str, text: str, mode: str = "equals") -> Post:
    return Post("value", (("css", css), ("expected", expected), ("mode", mode)), text=text)


def saved(lines: int, text: str) -> Post:
    return Post("saved", (("lines", lines),), text=text)


def all_of(*parts: Post, text: str) -> Post:
    return Post("all", parts=parts, text=text)


def any_of(*parts: Post, text: str) -> Post:
    return Post("any", parts=parts, text=text)


@dataclass(frozen=True)
class Step:
    key: str
    goal: str
    action: str  # goto | click | fill | type | select | check (no UI action; verify only)
    target: str | None
    post: Post
    scope: StepScope
    value: str | None = None
    display_value: str | None = None  # what prompts and logs show (secrets masked)
    when: Post | None = None  # run only if this holds (e.g. a dialog is open)
    skip_if_done: bool = False  # skip when the postcondition already holds
    fallback: bool = True
    bindings: tuple[tuple[str, str], ...] = field(default=())
    repair: tuple[str, ...] = ()  # for "check" steps: steps to re-run once on a mismatch
    wait_ms: int = 6000  # how long the postcondition may take to hold

    @property
    def shown_value(self) -> str | None:
        return self.display_value if self.display_value is not None else self.value

    def __post_init__(self) -> None:
        # Least privilege: a fallback may only use actions that fit the step's own kind.
        if self.scope.actions == ALLOWED_ACTIONS:
            object.__setattr__(self, "scope", replace(self.scope, actions=STEP_ACTIONS.get(self.action, frozenset())))


STEP_ACTIONS: dict[str, frozenset[str]] = {
    "click": frozenset({"click", "press", "wait"}),
    "fill": frozenset({"fill", "click", "press", "wait"}),
    "type": frozenset({"fill", "click", "press", "wait"}),
    "select": frozenset({"select", "click", "press", "wait"}),
}

LOGIN = frozenset({"login"})
HEADER_FORM = frozenset({"form"})
TOOLBAR = frozenset({"window-toolbar"})
DIALOG = frozenset({"dialog: "})
INFO_DIALOG = ".info-panel"


def build_plan(spec: TaskSpec, tag: RunTag, url: str) -> list[Step]:
    desc = tag.description(spec)
    ref = tag.po_reference
    # Prompts and cassette keys must not contain the random run id; the model sees and
    # returns these display forms, and the runner maps them back to the real values.
    desc_shown = desc.replace(tag.run_id, RUN_TOKEN)
    ref_shown = ref.replace(tag.run_id, RUN_TOKEN)
    date = spec.ui_promised_date
    login_row = lambda label: f'tr:has(> td.login-label span.z-label:text-is("{label}"))'  # noqa: E731

    steps: list[Step] = [
        Step("login.open", f"Open the iDempiere web UI at {url}.", "goto", None,
             visible('input[autocomplete="username"]', "the login form's User field is visible"),
             StepScope(LOGIN), value=url, fallback=False),
        Step("login.user", f"Type the user name {LOGIN_USER!r} into the User field of the login form.", "fill",
             "login.user", value('input[autocomplete="username"]', LOGIN_USER, f"the User field holds {LOGIN_USER!r}"),
             StepScope(LOGIN, frozenset({LOGIN_USER})), value=LOGIN_USER),
        Step("login.password", "Type the password into the Password field of the login form.", "fill",
             "login.password",
             # iDempiere masks the value with asterisks on blur, so only "non-empty" is observable.
             value('input[type="password"]', "", "the Password field is filled in", mode="nonempty"),
             # No fallback: the secret never goes to the model, and the guardrail refuses to
             # fill password fields anyway.
             StepScope(LOGIN), value=LOGIN_PASSWORD, display_value=PASSWORD_TOKEN, fallback=False),
        Step("login.select_role", "Tick the 'Select Role' checkbox so the role screen appears after login.", "click",
             "login.select_role",
             visible('span.z-checkbox.z-checkbox-on:has(label:text-is("Select Role"))', "Select Role is ticked"),
             StepScope(LOGIN), skip_if_done=True),
        Step("login.ok", "Submit the login form with its OK button.", "click", "login.ok",
             visible(f"{login_row('Role')} input", "the role selection screen is shown"), StepScope(LOGIN)),
        Step("role.role", f"Choose the role {LOGIN_ROLE!r}.", "select", "role.role",
             value(f"{login_row('Role')} input", LOGIN_ROLE, f"Role is {LOGIN_ROLE!r}"),
             StepScope(LOGIN | {"dropdown"}, frozenset({LOGIN_ROLE})), value=LOGIN_ROLE, skip_if_done=True),
        Step("role.org", f"Choose the organization {LOGIN_ORG!r}.", "select", "role.org",
             value(f"{login_row('Org')} input", LOGIN_ORG, f"Org is {LOGIN_ORG!r}"),
             StepScope(LOGIN | {"dropdown"}, frozenset({LOGIN_ORG})), value=LOGIN_ORG, skip_if_done=True),
        Step("role.warehouse", f"Choose the warehouse {LOGIN_WAREHOUSE!r}.", "select", "role.warehouse",
             value(f"{login_row('Warehouse')} input", LOGIN_WAREHOUSE, f"Warehouse is {LOGIN_WAREHOUSE!r}"),
             StepScope(LOGIN | {"dropdown"}, frozenset({LOGIN_WAREHOUSE})), value=LOGIN_WAREHOUSE, skip_if_done=True),
        Step("role.ok", "Confirm the role selection with OK to reach the main desktop.", "click", "role.ok",
             visible(".desktop-header input.z-bandbox-input", "the desktop's global search box is visible"),
             StepScope(LOGIN)),
        Step("menu.search", "Type 'Purchase Order' into the global menu search box.", "type", "menu.search",
             visible_text(".z-bandbox-popup tr.z-listitem", "Purchase Order",
                          "the 'Purchase Order' window is listed in the menu search results"),
             StepScope(frozenset({"global-search"}), frozenset({"Purchase Order"})), value="Purchase Order"),
        Step("menu.new_po", "Open a new record in the 'Purchase Order' window from the search results.", "click",
             "menu.new_po",
             all_of(
                 visible(_field(HEADER + "DocumentNo", "self"), "the Purchase Order header form is open"),
                 value(_field(HEADER + "C_DocTypeTarget_ID"), "Purchase Order", "target document type is Purchase Order"),
                 text="a new Purchase Order header is open",
             ),
             StepScope(frozenset({"search-results"}))),
        Step("po.vendor", f"Fill the vendor (business partner) field of the purchase order with {spec.vendor!r}.", "fill",
             "po.vendor",
             # The partner callout fills location, invoice partner and price list, and resets
             # POReference. Wait for all of it before typing anything else into the header.
             all_of(
                 value(_field(HEADER + "C_BPartner_ID"), spec.vendor, f"the business partner is {spec.vendor!r}"),
                 value(_field(HEADER + "C_BPartner_Location_ID"), "", "a partner location was filled in", mode="nonempty"),
                 value(_field(HEADER + "Bill_BPartner_ID"), spec.vendor, "the invoice partner was filled in"),
                 value(_field(HEADER + "M_PriceList_ID"), "", "a price list was filled in", mode="nonempty"),
                 text=f"the business partner is {spec.vendor!r} and the server filled in its location, invoice partner and price list",
             ),
             StepScope(HEADER_FORM, frozenset({spec.vendor})), value=spec.vendor, wait_ms=15000),
        Step("po.description", "Fill the order's Description with the run tag.", "fill", "po.description",
             value(_field(HEADER + "Description", "self"), desc, "the description holds the run tag"),
             StepScope(HEADER_FORM, frozenset({desc_shown})), value=desc, display_value=desc_shown),
        Step("po.reference", "Fill the Order Reference with the run reference.", "fill", "po.reference",
             value(_field(HEADER + "POReference", "self"), ref, "the order reference holds the run reference"),
             StepScope(HEADER_FORM, frozenset({ref_shown})), value=ref, display_value=ref_shown),
        Step("po.date_promised", f"Set Date Promised to {date}.", "fill", "po.date_promised",
             value(_field(HEADER + "DatePromised"), date, f"Date Promised is {date}"),
             StepScope(HEADER_FORM, frozenset({date})), value=date),
        Step("po.save", "Save the purchase order header with the toolbar's save button.", "click", "po.save",
             # After a save the form shows what the server stored, so re-check the entered values.
             all_of(
                 saved(0, "record saved, a document number assigned, no lines yet"),
                 value(_field(HEADER + "C_BPartner_ID"), spec.vendor, "the saved vendor is unchanged"),
                 value(_field(HEADER + "Description", "self"), desc, "the saved description is unchanged"),
                 value(_field(HEADER + "POReference", "self"), ref, "the saved order reference is unchanged"),
                 value(_field(HEADER + "DatePromised"), date, "the saved promised date is unchanged"),
                 text="the header is saved with a document number, no lines yet, and the entered values",
             ),
             StepScope(TOOLBAR)),
        # The UI can show a value the server later overwrote (a slow partner callout resets
        # POReference), so read the saved header back from the database. On a mismatch the
        # runner re-runs the listed steps once, then checks again.
        Step("po.readback", "Check the saved header in the database.", "check", None,
             Post("db_header", text="the saved header row holds the vendor, run tag, reference and promised date"),
             StepScope(frozenset()), fallback=False,
             repair=("po.description", "po.reference", "po.date_promised", "po.save")),
        Step("po.lines_tab", "Open the 'PO Line' tab to add order lines.", "click", "po.lines_tab",
             visible(_field(LINE + "M_Product_ID"), "the order line form is open"),
             StepScope(frozenset({"detail-pane", "form"}))),
    ]

    for n, line in enumerate(spec.lines, start=1):
        p = f"line{n}"
        # iDempiere skips a callout while another is still running, so wait for proof that the
        # product callout finished (it sets the price) before touching the next field.
        product_ok = all_of(
            value(_field(LINE + "M_Product_ID"), f"{line.sku}_", f"the line's product is {line.sku}", mode="startswith"),
            value(_field(LINE + "PriceActual"), "0", "the server has priced the product", mode="positive"),
            text=f"the line's product is {line.sku} and the server has priced it",
        )
        dialog_open = visible(INFO_DIALOG, "the Product Info dialog is open")
        qty_entered = value(_field(LINE + "QtyEntered"), str(line.qty), f"the quantity is {line.qty}", mode="number")
        steps += [
            Step(f"{p}.new", "Start a new order line with the toolbar's New button.", "click", "po.new_record",
                 value(_field(LINE + "M_Product_ID"), "", "an empty new order line is open", mode="empty"),
                 StepScope(TOOLBAR), skip_if_done=True),
            Step(f"{p}.product", f"Fill the line's Product field with the SKU {line.sku!r}.", "fill", "line.product",
                 any_of(product_ok, dialog_open, text=f"the product is {line.sku} or the Product Info dialog lists matches"),
                 StepScope(HEADER_FORM, frozenset({line.sku})), value=line.sku, wait_ms=15000),
            Step(f"{p}.product_pick", f"In the Product Info dialog, select the row whose Search Key is exactly {line.sku!r}.",
                 "click", "line.product_row",
                 visible(f"{INFO_DIALOG} button[title='OK']:enabled", "a product row is selected in the dialog"),
                 StepScope(DIALOG), when=dialog_open, bindings=(("sku", line.sku),)),
            Step(f"{p}.product_ok", "Confirm the selected product with the dialog's OK button.", "click", "line.product_ok",
                 all_of(product_ok, hidden(INFO_DIALOG, "the dialog is closed"), text=f"the product is {line.sku}"),
                 StepScope(DIALOG), when=dialog_open, wait_ms=15000),
            Step(f"{p}.qty", f"Fill the line's Quantity with {line.qty}.", "fill", "line.qty",
                 # PO Quantity (QtyOrdered) is set by a server callout, so it proves the server has the value.
                 all_of(qty_entered, value(_field(LINE + "QtyOrdered"), str(line.qty),
                                           "the server-computed PO Quantity matches", mode="number"),
                        text=f"the quantity is {line.qty} and the server has applied it"),
                 StepScope(HEADER_FORM, frozenset({str(line.qty)})), value=str(line.qty), wait_ms=15000),
            Step(f"{p}.save", "Save the order line with the toolbar's save button.", "click", "po.save",
                 all_of(saved(n, f"record saved and the order has {n} line(s)"), product_ok, qty_entered,
                        text=f"the line is saved with {line.sku} x {line.qty} and the order has {n} line(s)"),
                 StepScope(TOOLBAR)),
        ]
    return steps


def plan_values(steps: list[Step]) -> set[str]:
    """Every value the plan may type or select (for tests and audits)."""
    out: set[str] = set()
    for s in steps:
        out |= set(s.scope.values)
    return out
