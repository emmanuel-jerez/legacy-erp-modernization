"""Retrieval over the committed catalog snapshot: no database or model needed."""

from __future__ import annotations

import pytest

from erp_schema_agent.retrieval.search import retrieve, search_schema


def names(result) -> list[str]:
    return [hit.table for hit in result.tables]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Which products are below their reorder point?", {"m_replenish", "m_storageonhand", "m_product"}),
        ("Revenue by state and month in 2026", {"c_order", "c_orderline", "c_region", "c_location", "c_bpartner_location"}),
        ("Who are our top 5 customers by revenue?", {"c_bpartner", "c_order"}),
        ("How many units are on open purchase orders per vendor?", {"c_order", "c_orderline", "c_bpartner"}),
        ("What is the promised lead time of each vendor for FH products?", {"m_product_po"}),
        ("How many units of each product sold in California?", {"c_region", "c_location", "c_bpartner_location"}),
    ],
)
def test_retrieves_the_tables_a_correct_query_needs(question, expected):
    result = retrieve(question)
    assert expected <= set(names(result)), names(result)


def test_join_paths_connect_the_retrieved_tables():
    result = retrieve("Revenue by state")
    joins = {(a, b) for a, _, b, _ in result.joins}
    assert ("c_bpartner_location", "c_location") in joins
    assert ("c_location", "c_region") in joins


def test_lexical_search_uses_the_data_dictionary():
    # "Business Partner" is iDempiere's dictionary name for C_BPartner; nothing in the
    # table name itself says so.
    assert search_schema("business partner")[0].table == "c_bpartner"
    assert "m_storageonhand" in [h.table for h in search_schema("storage on hand quantity")[:3]]


def test_credential_columns_never_reach_the_context():
    result = retrieve("user passwords, salts and login pins for ad_user")
    assert "ad_user" in names(result)
    for column in ("password", "salt", "userpin", "emailuserpw"):
        assert f"- {column} (" not in result.context
        assert f" {column}," not in result.context
