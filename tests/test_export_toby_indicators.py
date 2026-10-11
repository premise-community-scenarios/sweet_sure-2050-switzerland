"""Preserve precision, zeros and existing Toby series identities on export."""

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from export_toby_indicators import UNITS, dense_results, parameter_path, write_prem1


@pytest.mark.parametrize(
    "variable,expected",
    [
        ("industry_process_FE_other_electric", "industry / process / other_electric"),
        ("residential_FE_other_electric", "residential / other_electric"),
        (
            "residential_space_heating_heat_pump",
            "residential / space_heating / heat_pump",
        ),
        ("services_heat_pump", "services / heat_pump"),
        ("other_transport_diesel", "transport / other_diesel"),
        ("heavy_duty_truck_gasoline", "transport / heavy_duty_truck / gasoline"),
        ("passenger_rail_electricity", "transport / passenger_rail / electricity"),
    ],
)
def test_registered_hierarchy_and_new_transport_modes(variable, expected):
    assert (
        parameter_path("EXT - 0 - FE_" + variable, "climate change", "CH")
        == "premise / climate change / " + expected + " / CH"
    )


def sample_results():
    return pd.DataFrame(
        [
            dict(
                variable="EXT - 0 - FE_services_other_electric",
                year=2020,
                impact_category=impact,
                location="CH",
                value=-np.pi * (i + 1),
                region="CH",
                model="remind",
                scenario="SSP2-PkBudg1000 - SPS1_bas0",
                unit=unit,
            )
            for i, (impact, unit) in enumerate(UNITS.items())
        ]
    )


def test_export_keeps_negative_values_and_adds_explicit_zero_series(tmp_path):
    frame = sample_results()
    dense = dense_results(
        frame,
        [frame.variable.iloc[0], "EXT - 0 - FE_bus_diesel"],
        [2020],
        frame.scenario.iloc[0],
    )
    assert len(dense) == 2 * 6 * 3
    assert dense.value.eq(0).sum() == 30
    assert dense.value.lt(0).sum() == 6
    target = tmp_path / "prem1.csv"
    write_prem1(dense, target)
    restored = pd.read_csv(target, float_precision="round_trip", encoding="latin-1")
    np.testing.assert_array_equal(restored.value, dense.value)
    assert list(restored.columns)[6] == "impact"
    assert set(restored.loc[restored.impact.eq("net use of fresh water"), "unit"]) == {
        "m3"
    }


def test_duplicate_or_unknown_coordinates_cannot_be_silently_summed():
    frame = sample_results()
    with pytest.raises(ValueError, match="Duplicate"):
        dense_results(
            pd.concat([frame, frame]),
            [frame.variable.iloc[0]],
            [2020],
            frame.scenario.iloc[0],
        )
    frame.loc[0, "location"] = "unknown"
    with pytest.raises(ValueError, match="Unexpected result"):
        dense_results(frame, [frame.variable.iloc[0]], [2020], frame.scenario.iloc[0])


def test_delimiters_fail_before_writing_an_import_file(tmp_path):
    frame = sample_results()
    dense = dense_results(
        frame, [frame.variable.iloc[0]], [2020], frame.scenario.iloc[0]
    )
    dense.loc[0, "scenario"] = "bad,scenario"
    with pytest.raises(ValueError, match="delimiters"):
        write_prem1(dense, tmp_path / "bad.csv")
