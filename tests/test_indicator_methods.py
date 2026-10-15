"""Check version-specific RELICS matching and the EF headline-only selection."""

import json
from pathlib import Path
import zipfile

import pytest
import yaml

from dev.indicator_methods import EF_HEADLINES, freeze_methods, relics_methods

MAPPING = Path(__file__).resolve().parents[1] / "configuration_file/relics_mapping.yaml"


def test_relics_matches_all_prefixes_with_exact_compartments_and_factors():
    mapping = [
        {
            "name": "Antimony",
            "environmental flow": [
                {
                    "name": "Antimony",
                    "categories": "natural resource::in ground",
                    "amount": 1,
                    "unit": "kilogram",
                },
                {
                    "name": "Stibnite",
                    "categories": "natural resource::in ground",
                    "amount": 0.716836171,
                    "unit": "kilogram",
                },
            ],
        }
    ]
    flows = {
        ("Antimony", "natural resource", "in ground", "kilogram"),
        ("STIBNITE ore", "natural resource", "in ground", "kilogram"),
        ("Antimony", "water", "ground-", "kilogram"),
    }
    methods, audit = relics_methods(mapping, flows)
    factors = {r["name"]: r["amount"] for r in methods[0]["exchanges"]}
    assert factors == {"Antimony": 1, "STIBNITE ore": 0.716836171}
    assert audit[0]["flow_count"] == 2
    with pytest.raises(ValueError, match="No biosphere flow"):
        relics_methods(mapping, set())
    with pytest.raises(ValueError, match="flow unit"):
        relics_methods(mapping, {("Antimony", "natural resource", "in ground", "gram")})


def test_complete_registry_excludes_ef_subcategories_and_checks_every_package_index(
    tmp_path,
):
    mappings = yaml.safe_load(MAPPING.read_text())
    flows = sorted(
        {
            (f["name"], *f["categories"].split("::"), f["unit"])
            for m in mappings
            for f in m["environmental flow"]
        }
    )
    package = tmp_path / "base.zip"
    index = "name;compartment;subcompartment;unit;index\n" + "\n".join(
        ";".join([*f, str(i)]) for i, f in enumerate(flows)
    )
    with zipfile.ZipFile(package, "w") as archive:
        for year in (2020, 2050):
            archive.writestr(f"inventories/{year}/B_matrix_index.csv", index)
    cf = tmp_path / "lcia_ei312.json"
    ef = [
        {
            "name": ["EF v3.1", category, "test metric"],
            "unit": "test unit",
            "exchanges": [
                {
                    "name": "Aluminium",
                    "categories": ["natural resource", "in ground"],
                    "amount": 1,
                }
            ],
        }
        for category in sorted(EF_HEADLINES | {"climate change: fossil"})
    ]
    cf.write_text(json.dumps(ef))
    registry = freeze_methods(
        cf, MAPPING, [{"scenario": "base", "package": str(package)}], tmp_path
    )
    assert len(registry["methods"]) == 81
    assert len(registry["package_coverage_checks"]) == 2
    selected = [m for m in registry["methods"] if m["family"] == "EF v3.1"]
    assert {m["method_tuple"][1] for m in selected} == EF_HEADLINES
    assert (
        len([m for m in registry["methods"] if m["family"] == "RELICS"])
        == len(mappings)
        == 65
    )
    assert json.loads(cf.read_text())[: len(ef)] == ef
    assert "climate change: fossil" not in {m["indicator"] for m in selected}
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("inventories/2020/B_matrix_index.csv", index)
        archive.writestr(
            "inventories/2050/B_matrix_index.csv",
            index.replace("Aluminium;", "unrelated flow;"),
        )
    with pytest.raises(ValueError, match="Unmatched EF factors"):
        freeze_methods(
            cf, MAPPING, [{"scenario": "base", "package": str(package)}], tmp_path
        )
