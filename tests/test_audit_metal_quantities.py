"""Analytical networks exercise the independent audit, including signed loops."""

import importlib.util
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scikits.umfpack")
FILE = Path(__file__).resolve().parents[1] / "dev/audit_metal_quantities.py"
spec = importlib.util.spec_from_file_location("audit_metal_quantities", FILE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def network(tmp_path, matrix):
    nodes = pd.DataFrame(
        {
            "index": range(4),
            "name": ["refinery", "market", "equipment", "energy"],
            "reference product": ["metal", "metal", "equipment", "energy"],
            "unit": ["kilogram", "kilogram", "unit", "megajoule"],
            "location": ["CH"] * 4,
        }
    )
    i, j = np.nonzero(matrix)
    entries = pd.DataFrame(
        {
            "index of product": i,
            "index of activity": j,
            "value": abs(matrix[i, j]),
            "flip": (matrix[i, j] < 0).astype(int),
        }
    )
    p = tmp_path / "network.zip"
    with ZipFile(p, "w") as z:
        z.writestr("inventory/2050/A_matrix.csv", entries.to_csv(sep=";", index=False))
        z.writestr(
            "inventory/2050/A_matrix_index.csv", nodes.to_csv(sep=";", index=False)
        )
        z.writestr(
            "scenario_data/scenario_data.csv",
            "region,variables,year,unit,value\nCH,FE,2050,PJ/yr.,1\n",
        )
        z.writestr(
            "mapping/mapping.yaml",
            "FE:\n  dataset:\n  - name: energy\n    reference product: energy\n    unit: megajoule\n",
        )
    method = {
        "definition": {
            "products": [{"product": "metal", "factor": 0.5}],
            "internal_products": ["metal"],
        }
    }
    return audit.Inventory(p, 2050, [method])


def test_market_losses_are_internal_and_nonunit_production_is_normalized(tmp_path):
    # Two kg refinery output -> market; 3 kg market + 1 kg direct refinery
    # produce two equipment units. Each MJ uses four equipment units.
    a = np.eye(4)
    a[0, 1] = -1.1
    a[1, 2] = -3
    a[0, 2] = -1
    a[2, 2] = 2
    a[2, 3] = -4
    inv = network(tmp_path, a)
    assert inv.Z[0, 3] == pytest.approx(4.0)
    supply, _ = inv.solve(np.array([0.0, 0.0, 0.0, 10.0]))
    assert inv.C[0] @ supply == pytest.approx(40.0)
    # A market-only selector would miss the direct refinery contribution.
    assert set(zip(inv.edges[0].supplier, inv.edges[0].consumer)) == {(0, 2), (1, 2)}
    assert set(zip(inv.internal[0].supplier, inv.internal[0].consumer)) == {(0, 1)}
    rows, summary = inv.decompose(3, 10, 0, max_nodes=1)
    assert summary["unexpanded_residual_kg"] == pytest.approx(40.0)
    assert sum(
        r["direct_counted_kg"] + r["residual_kg"] for r in rows
    ) == pytest.approx(40.0)


def test_credit_and_cycle_are_retained_as_signed_residuals(tmp_path):
    a = np.eye(4)
    a[0, 1] = -1
    a[1, 2] = -2
    a[2, 3] = 0.5
    a[3, 2] = -0.1
    inv = network(tmp_path, a)
    expected = np.array([0.0, 0.0, 1.0, 0.0]) @ np.linalg.solve(
        a, np.array([0.0, 0.0, 0.0, 10.0])
    )
    assert inv.Z[0, 3] * 10 == pytest.approx(expected)
    rows, summary = inv.decompose(3, 10, 0, max_nodes=100)
    assert any(r["reason"] == "cycle" for r in rows)
    assert any(r["required_product_quantity"] < 0 for r in rows)
    assert summary["direct_accounted_kg"] + summary[
        "unexpanded_residual_kg"
    ] == pytest.approx(expected)


def test_demand_conversion_is_checked_against_original_package(tmp_path):
    inv = network(tmp_path, np.eye(4))
    row = dict(
        variable="FE",
        year=2050,
        scenario_unit="PJ/yr.",
        unit="megajoule",
        conversion_factor=1e9,
        scenario_demand=1.0,
        name="energy",
        reference_product="energy",
        location="CH",
    )
    assert inv.demand(row) == (3, 1e9)
    with pytest.raises(AssertionError):
        inv.demand({**row, "conversion_factor": 1e6})
    with pytest.raises(AssertionError):
        inv.demand({**row, "scenario_demand": 2})
