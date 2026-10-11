import json

import pandas as pd
import pytest

from dev import validate_fuel_cell_recovery as audit
from dev.stem_fuel_cells import EFFICIENCY, RECOVERED


@pytest.mark.parametrize("unexpected", [False, True])
def test_export_check_preserves_production_and_detects_background_changes(
    tmp_path, monkeypatch, unexpected
):
    before, after = tmp_path / "before.zip", tmp_path / "after.zip"
    for package in (before, after):
        package.with_suffix(".build.json").write_text(
            json.dumps({"scenario": "arbitrary", "years": [2050]})
        )
    before.with_suffix(".efficiencies.json").write_text("[]")
    after.with_suffix(".efficiencies.json").write_text(
        json.dumps(
            [
                {
                    "year": 2050,
                    "activity": "fuel cell",
                    "product": "electricity",
                    "location": "CH",
                    "variables": [EFFICIENCY + RECOVERED],
                    "old_physical_efficiency": 0.3,
                    "stem_target_efficiency": 0.5,
                    "efficiency_bounds": {"clipped": False},
                }
            ]
        )
    )
    ai = pd.DataFrame(
        {
            "index": [0, 1],
            "name": ["fuel cell", "background"],
            "reference product": ["electricity", "gas"],
            "unit": ["kilowatt hour", "cubic meter"],
            "location": ["CH", "CH"],
        }
    )
    bi = pd.DataFrame(
        {
            "index": [0],
            "name": ["CO2"],
            "compartment": ["air"],
            "subcompartment": [""],
            "unit": ["kilogram"],
        }
    )

    def matrices(package, year):
        changed = package == after
        return {
            "A_matrix_index": ai,
            "B_matrix_index": bi,
            "A_matrix": pd.DataFrame(
                {
                    "index of activity": [0, 0, 1],
                    "index of product": [0, 1, 1],
                    "flip": [0, 1, 0],
                    "value": [1, 1.2 if changed else 2, 1],
                }
            ),
            "B_matrix": pd.DataFrame(
                {
                    "index of activity": [0, 1],
                    "index of biosphere flow": [0, 0],
                    "value": [
                        1.8 if changed else 3,
                        9 if changed and unexpected else 4,
                    ],
                }
            ),
        }

    monkeypatch.setattr(audit, "read_matrices", matrices)
    if unexpected:
        with pytest.raises(AssertionError):
            audit.validate(before, after)
    else:
        result = audit.validate(before, after)
        assert result[0]["exchange_ratio_after_over_before"] == pytest.approx(0.6)
        assert [c["changed_exchanges"] for c in result[0]["checks"]] == [1, 1]
