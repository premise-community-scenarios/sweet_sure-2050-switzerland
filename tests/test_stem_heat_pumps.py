from copy import deepcopy

import pandas as pd
import pytest

from dev.stem_heat_pumps import (
    COP_VARIABLES,
    GRID_PRODUCT,
    HEAT_NAME,
    HEAT_PRODUCT,
    StemCOPs,
    make_heat_pump,
    normalize_wrapper,
    operating_electricity,
    physical_heat_pump,
    swiss_wrapper,
)


def source():
    return {
        "name": HEAT_NAME,
        "reference product": HEAT_PRODUCT,
        "unit": "megajoule",
        "location": "NEU",
        "code": "background-heat",
        "current efficiency": 2.8,
        "exchanges": [
            {
                "name": HEAT_NAME,
                "product": HEAT_PRODUCT,
                "location": "NEU",
                "unit": "megajoule",
                "amount": 1,
                "type": "production",
                "input": ("old", "id"),
            },
            {
                "name": "market group for electricity, low voltage",
                "product": GRID_PRODUCT,
                "location": "NEU",
                "unit": "kilowatt hour",
                "amount": 0.0992,
                "type": "technosphere",
                "uncertainty type": 2,
                "loc": -2.3,
                "scale": 0.1,
            },
            {
                "name": "heat pump manufacture",
                "product": "heat pump",
                "location": "GLO",
                "unit": "unit",
                "amount": 1e-6,
                "type": "technosphere",
                "uncertainty type": 2,
                "loc": -13.8,
                "scale": 0.2,
            },
            {
                "name": "refrigerant",
                "categories": ("air",),
                "unit": "kilogram",
                "amount": 4e-6,
                "type": "biosphere",
            },
        ],
    }


@pytest.mark.parametrize("cop", [1.9, 2.9, 4.1, 8.0])
def test_cop_changes_only_operating_electricity_per_heat_and_conserves_final_electricity(
    cop,
):
    original = source()
    before = deepcopy(original)
    heat = make_heat_pump(original, "FE_services_heat_pump", cop)
    wrapper = {
        "name": "electricity used in heat pump",
        "reference product": GRID_PRODUCT,
        "unit": "megajoule",
        "location": "CH",
        "exchanges": [
            {
                "name": HEAT_NAME,
                "product": HEAT_PRODUCT,
                "unit": "megajoule",
                "location": "CH",
                "amount": 2.801,
                "type": "technosphere",
                "input": ("old", "heat"),
            },
            {
                "name": "Energy, geothermal, converted",
                "type": "biosphere",
                "amount": 2.0,
                "unit": "megajoule",
                "categories": ("natural resource", "in ground"),
            },
        ],
    }
    direct_before = deepcopy(wrapper["exchanges"][1])
    normalize_wrapper(wrapper, heat, cop)
    assert wrapper["exchanges"][1] == direct_before
    assert original == before
    assert heat["exchanges"][2:] == original["exchanges"][2:]
    (e,) = operating_electricity(heat)
    assert e["amount"] * 3.6 * cop == pytest.approx(1)
    assert e["location"] == "CH"
    assert e["uncertainty type"] == 0
    assert "scale" not in e and "loc" not in e
    assert wrapper["exchanges"][0]["amount"] == cop
    assert wrapper["exchanges"][0]["name"] == heat["name"]
    assert "input" not in wrapper["exchanges"][0]
    assert heat["code"] != original["code"]


def test_proxy_resolution_and_ambiguous_supplier_rejection():
    heat = source()
    proxy = deepcopy(heat)
    proxy["location"] = "CH"
    proxy["exchanges"] = [
        {
            "name": HEAT_NAME,
            "product": HEAT_PRODUCT,
            "unit": "megajoule",
            "location": "NEU",
            "amount": 1,
            "type": "technosphere",
        }
    ]
    actual, chain = physical_heat_pump([proxy, heat])
    assert actual is heat and len(chain) == 2
    with pytest.raises(ValueError, match="found 2"):
        physical_heat_pump([proxy, heat, deepcopy(heat)])
    proxy["exchanges"][0]["location"] = "CH"
    with pytest.raises(ValueError, match="Cycle"):
        physical_heat_pump([proxy, heat])


@pytest.mark.parametrize("cop", [0, -1, float("nan"), float("inf")])
def test_invalid_cop_is_rejected(cop):
    with pytest.raises(ValueError, match="Invalid STEM"):
        make_heat_pump(source(), "DH_heat_pump", cop)


def test_cops_are_scenario_specific_and_interpolated_without_extrapolation(tmp_path):
    variable = COP_VARIABLES["DH_heat_pump"]
    p = tmp_path / "scenario.csv"
    pd.DataFrame(
        [
            {"scenario": s, "region": "CH", "variables": variable, "2020": a, "2050": b}
            for s, a, b in [("custom_scenario", 2, 4), ("other_scenario", 6, 8)]
        ]
    ).to_csv(p, index=False)
    cops = StemCOPs(p, "custom_scenario")
    assert cops.value(variable, 2035) == 3
    assert StemCOPs(p, "other_scenario").value(variable, 2035) == 7
    with pytest.raises(ValueError, match="extrapolation"):
        cops.value(variable, 2060)


def test_regional_water_heating_wrapper_gets_a_separate_swiss_endpoint():
    regional = {
        "name": "electricity used in water heat pump",
        "reference product": GRID_PRODUCT,
        "unit": "megajoule",
        "location": "NEU",
        "code": "regional",
        "regions": ["CH"],
        "emptied": True,
        "exchanges": [
            {
                "name": "electricity used in water heat pump",
                "product": GRID_PRODUCT,
                "unit": "megajoule",
                "location": "NEU",
                "amount": 1,
                "type": "production",
            },
            {
                "name": HEAT_NAME,
                "product": HEAT_PRODUCT,
                "unit": "megajoule",
                "location": "NEU",
                "amount": 2.8,
                "type": "technosphere",
            },
        ],
    }
    before = deepcopy(regional)
    database = [regional]
    alias = {"name": regional["name"], "reference product": GRID_PRODUCT}
    wrapper = swiss_wrapper(database, [], alias)
    heat = make_heat_pump(source(), "FE_residential_water_heating_heat_pump", 2.5)
    normalize_wrapper(wrapper, heat, 2.5)
    assert regional == before
    assert len(database) == 2 and wrapper["location"] == "CH"
    assert wrapper["exchanges"][0]["location"] == "CH"
    assert wrapper["exchanges"][1]["amount"] == 2.5
    assert "emptied" not in wrapper and "regions" not in wrapper
    assert swiss_wrapper(database, [], alias) is wrapper
