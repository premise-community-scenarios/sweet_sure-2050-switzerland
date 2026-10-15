"""Guard the mass basis, mapping completeness, and saved Pathways demands."""

import json
from pathlib import Path
import sys
from zipfile import ZipFile

import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from refined_metal_methods import UniqueLoader, content_fraction, prepare
from run_refined_metals import verify_demands


def test_hydrated_lithium_mass_is_not_anhydrous_mass():
    weights = {"Li": 6.94, "H": 1.008, "O": 15.999}
    assert content_fraction("LiHO", "Li", weights) == pytest.approx(0.2898, rel=0.001)
    assert content_fraction("LiH3O2", "Li", weights) == pytest.approx(0.1654, rel=0.001)


@pytest.mark.parametrize("formula", ["Li(OH)", "Li0O", "NaOH", "LiOH.H2O"])
def test_ambiguous_or_unsupported_formula_fails(formula):
    with pytest.raises(ValueError):
        content_fraction(formula, "Li", {"Li": 6.94, "H": 1.008, "O": 15.999})


def test_duplicate_product_cannot_silently_replace_its_factor():
    with pytest.raises(ValueError, match="Duplicate YAML key"):
        yaml.load("copper: 1\ncopper: 0.5", Loader=UniqueLoader)


def test_missing_required_product_and_incompatible_mass_unit(tmp_path):
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(
        yaml.safe_dump(
            {
                "indicators": [
                    {
                        "name": "Copper",
                        "symbol": "Cu",
                        "products": {"copper": {"factor": 1}},
                    }
                ]
            }
        )
    )
    package = tmp_path / "scenario.zip"
    for product, unit, message in [
        ("copper ore", "kilogram", "Required product absent"),
        ("copper", "cubic meter", "Unexpected reference units"),
    ]:
        with ZipFile(package, "w") as archive:
            archive.writestr(
                "inventory/2020/A_matrix_index.csv",
                f"name;reference product;unit;location;index\nrefinery;{product};{unit};CH;0\n",
            )
        with pytest.raises(ValueError, match=message):
            prepare(mapping, [package], tmp_path)


def test_saved_demands_must_match_package_energy_and_supplier(tmp_path):
    package = tmp_path / "scenario.zip"
    variable = "EXT - 0 - FE_test"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "scenario_data/scenario_data.csv",
            f"region,variables,year,unit,value\nCH,{variable},2020,PJ/yr.,2.5\n",
        )
        archive.writestr(
            "mapping/mapping.yaml",
            yaml.safe_dump(
                {
                    variable: {
                        "dataset": [
                            {
                                "name": "power",
                                "reference product": "electricity",
                                "unit": "megajoule",
                            }
                        ]
                    }
                }
            ),
        )
    frame = pd.DataFrame(
        [
            dict(
                variable=variable,
                year=2020,
                scenario_unit="PJ/yr.",
                unit="megajoule",
                conversion_factor=1e9,
                scenario_demand=2.5,
                name="power",
                reference_product="electricity",
            )
        ]
    )
    verify_demands(package, frame)
    with pytest.raises(AssertionError):
        verify_demands(package, frame.assign(scenario_demand=2.6))
    with pytest.raises(ValueError, match="unit conversion"):
        verify_demands(package, frame.assign(conversion_factor=1e6))
    with pytest.raises(ValueError, match="supplier differs"):
        verify_demands(package, frame.assign(name="other plant"))


def test_claim_of_absent_metal_must_agree_with_product_catalogue(tmp_path):
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(
        yaml.safe_dump(
            {"unavailable_elements": {"Lutetium": "No identified refined product"}}
        )
    )
    package = tmp_path / "scenario.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "inventory/2020/A_matrix_index.csv",
            "name;reference product;unit;location;index\nrefinery;lutetium oxide;kilogram;CH;0\n",
        )
    with pytest.raises(ValueError, match="Claimed absent element"):
        prepare(mapping, [package], tmp_path)
