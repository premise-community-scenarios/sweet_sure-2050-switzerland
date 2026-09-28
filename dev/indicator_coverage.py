"""Audit notebook/package coverage and restore three verified export mappings.

The original package and every inventory matrix are retained unchanged. Repairs
require the exact build configuration and one matching CH supplier in every year.
"""

import ast
import copy
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import yaml

KNOWN_MISSING = {
    "EXT - 0 - FE_industry_heat_DH",
    "EXT - 0 - FE_services_DH",
    "EXT - 0 - FE_industry_heat_CHP_fuel_cell",
}


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def notebook_sectors(path):
    for cell in json.loads(Path(path).read_text())["cells"]:
        if cell["cell_type"] != "code":
            continue
        for node in ast.parse("".join(cell["source"])).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "mapping"
                for target in node.targets
            ):
                return ast.literal_eval(node.value)
    raise ValueError("Notebook sector lookup not found")


def prepare_package(package, config_path, notebook_path, output):
    """Return an audited package, with a local mapping-only repair if necessary."""
    report_dir = output / "coverage" / package.stem
    report_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        descriptor = json.loads(archive.read("datapackage.json"))
        resource_paths = {r["name"]: r["path"] for r in descriptor["resources"]}
        mapping = yaml.safe_load(archive.read(resource_paths["mapping"]))
        data = pd.read_csv(archive.open(resource_paths["scenario_data"]))
        fe = data.loc[
            data.region.eq("CH") & data.variables.str.startswith("EXT - 0 - FE")
        ].copy()
        if fe.empty or set(fe.unit) != {"PJ/yr."}:
            raise ValueError("Expected Swiss final-energy data in PJ/yr.")
        if not np.isfinite(fe.value).all() or (fe.value < -1e-12).any():
            raise ValueError("Invalid final-energy demands in package")
        missing = set(fe.variables) - mapping.keys()
        old_sectors = notebook_sectors(notebook_path)
        fe["missing_notebook_sector"] = ~fe.variables.isin(old_sectors)
        fe["missing_package_mapping"] = ~fe.variables.isin(mapping)
        fe["omitted_by_original_workflow"] = (
            fe.missing_notebook_sector | fe.missing_package_mapping
        )
        fe.to_csv(report_dir / "variable_coverage.csv", index=False)
        summary = (
            fe.groupby("year").value.sum().rename("total_final_energy_PJ").to_frame()
        )
        for label in [
            "missing_notebook_sector",
            "missing_package_mapping",
            "omitted_by_original_workflow",
        ]:
            summary[label + "_PJ"] = (
                fe.loc[fe[label]]
                .groupby("year")
                .value.sum()
                .reindex(summary.index, fill_value=0)
            )
            summary[label + "_percent"] = (
                100 * summary[label + "_PJ"] / summary.total_final_energy_PJ
            )
        summary.to_csv(report_dir / "demand_coverage.csv")
        report = {
            "original_package": str(package),
            "original_sha256": digest(package),
            "final_energy_variables": fe.variables.nunique(),
            "missing_notebook_sectors": sorted(
                set(fe.loc[fe.missing_notebook_sector, "variables"])
            ),
            "nonzero_missing_notebook_sectors": sorted(
                set(
                    fe.loc[
                        fe.missing_notebook_sector & fe.value.abs().gt(1e-10),
                        "variables",
                    ]
                )
            ),
            "missing_package_mappings": sorted(missing),
            "obsolete_notebook_sectors": sorted(old_sectors.keys() - set(fe.variables)),
            "explanations": {
                "notebook": "Incomplete static sector lookup; services_other_electric also differs from the notebook's services_FE_other_electric spelling. Pandas groupby drops unmapped sectors.",
                "district_heat": "premise.check_inventories skips duplicate handling for new datasets, then keys d_datasets by name/product. The shared market overwrites industry/services with the last residential variable.",
                "industrial_fuel_cell": "premise.check_inventories overwrites the production-pathway entry with the regionalize entry via d_datasets.update(), losing its variable metadata.",
            },
        }
        working_package = package
        if missing:
            if missing - KNOWN_MISSING:
                raise ValueError(
                    f"Unexpected missing inventory mappings: {sorted(missing)}"
                )
            build = json.loads(package.with_suffix(".build.json").read_text())
            if build["resources"]["config"]["sha256"] != digest(config_path):
                raise ValueError(
                    "Mapping repair requires the exact configuration used to build the ZIP"
                )
            configuration = yaml.safe_load(config_path.read_text())
            indices = {
                int(Path(name).parent.name): pd.read_csv(archive.open(name), sep=";")
                for name in archive.namelist()
                if name.endswith("/A_matrix_index.csv")
            }
            repairs = {}
            for variable in sorted(missing):
                key = variable.removeprefix("EXT - 0 - ")
                alias = configuration["production pathways"][key]["ecoinvent alias"]
                matched = []
                for year, index in indices.items():
                    found = index.loc[
                        index.name.eq(alias["name"])
                        & index["reference product"].eq(alias["reference product"])
                        & index.location.eq("CH")
                    ]
                    if len(found) != 1:
                        raise ValueError(
                            f"Repair requires one exact CH supplier for {variable}/{year}, found {len(found)}"
                        )
                    matched.append(found.iloc[0]["unit"])
                if set(matched) != {"megajoule"}:
                    raise ValueError(
                        f"Unexpected supplier units for {variable}: {matched}"
                    )
                repairs[variable] = {
                    "dataset": [
                        {
                            "name": alias["name"],
                            "reference product": alias["reference product"],
                            "unit": "megajoule",
                        }
                    ]
                }
            mapping.update(repairs)
            working_package = report_dir / package.name
            mapping_bytes = yaml.safe_dump(mapping, sort_keys=True).encode()
            # Update optional resource integrity metadata if supplied.
            for resource in descriptor["resources"]:
                if resource["name"] == "mapping":
                    if "bytes" in resource:
                        resource["bytes"] = len(mapping_bytes)
                    if "hash" in resource:
                        raise ValueError(
                            "Mapping resource has a hash; explicit descriptor hash handling is required"
                        )
            with zipfile.ZipFile(
                working_package, "w", compression=zipfile.ZIP_DEFLATED
            ) as repaired:
                for member in archive.infolist():
                    content = archive.read(member.filename)
                    if member.filename == resource_paths["mapping"]:
                        content = mapping_bytes
                    elif member.filename == "datapackage.json":
                        content = json.dumps(descriptor, indent=2).encode()
                    repaired.writestr(copy.copy(member), content)
            with zipfile.ZipFile(working_package) as repaired:
                untouched = [
                    n
                    for n in archive.namelist()
                    if n not in (resource_paths["mapping"], "datapackage.json")
                ]
                # Byte-for-byte comparison includes scenario data and all matrices.
                if any(archive.read(n) != repaired.read(n) for n in untouched):
                    raise ValueError("Non-mapping data changed during package repair")
            report.update(
                repairs=repairs,
                repair_config_sha256=digest(config_path),
                verified_supplier_years=len(indices) * len(repairs),
                unchanged_resource_files=len(untouched),
            )
            print(
                f"Restored {len(repairs)} mappings in an audited package copy: {working_package}"
            )
        report.update(
            working_package=str(working_package), working_sha256=digest(working_package)
        )
        (report_dir / "coverage.json").write_text(json.dumps(report, indent=2) + "\n")
    return working_package, report


def export_impact_omissions(raw, coverage, output, stem):
    """Quantify actual LCIA omitted by the notebook, after calculating all demands."""
    keys = ["year", "impact_category"]
    frame = raw.groupby(keys)["value"].sum().rename("corrected_total").to_frame()
    sector_missing = set(coverage["missing_notebook_sectors"])
    package_missing = set(coverage["missing_package_mappings"])
    for label, variables in (
        ("sector_lookup_omission", sector_missing),
        ("package_mapping_omission", package_missing),
        ("combined_omission", sector_missing | package_missing),
    ):
        frame[label] = (
            raw.loc[raw.variable.isin(variables)]
            .groupby(keys)
            .value.sum()
            .reindex(frame.index, fill_value=0)
        )
        frame[label + "_percent"] = (
            100 * frame[label] / frame.corrected_total.replace(0, np.nan)
        )
    frame["original_workflow_total"] = frame.corrected_total - frame.combined_omission
    frame.to_csv(output / f"{stem}_omitted_impacts.csv")
