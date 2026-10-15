"""Counting-boundary tests with the actual Edges CLIPS engine."""

import numpy as np
import pytest
from scipy.sparse import csr_matrix

from dev.refined_metal_edges import characterize


def definition(products=None):
    products = products or [{"product": "copper, cathode", "factor": 1.0}]
    return {
        "name": "Copper",
        "symbol": "Cu",
        "products": products,
        "internal_products": [p["product"] for p in products],
    }


def node(name, product):
    return {
        "name": name,
        "reference product": product,
        "unit": "kilogram",
        "location": "CH",
    }


@pytest.mark.parametrize("via_market", [False, True])
def test_direct_refinery_and_market_routes(via_market):
    nodes = {
        0: node("electrorefining", "copper, cathode"),
        1: node("market for copper, cathode", "copper, cathode"),
        2: node("cable production", "cable"),
    }
    a = np.eye(3)
    a[0, 1] = -1.05  # Distribution losses should not inflate delivered input.
    a[1 if via_market else 0, 2] = -2
    supply = np.linalg.solve(a, [0, 0, 1])
    c, score, audit = characterize(
        csr_matrix(a), nodes, supply, definition(), all_edges=True
    )
    assert score == pytest.approx(2)
    assert c[0, 1] == 0
    assert audit["unmatched_eligible_edges"] == 0


def test_market_chain_secondary_supply_and_downstream_product():
    nodes = {
        0: node("scrap recycling", "copper, cathode"),
        1: node("market group for copper", "copper, cathode"),
        2: node("regional copper distributor", "copper, cathode"),
        3: node("wire production", "wire"),
        4: node("motor production", "motor"),
    }
    a = np.eye(5)
    a[0, 1], a[1, 2], a[2, 3], a[3, 4] = -1, -1, -3, -2
    supply = np.linalg.solve(a, [0, 0, 0, 0, 1])
    _, score, audit = characterize(
        csr_matrix(a), nodes, supply, definition(), all_edges=True
    )
    assert score == pytest.approx(6)
    assert audit["internal_edges"] == 2


def test_selected_compound_conversion_counted_at_boundary_only():
    products = [
        {"product": "carbonate", "factor": 0.2},
        {"product": "hydroxide", "factor": 0.3},
    ]
    nodes = {
        0: node("carbonate refining", "carbonate"),
        1: node("conversion", "hydroxide"),
        2: node("battery", "battery"),
    }
    a = np.eye(3)
    a[0, 1], a[1, 2], a[0, 2] = -2, -4, -1
    _, score, _ = characterize(
        csr_matrix(a),
        nodes,
        np.linalg.solve(a, [0, 0, 1]),
        definition(products),
        all_edges=True,
    )
    assert score == pytest.approx(4 * 0.3 + 1 * 0.2)


def test_credits_preserve_supply_sign_but_outputs_are_not_inputs():
    nodes = {
        0: node("refinery", "copper, cathode"),
        1: node("factory", "widget"),
        2: node("substitution", "service"),
    }
    a = np.eye(3)
    a[0, 1], a[1, 2], a[0, 2] = -2, 1, 0.5
    _, score, _ = characterize(
        csr_matrix(a),
        nodes,
        np.linalg.solve(a, [0, 0, 1]),
        definition(),
        all_edges=True,
    )
    assert score == pytest.approx(-2)


def test_unit_mismatch_rejected():
    nodes = {
        0: {**node("refinery", "copper, cathode"), "unit": "tonne"},
        1: node("factory", "widget"),
    }
    with pytest.raises(ValueError, match="non-kilogram"):
        characterize(csr_matrix([[1, -1], [0, 1]]), nodes, np.ones(2), definition())


def test_negative_reference_production_is_flagged_and_not_counted():
    nodes = {
        0: node("recycling inventory", "copper, cathode"),
        1: node("factory", "widget"),
    }
    a = np.array([[-1.0, -2.0], [0.0, 1.0]])
    c, score, audit = characterize(
        csr_matrix(a), nodes, np.linalg.solve(a, [0, 1]), definition(), all_edges=True
    )
    assert c[0, 0] == 0
    assert score == pytest.approx(2)
    assert audit["nonpositive_reference_suppliers"][0]["name"] == "recycling inventory"


def test_inactive_metal_is_explicit_zero_with_verified_mapping():
    nodes = {0: node("refinery", "copper, cathode"), 1: node("factory", "widget")}
    _, score, audit = characterize(
        csr_matrix([[1.0, -2.0], [0.0, 1.0]]), nodes, np.zeros(2), definition()
    )
    assert score == 0
    assert audit["counted_edges"] == 1


def test_product_present_but_no_consumption_edges():
    nodes = {0: node("refinery", "copper, cathode"), 1: node("factory", "widget")}
    _, score, audit = characterize(
        csr_matrix(np.eye(2)), nodes, np.array([0.0, 1.0]), definition()
    )
    assert score == 0
    assert audit["counted_edges"] == 0
