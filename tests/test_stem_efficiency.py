from copy import deepcopy

import pytest

from dev.stem_efficiency import (
    FuelBasis,
    apply_absolute,
    apply_electricity_input_efficiency,
    identity,
)


def generator():
    return {
        "name": "CHP",
        "reference product": "electricity",
        "location": "CH",
        "unit": "kilowatt hour",
        "exchanges": [
            {
                "name": "gas",
                "type": "technosphere",
                "unit": "cubic meter",
                "amount": 0.25,
            },
            {"name": "CO2", "type": "biosphere", "unit": "kilogram", "amount": 0.5},
            {"name": "CHP", "type": "production", "unit": "kilowatt hour", "amount": 1},
        ],
    }


def energy(exc):
    return exc["amount"] * 36 if exc["name"] == "gas" else 0


def rescale(exc, factor):
    exc["amount"] *= factor


def test_physical_chp_target_preserves_allocation():
    ds = generator()
    basis = FuelBasis([ds], {identity(ds): 0.75}, energy)
    assert basis.efficiency(ds) == pytest.approx(0.3)
    result = apply_absolute(ds, basis, 0.24, rescale)
    assert result["scaling_factor"] == pytest.approx(1.25)
    assert ds["exchanges"][0]["amount"] == pytest.approx(0.3125)
    assert ds["exchanges"][1]["amount"] == pytest.approx(0.625)
    assert ds["exchanges"][2]["amount"] == 1
    assert basis.efficiency(ds) == pytest.approx(0.24)


def test_ccs_uses_upstream_fuel_instead_of_electricity_conversion():
    source = generator()
    wrapper = {
        "name": "CHP CCS",
        "reference product": "electricity",
        "location": "CH",
        "unit": "kilowatt hour",
        "exchanges": [
            {
                "name": "CHP",
                "product": "electricity",
                "location": "CH",
                "unit": "kilowatt hour",
                "type": "technosphere",
                "amount": 1.08,
            },
        ],
    }
    basis = FuelBasis([source, wrapper], {identity(source): 0.75}, energy)
    assert basis.efficiency(wrapper) == pytest.approx(0.3 / 1.08)
    result = apply_absolute(wrapper, basis, 0.25, rescale)
    assert result["scaling_factor"] == pytest.approx((0.3 / 1.08) / 0.25)
    assert wrapper["exchanges"][0]["amount"] == pytest.approx(1.2)
    assert source["exchanges"][0]["amount"] == 0.25


def test_absolute_targets_above_sixty_percent_are_not_clipped():
    ds = generator()
    basis = FuelBasis([ds], {identity(ds): 1}, energy)
    apply_absolute(ds, basis, 0.85, rescale)
    assert basis.efficiency(ds) == pytest.approx(0.85)


def test_missing_supplier_and_circular_energy_inputs_fail():
    ds = generator()
    ds["exchanges"] = [
        {
            "name": "missing",
            "product": "electricity",
            "location": "CH",
            "unit": "kilowatt hour",
            "type": "technosphere",
            "amount": 1,
        }
    ]
    basis = FuelBasis([ds], {}, energy)
    with pytest.raises(ValueError, match="Unresolved energy supplier"):
        basis.efficiency(ds)
    ds["exchanges"][0]["name"] = ds["name"]
    with pytest.raises(ValueError, match="Cycle"):
        basis.efficiency(ds)


def test_waste_uses_declared_combustion_efficiency_not_auxiliary_fuel():
    ds = generator()
    ds["name"] = "electricity production, from incineration"
    ds["current efficiency"] = 0.1221
    ds["exchanges"][0]["amount"] = 0.001
    basis = FuelBasis([ds], {}, energy)
    assert basis.efficiency(ds) == pytest.approx(0.1221)
    result = apply_absolute(ds, basis, 0.06, rescale)
    assert result["scaling_factor"] == pytest.approx(0.1221 / 0.06)
    assert result["basis"] == "declared waste-combustion efficiency"
    assert basis.efficiency(ds) == pytest.approx(0.06)


def electricity_converter():
    return {
        "name": "arbitrary electricity converter",
        "reference product": "electricity",
        "unit": "kilowatt hour",
        "location": "CH",
        "current efficiency": 0.99,
        "exchanges": [
            {
                "name": "electricity",
                "type": "production",
                "unit": "kilowatt hour",
                "amount": 1,
            },
            {
                "name": "market for electricity, high voltage",
                "type": "technosphere",
                "unit": "kilowatt hour",
                "amount": 0.8,
            },
            {
                "name": "market for electricity, high voltage (SPS)",
                "type": "technosphere",
                "unit": "kilowatt hour",
                "amount": 0.63,
            },
            {
                "name": "market for electricity, low voltage",
                "type": "technosphere",
                "unit": "kilowatt hour",
                "amount": 0.01,
            },
            {
                "name": "plant construction",
                "type": "technosphere",
                "unit": "unit",
                "amount": 0.0001,
            },
            {
                "name": "Water",
                "type": "biosphere",
                "unit": "cubic meter",
                "amount": 4.29825,
            },
        ],
    }


@pytest.mark.parametrize("target", [0.8, 0.65, 0.91])
def test_selected_electricity_efficiency_preserves_other_exchanges(target):
    ds = electricity_converter()
    before = deepcopy(ds)
    record = apply_electricity_input_efficiency(
        ds, target, rescale, ["market for electricity, high voltage"]
    )
    inputs = ds["exchanges"][1:3]
    assert sum(e["amount"] for e in inputs) == pytest.approx(1 / target)
    assert inputs[0]["amount"] / inputs[1]["amount"] == pytest.approx(0.8 / 0.63)
    assert ds["exchanges"][0] == before["exchanges"][0]
    assert ds["exchanges"][3:] == before["exchanges"][3:]
    assert record["old_physical_efficiency"] == pytest.approx(1 / 1.43)
    assert record["verified_physical_efficiency"] == pytest.approx(target)
    assert record["basis"] == "explicitly selected electricity input/output"
    assert "biosphere scaling factor" not in ds["log parameters"]


@pytest.mark.parametrize(
    "fault",
    [
        "no match",
        "non-electric input",
        "negative input",
        "nonfinite",
        "multiple outputs",
    ],
)
def test_invalid_filtered_efficiency_fails_before_mutation(fault):
    ds = electricity_converter()
    names = ["market for electricity, high voltage"]
    if fault == "no match":
        names = ["missing"]
    elif fault == "non-electric input":
        ds["exchanges"][1]["unit"] = "megajoule"
    elif fault == "negative input":
        ds["exchanges"][1]["amount"] = -1
    elif fault == "nonfinite":
        ds["exchanges"][1]["amount"] = float("inf")
    else:
        ds["exchanges"].append(deepcopy(ds["exchanges"][0]))
    before = deepcopy(ds)
    with pytest.raises(ValueError, match="requires one electricity output"):
        apply_electricity_input_efficiency(ds, 0.8, rescale, names)
    assert ds == before


def test_pumping_mapping_explicitly_selects_only_electricity():
    from pathlib import Path
    import yaml

    config = yaml.safe_load(
        (Path(__file__).parents[1] / "configuration_file/config.yaml").read_text()
    )
    assert config["production pathways"]["hydro (pumped storage)"]["efficiency"] == [
        {
            "variable": "Efficiency of power plants (electric)|Hydro|Pump storage",
            "absolute": True,
            "includes": {
                "technosphere": [
                    "market for electricity, high voltage",
                    "market group for electricity, high voltage",
                ]
            },
        }
    ]


def test_configured_filters_handle_regional_electricity_market_groups():
    from pathlib import Path
    import yaml

    config = yaml.safe_load(
        (Path(__file__).parents[1] / "configuration_file/config.yaml").read_text()
    )
    ds = electricity_converter()
    ds["exchanges"][1]["name"] = "market group for electricity, high voltage"
    filters = config["production pathways"]["hydro (pumped storage)"]["efficiency"][0][
        "includes"
    ]["technosphere"]
    record = apply_electricity_input_efficiency(ds, 0.8, rescale, filters)
    assert record["old_electricity_input"] == pytest.approx(1.43)
    assert record["new_electricity_input"] == pytest.approx(1.25)


def test_unbounded_electricity_efficiency_needs_no_optional_bounds_module(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "premise.efficiency_bounds", None)
    ds = electricity_converter()
    record = apply_electricity_input_efficiency(
        ds, 0.8, rescale, ["market for electricity, high voltage"]
    )
    assert record["verified_physical_efficiency"] == pytest.approx(0.8)
    assert record["efficiency_bounds"] is None
