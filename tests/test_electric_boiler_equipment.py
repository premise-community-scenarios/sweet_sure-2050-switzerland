"""Check dimensional allocation and explicitly documented material closure."""

from pathlib import Path
import sys
import math
import yaml
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from stem_electric_boilers import allocation
from update_electric_boiler_inventories import material_masses


def test_lifetime_allocation_reproduces_installed_equipment_per_year():
    # 2 GW / 10 kW = 200,000 units. Twenty years assigns 10,000 units/year.
    # With 12 PJ/year the saved amount must recover that allocation.
    assert allocation(2, 12, 10, 20) * 12e9 == pytest.approx(10000)
    assert allocation(2, 24, 10, 20) == pytest.approx(allocation(2, 12, 10, 20) / 2)


def test_idle_and_invalid_energy():
    assert allocation(1, 0, 10, 20) == 0
    for cap, energy in [(0, 1), (-1, 1), (1, -1), (math.nan, 1), (1, math.inf)]:
        with pytest.raises(ValueError):
            allocation(cap, energy, 10, 20)


def test_declared_mass_closes_and_proxies_are_visible():
    cfg = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "configuration_file/electric_boiler_equipment.yaml"
        ).read_text()
    )
    assert len({p["name"] for p in cfg["profiles"].values()}) == 4
    for p in cfg["profiles"].values():
        assert sum(material_masses(p).values()) == pytest.approx(p["mass_kg"])
        assert p["limitations"] and all(k in cfg["sources"] for k in p["source_ids"])
    assert "provisional" in " ".join(cfg["profiles"]["industry"]["limitations"])
