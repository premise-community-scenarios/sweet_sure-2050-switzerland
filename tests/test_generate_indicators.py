"""Guard against silently dropping demand and changing units or LCIA totals."""

import json
from pathlib import Path
import sys
import zipfile

import numpy as np
import pandas as pd
import pytest
import yaml

DEV = Path(__file__).resolve().parents[1] / "dev"
sys.path.insert(0, str(DEV))
from generate_indicators import (
    METHODS,
    aggregate_results,
    sector_for_variable,
    validate_result_coverage,
)
from indicator_coverage import digest, notebook_sectors, prepare_package


def test_all_notebook_sectors_preserved_and_missing_variables_included():
    original = notebook_sectors(DEV / "SWEET-SURE - indicators generation.ipynb")
    assert all(sector_for_variable(key) == value for key, value in original.items())
    assert sector_for_variable("EXT - 0 - FE_services_other_electric") == "services"
    assert sector_for_variable("EXT - 0 - FE_passenger_rail_electricity") == "transport"
    assert sector_for_variable("EXT - 0 - FE_industry_process_biomethane") == "industry"
    with pytest.raises(ValueError, match="No sector"):
        sector_for_variable("EXT - 0 - FE_unknown_sector")


def sample_results():
    water = next(method for method in METHODS if "fresh water" in method)
    return pd.DataFrame(
        [
            dict(
                variable="EXT - 0 - FE_services_other_electric",
                year=2050,
                region="CH",
                model="remind",
                scenario="SPS1_bas0",
                impact_category=water,
                location=location,
                act_category=str(index),
                value=float(value),
            )
            for index, (location, value) in enumerate(
                [("CH", 10), ("CH", -2), ("DE", 3), ("FR", 4), ("US", 5), ("GLO", -1)]
            )
        ]
    )


def test_aggregation_retains_negative_impacts_all_sectors_and_water_units():
    result = aggregate_results(sample_results(), {"DE", "FR", "CH"})
    assert result.set_index("location").value.to_dict() == {
        "CH": 8,
        "EU wo CH": 7,
        "RoW": 4,
    }
    assert set(result.unit) == {"m3"}
    assert set(result.sector) == {"services"}
    assert result.value.sum() == sample_results().value.sum()


def test_zero_methods_require_calculated_coordinates_and_survive_dense_export():
    raw = sample_results()
    method = raw.impact_category.iloc[0]
    methods = {method: ("EF test", "m3"), "RELICS test": ("RELICS test", "kilogram")}
    demands = [
        {"year": 2050, "variable": raw.variable.iloc[0], "scenario_demand": 4},
        {"year": 2050, "variable": "EXT - 0 - FE_bus_hydrogen", "scenario_demand": 0},
    ]
    with pytest.raises(ValueError, match="Missing year/indicator"):
        validate_result_coverage(raw, demands, [2050], methods)
    result = validate_result_coverage(
        raw, demands, [2050], methods, {"years": [2050], "methods": list(methods)}
    )
    assert result["zero_year_indicator_combinations"] == 1
    dense = aggregate_results(raw, {"DE", "FR"}, methods, demands)
    assert len(dense) == 2 * 2 * 3
    assert dense.loc[dense.impact_category.eq("RELICS test"), "value"].eq(0).all()
    assert (
        dense.loc[dense.variable.eq("EXT - 0 - FE_bus_hydrogen"), "value"].eq(0).all()
    )
    assert dense.value.sum() == raw.value.sum()


@pytest.mark.parametrize("invalid", [np.nan, np.inf, -np.inf])
def test_invalid_results_fail_instead_of_disappearing(invalid):
    raw = sample_results()
    raw.loc[0, "value"] = invalid
    with pytest.raises(ValueError):
        aggregate_results(raw, {"DE", "FR"})


def make_incomplete_package(tmp_path, duplicate=False):
    package = tmp_path / "test.zip"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "production pathways": {
                    "FE_industry_heat_DH": {
                        "ecoinvent alias": {
                            "name": "district heat",
                            "reference product": "heat",
                        }
                    }
                }
            }
        )
    )
    resources = {
        "scenario_data": "scenario_data/data.csv",
        "mapping": "mapping/mapping.yaml",
    }
    demand = b"variables,region,year,value,unit,model,pathway\nEXT - 0 - FE_industry_heat_DH,CH,2020,7,PJ/yr.,remind,test\n"
    index = "name;reference product;unit;location;index\ndistrict heat;heat;megajoule;CH;0\n"
    if duplicate:
        index += "district heat;heat;megajoule;CH;1\n"
    with zipfile.ZipFile(package, "w") as z:
        z.writestr(
            "datapackage.json",
            json.dumps(
                {"resources": [{"name": k, "path": v} for k, v in resources.items()]}
            ),
        )
        z.writestr(resources["scenario_data"], demand)
        z.writestr(resources["mapping"], "{}")
        z.writestr("inventories/remind/test/2020/A_matrix_index.csv", index)
        z.writestr("inventories/remind/test/2020/A_matrix.csv", "matrix unchanged")
    package.with_suffix(".build.json").write_text(
        json.dumps({"resources": {"config": {"sha256": digest(config)}}})
    )
    return package, config


def test_mapping_repair_preserves_original_and_all_matrix_bytes(tmp_path):
    package, config = make_incomplete_package(tmp_path)
    before = digest(package)
    repaired, report = prepare_package(
        package,
        config,
        DEV / "SWEET-SURE - indicators generation.ipynb",
        tmp_path / "output",
    )
    assert digest(package) == before
    assert report["verified_supplier_years"] == 1
    with zipfile.ZipFile(package) as old, zipfile.ZipFile(repaired) as new:
        for name in old.namelist():
            if name not in {"datapackage.json", "mapping/mapping.yaml"}:
                assert old.read(name) == new.read(name)
        assert "EXT - 0 - FE_industry_heat_DH" in yaml.safe_load(
            new.read("mapping/mapping.yaml")
        )


def test_mapping_repair_rejects_ambiguous_supplier(tmp_path):
    package, config = make_incomplete_package(tmp_path, duplicate=True)
    with pytest.raises(ValueError, match="one exact CH supplier"):
        prepare_package(
            package,
            config,
            DEV / "SWEET-SURE - indicators generation.ipynb",
            tmp_path / "output",
        )


def test_mapping_repair_rejects_changed_build_configuration(tmp_path):
    package, config = make_incomplete_package(tmp_path)
    config.write_text(config.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="exact configuration"):
        prepare_package(
            package,
            config,
            DEV / "SWEET-SURE - indicators generation.ipynb",
            tmp_path / "output",
        )
