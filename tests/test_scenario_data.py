"""Validate all declared STEM scenarios, including equipment-allocation inputs."""

import json
from pathlib import Path
import numpy as np
import pandas as pd
import yaml


def variables(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "variable":
                yield v
            else:
                yield from variables(v)
    elif isinstance(value, list):
        for v in value:
            yield from variables(v)


def test_scenario_data_file():
    descriptor = json.loads(Path("datapackage.json").read_text())
    data = pd.read_csv("scenario_data/scenario_data.csv")
    keys = ["model", "scenario", "region", "variables", "unit"]
    assert set(keys).issubset(data.columns)
    assert not data[keys].isna().any().any()
    assert not data.duplicated(keys).any()
    years = [c for c in data if c.isdigit()]
    assert years and all(2005 <= int(y) <= 2100 for y in years)
    assert np.isfinite(data[years].to_numpy(float)).all()
    assert set(data.scenario) == set(descriptor["scenarios"])
    assert set(data.model) == {"STEM"} and set(data.region) == {"CH", "SPS1"}
    # The existing pellet coproduct series use the documented SPS1 source region.
    assert (
        data.loc[data.region.eq("SPS1"), "variables"]
        .str.contains("wood to pellet production")
        .all()
    )
    config = yaml.safe_load(Path("configuration_file/config.yaml").read_text())
    required = set(variables(config))
    boilers = yaml.safe_load(
        Path("configuration_file/electric_boiler_equipment.yaml").read_text()
    )
    for p in boilers["profiles"].values():
        required.update([p["capacity_variable"], p["energy_variable"]])
    for scenario, group in data.groupby("scenario"):
        assert required.issubset(set(group.variables)), scenario
