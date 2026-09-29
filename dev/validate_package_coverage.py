"""Check native final-energy mappings and STEM demands in one exported ZIP."""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import yaml


def validate(package):
    build = json.loads(package.with_suffix(".build.json").read_text())
    for resource in build["resources"].values():
        assert (
            hashlib.sha256(Path(resource["path"]).read_bytes()).hexdigest()
            == resource["sha256"]
        )
    config = yaml.safe_load(Path(build["resources"]["config"]["path"]).read_text())
    source = pd.read_csv(build["resources"]["scenario_data"]["path"])
    source = source.loc[
        source.scenario.eq(build["scenario"]) & source.region.eq("CH")
    ].set_index("variables")
    input_years = sorted(int(c) for c in source.columns if c.isdigit())
    expected = {
        "EXT - 0 - " + name: settings["production volume"]["variable"]
        for name, settings in config["production pathways"].items()
        if name.startswith("FE_")
    }
    checks = []
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None, "ZIP integrity failure"
        descriptor = json.loads(archive.read("datapackage.json"))
        resources = {r["name"]: r["path"] for r in descriptor["resources"]}
        mapping = yaml.safe_load(archive.read(resources["mapping"]))
        data = pd.read_csv(archive.open(resources["scenario_data"]))
        selected = data.loc[
            data.region.eq("CH") & data.variables.str.startswith("EXT - 0 - FE")
        ]
        assert set(selected.variables) == set(expected)
        assert not selected.duplicated(["variables", "year"]).any()
        assert set(selected.year) == set(build["years"])
        assert len(selected) == len(expected) * len(build["years"])
        assert set(selected.unit) == {"PJ/yr."}
        assert np.isfinite(selected.value).all() and selected.value.ge(-1e-12).all()
        assert (
            not set(expected) - mapping.keys()
        ), "Missing native final-energy mappings"
        for row in selected.itertuples(index=False):
            values = source.loc[
                expected[row.variables], [str(y) for y in input_years]
            ].to_numpy(dtype=float)
            np.testing.assert_allclose(
                row.value,
                np.interp(row.year, input_years, values),
                rtol=1e-12,
                atol=1e-14,
            )
        for year in build["years"]:
            paths = [
                p
                for p in archive.namelist()
                if p.endswith(f"/{year}/A_matrix_index.csv")
            ]
            assert len(paths) == 1, (year, paths)
            activities = pd.read_csv(archive.open(paths[0]), sep=";")
            for variable in sorted(expected):
                supplier = mapping[variable]["dataset"][0]
                matches = activities.loc[
                    activities.name.eq(supplier["name"])
                    & activities["reference product"].eq(supplier["reference product"])
                    & activities.unit.eq(supplier["unit"])
                ]
                assert not matches.empty, (year, variable, supplier)
                checks.append(
                    {
                        "year": year,
                        "variable": variable,
                        "supplier": supplier,
                        "locations": sorted(matches.location.unique().tolist()),
                    }
                )
    result = {
        "package": str(package.resolve()),
        "scenario": build["scenario"],
        "passed": True,
        "native_final_energy_variables": len(expected),
        "source_demand_values_checked": len(selected),
        "supplier_year_checks": len(checks),
        "missing_native_mappings": 0,
        "missing_inventory_suppliers": 0,
        "note": "Supplier presence is checked in every year's inventory. This check does not run Pathways geographic fallback or LCIA.",
        "suppliers": checks,
    }
    package.with_suffix(".coverage.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "suppliers"}, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    validate(parser.parse_args().package.resolve())
