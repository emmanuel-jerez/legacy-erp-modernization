"""Data-access policy shared by both guardrail layers (the database role and the validator)."""

from __future__ import annotations

import re

# Column names that hold credentials or payment/identity secrets. The role is never
# granted them and the validator rejects any identifier that matches. Built from a scan
# of every adempiere column name; over-matching (``datepasswordchanged``, ``tokentype``)
# only hides a harmless column, under-matching leaks one, so err wide.
SENSITIVE_COLUMN = re.compile(
    r"password|passwd|pwd|userpw|secret|accesstoken|refreshtoken|privatekey|apikey"
    r"|encryptionkey|connectionkey|^salt$|_salt$|userpin|^pin$|creditcardnumber|creditcardvv"
    r"|creditcardexp|_ssn$|^iban$|accountno|routingno|authcode|websession"
)


def is_sensitive_column(name: str) -> bool:
    return bool(SENSITIVE_COLUMN.search(name.lower()))
