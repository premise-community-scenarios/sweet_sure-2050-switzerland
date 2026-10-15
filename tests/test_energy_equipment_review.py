"""Check attribution with changed demand, coefficients and upstream inventory."""

from pathlib import Path
from types import SimpleNamespace
import sys
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
pytest.importorskip("scikits.umfpack")
from review_energy_equipment import attribution, heat_equipment_paths


def inventory(coefficient, upstream, extra):
    # root consumes a shared supplier and one version-specific supplier.
    matrix = csr_matrix([[1, 0, 0], [-coefficient, 1, 0], [-1, 0, 1]], dtype=float)
    nodes = pd.DataFrame(
        [
            ["root", "energy", "MJ", "CH"],
            ["shared", "material", "kg", "GLO"],
            [extra, "other", "kg", "GLO"],
        ],
        columns=["name", "reference product", "unit", "location"],
    )
    return SimpleNamespace(
        nodes=nodes,
        A=matrix,
        columns=matrix.tocsc(),
        methods=[1],
        Z=np.array([[coefficient * upstream + 3, upstream, 3]]),
        C=np.array([[0, upstream, 3]]),
    )


def test_attribution_reconciles_without_assigning_new_suppliers_to_old_intensities():
    old = inventory(2, 5, "removed")
    new = inventory(4, 7, "added")
    parts = attribution(old, new, 0, 0, 10, 12)
    assert sum(parts.values())[0] == pytest.approx(12 * 31 - 10 * 13)
    assert parts["demand"][0] == 26
    assert parts["foreground_coefficients"][0] == 120
    assert parts["upstream_intensities"][0] == 96
    assert parts["added_suppliers"][0] == 36
    assert parts["removed_suppliers"][0] == -36


def test_heat_screen_distinguishes_end_use_hardware_from_grid_infrastructure():
    # Two MJ heat per MJ endpoint; boiler allocation is 0.01 units/MJ heat.
    # Electricity also has a plant, which must stay outside this coverage screen.
    matrix = csr_matrix(
        [
            [1, 0, 0, 0, 0],
            [-2, 1, 0, 0, 0],
            [0, -0.01, 1, 0, 0],
            [0, -0.3, 0, 1, 0],
            [0, 0, 0, -0.1, 1],
        ],
        dtype=float,
    )
    nodes = pd.DataFrame(
        [
            ["final heat", "heat", "megajoule"],
            ["heat production", "heat", "megajoule"],
            ["gas boiler", "boiler", "unit"],
            ["electricity production", "electricity", "kilowatt hour"],
            ["power plant", "power plant", "unit"],
        ],
        columns=["name", "reference product", "unit"],
    )
    inv = SimpleNamespace(nodes=nodes, A=matrix, columns=matrix.tocsc())
    rows = heat_equipment_paths(inv, 0)
    assert len(rows) == 1
    assert rows[0]["equipment"] == "gas boiler"
    assert rows[0]["input_per_MJ"] == pytest.approx(0.02)
