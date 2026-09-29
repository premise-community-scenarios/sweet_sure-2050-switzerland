"""Inspect a rebuilt package and compare its matrices with a previous build.

Compares coefficients after aligning activities and biosphere flows by identity,
so reordered indices do not appear as inventory changes. No ZIP is modified.
"""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
import yaml


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resources(archive):
    descriptor = json.loads(archive.read("datapackage.json"))
    return {r["name"]: r["path"] for r in descriptor["resources"]}


def normalized_mapping(record):
    """Compare descriptors separately from their list order, without losing either."""
    normalized = dict(record)
    if "dataset" in normalized:
        normalized["dataset"] = sorted(
            normalized["dataset"], key=lambda item: json.dumps(item, sort_keys=True)
        )
    return normalized


def read_matrix(archive, directory, name):
    with archive.open(f"{directory}/{name}.csv") as stream:
        return pd.read_csv(stream, sep=";", skip_blank_lines=False)


def aligned_indices(before, after):
    fields = [c for c in before.columns if c != "index"]
    keys = [
        list(frame[fields].fillna("").itertuples(index=False, name=None))
        for frame in (before, after)
    ]
    if any(len(set(k)) != len(k) for k in keys):
        raise ValueError("Ambiguous matrix identities")
    identities = sorted(set(keys[0]) | set(keys[1]))
    positions = {key: i for i, key in enumerate(identities)}
    mappings = []
    for frame, frame_keys in zip((before, after), keys):
        mapping = np.empty(len(frame), dtype=np.int64)
        mapping[frame["index"].to_numpy()] = [positions[k] for k in frame_keys]
        mappings.append(mapping)
    return (
        identities,
        mappings,
        {
            "added": sorted(set(keys[1]) - set(keys[0])),
            "removed": sorted(set(keys[0]) - set(keys[1])),
        },
    )


def compare_matrix(before, after, activity_maps, supplier_maps, shape, kind):
    supplier_column = "index of product" if kind == "A" else "index of biosphere flow"
    matrices = []
    for i, frame in enumerate((before, after)):
        values = frame["value"].to_numpy()
        if kind == "A":
            values = values * np.where(frame["flip"].eq(1), -1, 1)
        matrices.append(
            coo_matrix(
                (
                    values,
                    (
                        supplier_maps[i][frame[supplier_column].to_numpy()],
                        activity_maps[i][frame["index of activity"].to_numpy()],
                    ),
                ),
                shape=shape,
            ).tocsr()
        )
    delta = matrices[1] - matrices[0]
    delta.eliminate_zeros()
    delta = delta.tocoo()
    outside_tolerance = np.zeros(delta.nnz, dtype=bool)
    if delta.nnz:
        old = np.asarray(matrices[0][delta.row, delta.col]).ravel()
        new = np.asarray(matrices[1][delta.row, delta.col]).ravel()
        outside_tolerance = ~np.isclose(old, new, rtol=1e-12, atol=1e-15)
    return {
        "previous_entries": len(before),
        "rebuilt_entries": len(after),
        "different_coefficients_exact": delta.nnz,
        "different_coefficients_above_tolerance": int(outside_tolerance.sum()),
        "maximum_absolute_difference": float(np.max(np.abs(delta.data), initial=0)),
        "rtol": 1e-12,
        "atol": 1e-15,
    }


def inspect(package, previous):
    build = json.loads(package.with_suffix(".build.json").read_text())
    source = Path(build["resources"]["scenario_data"]["path"])
    if digest(source) != build["resources"]["scenario_data"]["sha256"]:
        raise ValueError("Scenario source changed since the build")
    config_path = Path(build["resources"]["config"]["path"])
    if digest(config_path) != build["resources"]["config"]["sha256"]:
        raise ValueError("Configuration changed since the build")
    config = yaml.safe_load(config_path.read_text())
    frame = pd.read_csv(source)
    source_data = frame.loc[
        frame.scenario.eq(build["scenario"]) & frame.region.eq("CH")
    ].set_index("variables")
    input_years = sorted(int(c) for c in frame.columns if c.isdigit())
    result = {
        "package": str(package),
        "package_sha256": digest(package),
        "previous_package": str(previous),
        "previous_sha256": digest(previous),
        "matrix_comparison": [],
    }
    with zipfile.ZipFile(previous) as old_zip, zipfile.ZipFile(package) as new_zip:
        if new_zip.testzip() is not None:
            raise ValueError("ZIP checksum failure")
        old_resources, new_resources = resources(old_zip), resources(new_zip)
        old_mapping = yaml.safe_load(old_zip.read(old_resources["mapping"]))
        mapping = yaml.safe_load(new_zip.read(new_resources["mapping"]))
        data = pd.read_csv(new_zip.open(new_resources["scenario_data"]))
        old_data = pd.read_csv(old_zip.open(old_resources["scenario_data"]))
        keys = [c for c in data.columns if c != "value"]
        pd.testing.assert_frame_equal(
            old_data.sort_values(keys).reset_index(drop=True),
            data.sort_values(keys).reset_index(drop=True),
            check_exact=True,
        )
        fe = data.loc[
            data.region.eq("CH") & data.variables.str.startswith("EXT - 0 - FE")
        ]
        if fe.empty or set(fe.unit) != {"PJ/yr."}:
            raise ValueError("Missing or unexpected final-energy demand data")
        if fe.duplicated(["variables", "year"]).any():
            raise ValueError("Duplicate final-energy demands")
        missing = sorted(set(fe.variables) - mapping.keys())
        if missing:
            raise ValueError(f"Rebuilt package still has missing mappings: {missing}")
        expected_variables = {
            "EXT - 0 - " + v: settings["production volume"]["variable"]
            for v, settings in config["production pathways"].items()
            if v.startswith("FE_")
        }
        if set(fe.variables) != set(expected_variables):
            raise ValueError("Exported final-energy variables differ from STEM")
        if set(fe.year) != set(build["years"]) or len(fe) != len(
            expected_variables
        ) * len(build["years"]):
            raise ValueError("Incomplete variable/year coverage")
        for row in fe.itertuples(index=False):
            source_values = source_data.loc[
                expected_variables[row.variables], [str(y) for y in input_years]
            ].to_numpy(dtype=float)
            expected = np.interp(row.year, input_years, source_values)
            np.testing.assert_allclose(row.value, expected, rtol=1e-12, atol=1e-14)
        result.update(
            zip_integrity_passed=True,
            final_energy_variables=len(expected_variables),
            source_demand_values_checked=len(fe),
            scenario_data_unchanged=True,
            missing_final_energy_mappings=missing,
            added_mappings=sorted(mapping.keys() - old_mapping.keys()),
            removed_mappings=sorted(old_mapping.keys() - mapping.keys()),
            changed_existing_mappings=sorted(
                k
                for k in mapping.keys() & old_mapping.keys()
                if normalized_mapping(mapping[k]) != normalized_mapping(old_mapping[k])
            ),
            existing_mapping_order_changes=sorted(
                k
                for k in mapping.keys() & old_mapping.keys()
                if mapping[k] != old_mapping[k]
                and normalized_mapping(mapping[k]) == normalized_mapping(old_mapping[k])
            ),
            direct_supplier_checks=[],
            unmapped_scenario_variables=[
                {
                    "variable": variable,
                    "units": sorted(group.unit.unique().tolist()),
                    "regions": sorted(group.region.unique().tolist()),
                    "nonzero_values": int(group.value.abs().gt(1e-12).sum()),
                    "maximum_absolute_value": float(group.value.abs().max()),
                    "selected_by_final_energy_workflow": variable.startswith(
                        "EXT - 0 - FE"
                    ),
                }
                for variable, group in data.loc[~data.variables.isin(mapping)].groupby(
                    "variables"
                )
            ],
        )
        restored = sorted(set(fe.variables) - old_mapping.keys())
        members = sorted(n for n in new_zip.namelist() if n.endswith("/A_matrix.csv"))
        old_members = {n for n in old_zip.namelist() if n.endswith("/A_matrix.csv")}
        if set(members) != old_members:
            raise ValueError("Exported year directories changed")
        for member in members:
            directory = str(Path(member).parent)
            ai = [
                read_matrix(z, directory, "A_matrix_index") for z in (old_zip, new_zip)
            ]
            bi = [
                read_matrix(z, directory, "B_matrix_index") for z in (old_zip, new_zip)
            ]
            activities, amaps, activity_changes = aligned_indices(*ai)
            flows, bmaps, flow_changes = aligned_indices(*bi)
            year = int(Path(directory).name)
            for variable in restored:
                suppliers = mapping[variable]["dataset"]
                if len(suppliers) != 1:
                    raise ValueError(f"Unexpected restored mapping: {variable}")
                supplier = suppliers[0]
                found = ai[1].loc[
                    ai[1]["name"].eq(supplier["name"])
                    & ai[1]["reference product"].eq(supplier["reference product"])
                    & ai[1]["unit"].eq(supplier["unit"])
                    & ai[1]["location"].eq("CH")
                ]
                if len(found) != 1:
                    raise ValueError(
                        f"Non-unique restored CH supplier: {variable}, {year}"
                    )
                result["direct_supplier_checks"].append(
                    {
                        "year": year,
                        "variable": variable,
                        "supplier": supplier,
                        "location": "CH",
                    }
                )
            record = {
                "year": year,
                "activity_changes": activity_changes,
                "biosphere_flow_changes": flow_changes,
            }
            for kind, supplier_maps, size in (
                ("A", amaps, len(activities)),
                ("B", bmaps, len(flows)),
            ):
                matrices = [
                    read_matrix(z, directory, f"{kind}_matrix")
                    for z in (old_zip, new_zip)
                ]
                record[kind] = compare_matrix(
                    *matrices, amaps, supplier_maps, (size, len(activities)), kind
                )
                del matrices
            result["matrix_comparison"].append(record)
            print(json.dumps(record), flush=True)
    result["inventory_coefficients_unchanged_within_tolerance"] = all(
        row[kind]["different_coefficients_above_tolerance"] == 0
        for row in result["matrix_comparison"]
        for kind in ("A", "B")
    )
    result["inventory_identities_unchanged"] = all(
        not row[field][change]
        for row in result["matrix_comparison"]
        for field in ("activity_changes", "biosphere_flow_changes")
        for change in ("added", "removed")
    )
    output = package.with_suffix(".comparison.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Inspection report: {output}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("previous", type=Path)
    args = parser.parse_args()
    inspect(args.package.resolve(), args.previous.resolve())
