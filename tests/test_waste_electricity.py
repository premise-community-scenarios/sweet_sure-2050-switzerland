from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from dev.add_waste_electricity_inventories import VARIANTS, blocks
from dev.format import format_frame

ROOT = Path(__file__).resolve().parents[1]


def test_formatter_preserves_separate_waste_and_chp_output():
    values = {
        "Exports|Electricity": 1,
        "Imports|Electricity": 3,
        "Electricity generation|Nuclear Fuel": 10,
    }
    for i, spec in enumerate(VARIANTS, 1):
        values[spec["variable"]] = i
        values[
            spec["variable"].replace(
                "Wastes Incineration (electric only)",
                "CHP Wastes (for District Heating)",
            )
        ] = (
            i + 10
        )
    frame = pd.DataFrame(
        [
            ["STEM", "SPS1", "CH", v, "PJ/yr", *([amount] * 7)]
            for v, amount in values.items()
        ]
    )
    actual = format_frame(frame, "SPS1_bas0").set_index("variables")
    for variable, amount in values.items():
        expected = 2 if variable == "Imports|Electricity" else amount
        assert np.all(actual.loc[variable, 2020:2050].to_numpy(float) == expected)


def test_waste_pathways_are_unique_and_use_corresponding_efficiencies():
    config = yaml.safe_load((ROOT / "configuration_file/config.yaml").read_text())
    market = next(
        m
        for m in config["markets"]
        if m["name"] == "market for electricity, high voltage (SPS)"
    )
    variables = []
    for name in market["includes"]:
        settings = config["production pathways"][name]
        variables.append(settings["production volume"]["variable"])
        if name.startswith("waste incineration,"):
            production = settings["production volume"]["variable"].split("|")[-1]
            assert settings["efficiency"] == [
                {
                    "variable": "Efficiency of power plants (electric)|Wastes|"
                    + production,
                    "absolute": True,
                }
            ]
    assert len(variables) == len(set(variables))
    for spec in VARIANTS:
        assert variables.count(spec["variable"]) == 1
        settings = config["production pathways"][spec["pathway"]]
        assert settings["ecoinvent alias"]["name"] == spec["name"]


@pytest.mark.parametrize("spec", VARIANTS, ids=[v["pathway"] for v in VARIANTS])
def test_electricity_only_inventory_allocation_and_carbon_balance(spec):
    inventories = blocks(ROOT / "inventories/lci-sweet_sure.xlsx")
    source = inventories[spec["source"]]
    target = inventories[spec["name"]]
    # Trailing blank rows do not belong to the inventory itself.
    source = [r for r in source if any(v is not None for v in r)]
    target = [r for r in target if any(v is not None for v in r)]
    assert len(source) == len(target)
    for a, b in zip(source, target):
        if a[5] in ("biosphere", "technosphere"):
            assert b[1] == pytest.approx(a[1] / spec["fraction"])
            assert a[:1] + a[2:6] + a[7:] == b[:1] + b[2:6] + b[7:]
            assert not any(b[8:])
        elif a[5] == "production":
            assert b[0] == spec["name"] and b[7] == spec["product"] and b[1] == 1
        elif a[0] == "current efficiency":
            assert a[1] == b[1] > 0
    if "biowaste" in spec["name"]:
        amount = {r[0]: r[1] for r in target if r[5] in ("biosphere", "technosphere")}
        captured = sum(
            v for k, v in amount.items() if k.startswith("carbon dioxide, captured")
        )
        assert amount["Carbon dioxide, in air"] == pytest.approx(
            amount["Carbon dioxide, non-fossil"] + captured
        )
