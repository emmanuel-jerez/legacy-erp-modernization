"""`_insert` refuses batches whose rows disagree on columns (no database needed)."""

import pytest

from erp_sandbox.seed import _insert


def test_mismatched_keys_are_rejected():
    with pytest.raises(ValueError, match="different keys"):
        _insert(None, "m_product", [{"a": 1, "b": 2}, {"a": 3}])  # type: ignore[arg-type]
