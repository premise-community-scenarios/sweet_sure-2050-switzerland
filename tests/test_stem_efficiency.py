from copy import deepcopy

import pytest

from dev.stem_efficiency import FuelBasis, apply_absolute, identity


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
