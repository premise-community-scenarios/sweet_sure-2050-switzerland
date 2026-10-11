"""Guard against confusing missing Toby records, stored zeros and score changes."""

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from compare_toby_indicators import aggregate, compare_cells, percent_change, read_old


def example():
    mapping = pd.DataFrame(
        {
            "variable": ["observed", "zero", "blank", "new"],
            "impact": ["climate change"] * 4,
            "location": ["CH"] * 4,
            "unit": ["kg CO2-eq."] * 4,
            "path": ["p1", "p2", "p3", "p4"],
            "prm_id": [1, 2, 3, np.nan],
            "registered": [True, True, True, False],
        }
    )
    latest = mapping[["variable", "impact", "location", "unit"]].copy()
    latest["scenario"] = "SSP2 - SPS1_bas0"
    latest["sector"] = "industry"
    latest["year"] = 2050
    latest["value"] = [90.0, -5.0, 12.0, 20.0]
    old = latest.iloc[:3].drop(columns=["scenario", "sector", "value"])
    old["old_value"] = [100.0, 0.0, np.nan]
    return latest, old, mapping


def test_observed_zero_blank_and_unregistered_have_distinct_meaning():
    latest, old, mapping = example()
    cells = compare_cells(latest, old, mapping, "SPS1_bas0")
    assert list(cells.old_status) == [
        "stored",
        "stored",
        "blank_registered",
        "unregistered",
    ]
    result = aggregate(cells).iloc[0]
    assert result.old_stored_total == 100
    assert result.latest_total == 117
    assert result.latest_on_matches == 85
    assert result.matched_change == -15
    assert result.latest_on_old_blank == 12
    assert result.latest_unregistered == 20
    assert result.total_change == 17
    assert result.old_stored_cells == 2
    by_variable = aggregate(cells, ["variable"]).set_index("variable")
    assert np.isnan(by_variable.loc["blank", "old_stored_total"])
    assert np.isnan(by_variable.loc["new", "total_change_percent"])
    assert by_variable.loc["zero", "old_stored_total"] == 0
    assert np.isnan(by_variable.loc["zero", "matched_change_percent"])


def test_percentages_keep_direction_for_negative_scores_and_undefined_zeros():
    result = percent_change(
        pd.Series([10.0, -10.0, 2.0]), pd.Series([-20.0, 20.0, 0.0])
    )
    assert result.iloc[0] == 50
    assert result.iloc[1] == -50
    assert np.isnan(result.iloc[2])


def test_changed_unit_does_not_silently_drop_a_latest_value():
    latest, old, mapping = example()
    latest.loc[0, "unit"] = "wrong"
    with pytest.raises(ValueError, match="no mapping"):
        compare_cells(latest, old, mapping, "SPS1_bas0")


def test_duplicate_latest_scores_cannot_double_count():
    latest, old, mapping = example()
    with pytest.raises(ValueError, match="Duplicate"):
        compare_cells(pd.concat([latest, latest.iloc[:1]]), old, mapping, "SPS1_bas0")


def test_report_blanks_must_agree_with_raw_record_absence(tmp_path):
    _, _, mapping = example()
    report = pd.DataFrame(
        {
            "prm_id": [1, 2, 3],
            "scn_id": [7, 7, 7],
            "path": ["p1", "p2", "p3"],
            "unit": ["kg CO2-eq."] * 3,
            "2050": [100.0, 0.0, np.nan],
        }
    )
    report_path, raw_path = tmp_path / "report.csv", tmp_path / "raw.csv"
    report.to_csv(report_path, index=False)
    raw_path.write_text("1,7,2050,100\n2,7,2050,0\n")
    old, years, count = read_old(report_path, raw_path, mapping, 7)
    assert years == [2050] and count == 2
    assert old.old_value.isna().sum() == 1
    raw_path.write_text("1,7,2050,100\n2,7,2050,0\n3,7,2050,0\n")
    with pytest.raises(ValueError, match="disagree"):
        read_old(report_path, raw_path, mapping, 7)


def test_old_unit_or_id_mismatch_fails(tmp_path):
    _, _, mapping = example()
    report = pd.DataFrame(
        {
            "prm_id": [1, 2, 3],
            "scn_id": [7, 7, 7],
            "path": ["p1", "p2", "p3"],
            "unit": ["wrong"] * 3,
            "2050": [100.0, 0.0, np.nan],
        }
    )
    report_path = tmp_path / "report.csv"
    report.to_csv(report_path, index=False)
    with pytest.raises(ValueError, match="unit differs"):
        read_old(report_path, tmp_path / "raw.csv", mapping, 7)
