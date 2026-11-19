"""Seam: the static allowlist that runs before any generated code executes."""

from __future__ import annotations

import pytest

from erp_migration_agent.guard import check_source

HEADER = "def f(x, *, data):\n"


@pytest.mark.parametrize(
    "body",
    [
        "    import subprocess\n",
        "    from os import path\n",
        "    return eval('1')\n",
        "    return open('/etc/passwd').read()\n",
        "    return ().__class__.__mro__\n",
        "    return __builtins__\n",
        "    return getattr(x, 'y')\n",
        "    return (lambda: 0).gi_frame\n",
        "    from . import sibling\n",
        "    import calendar\n    return calendar.sys.modules['os'].getcwd()\n",
        "    import typing\n    return typing._eval_type\n",
        "    from typing import get_type_hints\n",
        "    import enum\n    return enum.bltns.open('/etc/hostname').read()\n",
        "    import typing\n    return typing.operator.attrgetter('x')\n",
        "    return type('Decimal', (), {'__module__': 'decimal'})()\n",
        "    import datetime\n    return datetime.datetime.now()\n",
    ],
)
def test_escape_hatches_are_rejected(body):
    verdict = check_source(HEADER + body, "f")
    assert not verdict.ok


def test_allowlisted_port_passes_and_entry_point_is_required():
    ok = (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\nfrom decimal import Decimal, ROUND_HALF_UP\nimport calendar\n\n"
        "@dataclass\nclass Term:\n    days: int\n    def __post_init__(self):\n        pass\n\n"
        "def f(x, *, data):\n    return Decimal(x).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)\n"
    )
    assert check_source(ok, "f").ok
    verdict = check_source(ok, "g")
    assert not verdict.ok and "top-level function 'g'" in verdict.violations[0]
