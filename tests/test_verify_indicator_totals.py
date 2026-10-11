"""Check independent solving against a small, analytically solvable inventory."""

import json
from pathlib import Path
import sys
import zipfile

import pandas as pd
import pytest
import numpy as np
from scipy.sparse import csr_matrix

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from generate_indicators import sha256
from verify_indicator_totals import verify, solve_residuals


def make_case(tmp_path, incorrect=False):
    # Demand is 3 for a and 2 for b. A = [[1, 0], [-2, 1]];
    # supply = [3, 8]; biosphere = [4, 5]; CF = 7; score = 364.
    package = tmp_path / "example.zip"
    contents = {
        "A_matrix": "index of activity;index of product;value;flip\n0;0;1;0\n0;1;1.25;1\n1;1;1;0\n0;1;0.75;1\n",
        "B_matrix": "index of activity;index of biosphere flow;value\n0;0;4\n1;0;5\n",
        "A_matrix_index": "name;reference product;unit;location;index\na;a;megajoule;CH;0\nb;b;megajoule;CH;1\n",
        "B_matrix_index": "name;compartment;subcompartment;index\nflow;air;unspecified;0\n",
    }
    with zipfile.ZipFile(package, "w") as archive:
        for name, content in contents.items():
            archive.writestr(f"inventory/2020/{name}.csv", content)
    data = tmp_path / "data"
    data.mkdir()
    cf = data / "lcia_ei312.json"
    cf.write_text(
        json.dumps(
            [
                {
                    "name": ["test", "indicator"],
                    "exchanges": [{"name": "flow", "categories": ["air"], "amount": 7}],
                }
            ]
        )
    )
    raw = tmp_path / "results.gzip"
    pd.DataFrame(
        [
            {
                "year": 2020,
                "impact_category": "test - indicator",
                "value": 365 if incorrect else 364,
            }
        ]
    ).to_parquet(raw)
    pd.DataFrame(
        [
            dict(
                year=2020,
                name="a",
                reference_product="a",
                unit="megajoule",
                location="CH",
                scenario_demand=1.5,
                conversion_factor=2,
            ),
            dict(
                year=2020,
                name="b",
                reference_product="b",
                unit="megajoule",
                location="CH",
                scenario_demand=2,
                conversion_factor=1,
            ),
        ]
    ).to_csv(tmp_path / "results_demands.csv", index=False)
    manifest = tmp_path / "results.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "coverage": {"working_package": str(package)},
                "calculation": {
                    "working_package_sha256": sha256(package),
                    "ecoinvent_version": "3.12",
                    "cf_sha256": sha256(cf),
                    "methods": ["test - indicator"],
                },
                "pathways_source": str(tmp_path),
                "raw_sha256": sha256(raw),
            }
        )
    )
    return manifest


@pytest.mark.parametrize("solver", ["superlu", "umfpack"])
def test_both_solvers_verify_analytical_score_and_record_backend(tmp_path, solver):
    verify(make_case(tmp_path), [2020], solver=solver)
    report = json.loads((tmp_path / "results_independent_validation.json").read_text())
    assert report["passed"]
    assert report["solver"] == solver
    assert report["checks"][0]["independent_score"] == 364


def test_wrong_indicator_totals_fail_verification(tmp_path):
    with pytest.raises(AssertionError):
        verify(make_case(tmp_path, incorrect=True), [2020])
    assert not (tmp_path / "results_independent_validation.json").exists()


def test_separate_demands_use_all_suppliers_before_summing(tmp_path):
    verify(make_case(tmp_path), [2020], solver="superlu", demand_mode="separate")
    report = json.loads((tmp_path / "results_independent_validation.json").read_text())
    assert report["demand_mode"] == "separate"
    assert report["checks"][0]["independent_score"] == 364


def test_residual_accounts_for_internal_flows_and_detects_inaccurate_equations():
    matrix = csr_matrix([[1e12, -1e12 + 1], [0, 1]])
    demand = np.array([1.0, 1.0])
    relative, backward = solve_residuals(matrix, np.array([1.0 + 2e-16, 1.0]), demand)
    assert relative > 1e-5
    assert backward < 1e-15
    _, bad = solve_residuals(matrix, np.array([1.0, 2.0]), demand)
    assert bad > 0.1
