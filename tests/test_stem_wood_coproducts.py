from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from dev import stem_wood_coproducts as wood


def test_allocated_joint_outputs_reproduce_original_process_demand():
    # Arbitrary annual output mix, including a kWh coproduct. The combined
    # demand for the physical process must be the original fuel output/LHV.
    fuel, electricity, heat = 25.0, 2.0, 3.0
    alpha = wood.allocation(fuel, electricity, heat)
    source = {
        "name": "test source",
        "reference product": "biomethane, high pressure",
        "unit": "cubic meter",
        "location": "CH",
    }
    before = deepcopy(source)
    weighted = 0
    for spec in wood.PROVIDERS.values():
        if spec[3] != "gas":
            continue
        ds = wood.allocated_provider(source, spec, alpha)
        quantity = {
            "fuel": fuel / wood.LHV["gas"],
            "electricity": electricity / 3.6,
            "heat": heat,
        }[spec[4]]
        weighted += quantity * ds["exchanges"][1]["amount"]
    assert weighted == pytest.approx(fuel / wood.LHV["gas"])
    assert source == before


def test_inactive_process_is_valid_but_orphan_coproducts_are_rejected():
    assert wood.allocation(0, 0, 0) == 1
    with pytest.raises(ValueError, match="without"):
        wood.allocation(0, 1, 0)
    with pytest.raises(ValueError, match="Invalid"):
        wood.allocation(1, -1, 1)


@pytest.mark.parametrize(
    "location,needs_regionalization", [("CH", False), ("NEU", True)]
)
def test_native_swiss_process_keeps_existing_suppliers(location, needs_regionalization):
    source = {
        "name": "source",
        "reference product": "fuel",
        "unit": "kilogram",
        "location": location,
        "exchanges": [
            {"type": "production", "name": "source", "amount": 1, "location": location},
            {"type": "technosphere", "name": "biomass", "amount": 2, "location": "NEU"},
        ],
    }
    original = deepcopy(source)
    basis = wood.process_basis(source)
    assert basis["STEM coproduct needs regionalization"] is needs_regionalization
    assert basis["exchanges"][1] == original["exchanges"][1]
    assert source == original


def test_derived_rows_keep_raw_inputs_and_do_not_cap_extreme_efficiency():
    values = {
        wood.ELECTRICITY: [0.25, 0.0001],
        wood.HEAT: [0.95, 0.00026],
        wood.FUEL_E: [0.2, 0.25],
        wood.FUEL_H: [0.8, 0.65],
        wood.EFFICIENCY: [0.25, 0.0001 / 0.9],
        wood.BIOGAS_E: [0.05, 0],
        wood.BIODIESEL_H: [0.3, 0],
        wood.BIOGAS: [1, 0],
        "Domestic biogas production": [4, 3],
    }
    frame = pd.DataFrame(
        [
            {
                "model": "STEM",
                "scenario": "arbitrary",
                "region": "CH",
                "unit": "PJ/yr.",
                "variables": v,
                2020: a[0],
                2050: a[1],
            }
            for v, a in values.items()
        ]
    )
    components = {
        "chp_e": np.array([0.2, 0.0001]),
        "chp_h": np.array([0.6, 0.00026]),
        "chp_fuel": np.array([1, 0.9]),
        "gas_e": np.array([0.05, 0]),
        "gas_h": np.array([0.05, 0]),
        "diesel_h": np.array([0.3, 0]),
    }
    result = wood.add_derived_series(frame, components).set_index("variables")
    pd.testing.assert_frame_equal(
        result.loc[frame.variables].reset_index()[frame.columns], frame
    )
    assert result.loc[wood.EFFICIENCY + wood.CHP_ONLY, 2050] == pytest.approx(
        0.0001 / 0.9
    )
    assert result.loc[wood.FUEL_E + wood.CHP_ONLY, 2020] == pytest.approx(0.25)
    assert result.loc[wood.RESIDUAL_BIOGAS, 2020] == 3
    components["gas_h"][0] = 0.2
    with pytest.raises(ValueError, match="heat total"):
        wood.add_derived_series(frame, components)


def test_configuration_has_one_path_per_separated_output():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "configuration_file/config.yaml").read_text())
    paths = config["production pathways"]
    for path, spec in wood.PROVIDERS.items():
        assert paths[path]["ecoinvent alias"]["name"] == spec[0]
        assert paths[path]["ecoinvent alias"]["reference product"] == spec[1]
    for name in (
        "market for electricity, high voltage (SPS)",
        "market for district heating (SPS)",
        "market for methane (SPS)",
    ):
        market = next(m for m in config["markets"] if m["name"] == name)
        variables = [
            paths[p]["production volume"]["variable"] for p in market["includes"]
        ]
        assert len(variables) == len(set(variables))
        assert wood.ELECTRICITY not in variables and wood.HEAT not in variables
