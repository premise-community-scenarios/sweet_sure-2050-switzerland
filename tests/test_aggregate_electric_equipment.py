"""Protect residual energy and the narrow end-use equipment boundary."""

from pathlib import Path
import sys
import yaml
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from update_aggregate_electric_inventories import shares


def test_unresolved_energy_is_not_reallocated_to_known_equipment():
    weights = shares({"energy_PJ": {"motor": 40, "unresolved_other": 60}})
    assert weights["motor"] == 0.4
    assert sum(weights.values()) == 1
    with pytest.raises(ValueError):
        shares({"energy_PJ": {"motor": -1}})


def test_three_distinct_endpoints_preserve_residual_categories():
    cfg = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "configuration_file/aggregate_electric_equipment.yaml"
        ).read_text()
    )
    assert len({p["name"] for p in cfg["profiles"].values()}) == 3
    for p in cfg["profiles"].values():
        assert sum(v for k, v in shares(p).items() if k.startswith("unresolved_")) > 0
        assert p["limitations"]
