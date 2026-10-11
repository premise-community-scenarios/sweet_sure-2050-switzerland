"""Reference cases for the read-only STEM CHP consistency audit."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from audit_stem_chp_balances import balance_metrics, formula_text, raw_series


def test_allocated_fuel_is_counted_once_and_efficiency_is_a_fraction():
    result = balance_metrics(0.2, 0.6, 0.25, 0.75, 0.2)
    assert result["total_fuel_PJ"] == 1
    assert result["combined_output_over_input"] == pytest.approx(0.8)
    assert result["energy_allocation_matches"]
    assert result["reported_efficiency_matches"]
    assert not result["output_exceeds_input_10pct"]


def test_large_output_and_small_output_are_flagged_without_replacing_values():
    high = balance_metrics(0.002, 0.3, 0.00005, 0.00745, 0.002 / 0.0075)
    low = balance_metrics(0.0001, 0.00026, 0.25, 0.65, 0.0001 / 0.9)
    assert high["output_exceeds_input_10pct"]
    assert low["combined_efficiency_below_1pct"]
    assert low["derived_electric_efficiency"] == pytest.approx(0.0001 / 0.9)


def test_zero_fuel_does_not_hide_positive_output():
    result = balance_metrics(1, 0, 0, 0, "-")
    assert result["output_without_fuel"]
    assert result["combined_output_over_input"] is None


def test_shared_formula_is_translated_to_the_requested_year():
    cells = {"K550": ("n", 0.1, (None, (("t", "shared"), ("si", "7"))))}
    masters = {"7": ("F550", 'IFERROR(F152/SUM(F380,F437),"-")')}
    assert formula_text(cells, "K550", masters) == '=IFERROR(K152/SUM(K380,K437),"-")'


def test_raw_row_identity_uses_technology_and_flow_labels_not_row_position():
    cache = {
        "sheets": {
            "OUT_Electricity_CHP": {
                "J3": "2040",
                "K3": "2050",
                "B425": "CHPWOD",
                "C425": "VAR_fout",
                "D425": "LTH",
                "J425": 0.4,
                "B426": "CHPWOD",
                "C426": "VAR_fin",
                "D426": "EWOD",
                "J426": 0.7,
            }
        }
    }
    result = raw_series(cache, "CHPWOD", "VAR_fout", "LTH")
    assert result[2040] == (0.4, "J425", False)
    assert result[2050] == (0, "K425", True)
    cache["sheets"]["OUT_Electricity_CHP"].update(
        {
            "B500": "CHPWOD",
            "C500": "VAR_fout",
            "D500": "LTH",
            "J500": 0.9,
        }
    )
    with pytest.raises(ValueError, match="Expected one"):
        raw_series(cache, "CHPWOD", "VAR_fout", "LTH")


def test_absent_coproduct_flow_remains_distinct_from_an_observed_zero():
    cache = {"sheets": {"OUT_Electricity_CHP": {"J3": "2040"}}}
    with pytest.raises(ValueError, match="Expected one"):
        raw_series(cache, "CHPWOD", "VAR_fin", "EWOD")
    assert raw_series(cache, "SUP_WODBIO", "VAR_fout", "ELC35", allow_absent_flow=True)[
        2040
    ] == (0, None, True)
