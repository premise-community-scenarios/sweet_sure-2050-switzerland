#!/usr/bin/env python3
"""Export verified Pathways results using Toby's documented prem1 CSV schema.

This script only writes local files. It does not upload or register parameters.
Reference files are public downloads from https://toby.euler.usi.ch/.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pandas as pd

from generate_indicators import METHODS, sector_for_variable, sha256, write_json

PREM1_COLUMNS = [
    "sector",
    "variable",
    "year",
    "region",
    "model",
    "scenario",
    "impact",
    "location",
    "value",
    "unit",
]
LOCATIONS = ["CH", "EU wo CH", "RoW"]
UNITS = dict(METHODS.values())


def parameter_path(variable, impact, location):
    """Match Toby's existing hierarchy, including its legacy other_transport names."""
    sector = sector_for_variable(variable)
    name = variable.removeprefix("EXT - 0 - FE_")
    if sector == "transport":
        if name.startswith("other_transport_"):
            components = ["transport", name.replace("other_transport_", "other_", 1)]
        elif name in ("tram", "trolleybus"):
            components = ["transport", name]
        else:
            mode, fuel = name.rsplit("_", 1)
            components = ["transport", mode, fuel]
    else:
        name = name.removeprefix(sector + "_")
        components = [sector]
        subgroups = {
            "industry": ("heat", "process"),
            "residential": ("space_heating", "water_heating"),
            "services": (),
        }[sector]
        for subgroup in subgroups:
            if name.startswith(subgroup + "_"):
                components += [subgroup, name.removeprefix(subgroup + "_")]
                break
        else:
            components.append(name)
    # The original workbook carries a redundant FE_ in two variable names;
    # Toby's registered labels omit it. Preserve the original variable in prem1.
    components[-1] = components[-1].removeprefix("FE_")
    return " / ".join(["premise", impact, *components, location])


def read_registry(reference):
    rows = []
    for row in ET.parse(reference / "parameter_paths.xml").findall("./tbl/row"):
        if row.get("name1") != "premise" or row.get("leaf") != "1":
            continue
        path = " / ".join(
            row.get(f"name{i}") for i in range(1, 11) if row.get(f"name{i}")
        )
        rows.append({"path": path, "prm_id": int(row.get("prm_id"))})
    registry = pd.DataFrame(rows)
    if (
        registry.empty
        or registry.path.duplicated().any()
        or registry.prm_id.duplicated().any()
    ):
        raise ValueError("Missing or ambiguous premise parameter registry")
    old = pd.read_csv(reference / "old_SPS1_bas0.csv")
    known = old[["prm_id", "path", "unit"]].merge(
        registry,
        on=["prm_id", "path"],
        validate="one_to_one",
        how="outer",
        indicator=True,
    )
    if not known._merge.eq("both").all():
        raise ValueError("Old report and current parameter registry differ")
    return known.drop(columns="_merge")


def dense_results(frame, variables, years, scenario):
    """Include zero-valued series instead of leaving old platform values ambiguous."""
    keys = ["variable", "year", "impact_category", "location"]
    expected = pd.MultiIndex.from_product(
        [variables, years, UNITS, LOCATIONS], names=keys
    )
    if frame.duplicated(keys).any():
        raise ValueError("Duplicate indicator coordinates")
    if (
        set(frame.region) != {"CH"}
        or set(frame.model) != {"remind"}
        or set(frame.scenario) != {scenario}
    ):
        raise ValueError("Unexpected scenario coordinates")
    if not frame.unit.eq(frame.impact_category.map(UNITS)).all():
        raise ValueError("Unexpected indicator units")
    values = frame.set_index(keys).value
    if len(values.index.difference(expected)):
        raise ValueError("Unexpected result coordinates")
    dense = values.reindex(expected, fill_value=0).rename("value").reset_index()
    if not np.isfinite(dense.value).all():
        raise ValueError("Nonfinite indicator value")
    dense["sector"] = dense.variable.map(sector_for_variable)
    dense["region"] = "CH"
    dense["model"] = "remind"
    dense["scenario"] = scenario
    dense["unit"] = dense.impact_category.map(UNITS)
    before = frame.groupby(["year", "impact_category"]).value.sum().sort_index()
    after = dense.groupby(["year", "impact_category"]).value.sum().sort_index()
    pd.testing.assert_series_equal(
        before, after, check_exact=False, rtol=1e-12, atol=1e-8
    )
    return dense.rename(columns={"impact_category": "impact"})[PREM1_COLUMNS]


def write_prem1(frame, path):
    if list(frame.columns) != PREM1_COLUMNS:
        raise ValueError("Wrong prem1 column order")
    for column in frame.select_dtypes(include="object"):
        if frame[column].str.contains(r"[,\r\n]", regex=True).any():
            raise ValueError(f"Toby forbids delimiters in text fields: {column}")
    if not np.isfinite(frame.value).all():
        raise ValueError("Nonfinite indicator values")
    frame.to_csv(
        path, index=False, encoding="latin-1", float_format="%.16e", lineterminator="\n"
    )
    restored = pd.read_csv(path, encoding="latin-1", float_precision="round_trip")
    pd.testing.assert_frame_equal(
        frame.reset_index(drop=True), restored, check_exact=True, check_dtype=False
    )


def export(batch, reference, output, selected=None):
    inputs = json.loads((batch / "inputs.json").read_text())
    if not selected:
        progress = json.loads((batch / "progress.json").read_text())
        if progress["status"] != "complete":
            raise ValueError("Full export requires a completed indicator batch")
    sources = json.loads((reference / "sources.json").read_text())
    for name, info in sources["files"].items():
        if sha256(reference / name) != info["sha256"]:
            raise ValueError(f"Reference file changed: {name}")
    registry = read_registry(reference)
    scenario_registry = pd.read_csv(reference / "scenarios.csv")
    if scenario_registry.scn_code.duplicated().any():
        raise ValueError("Ambiguous Toby scenario codes")
    scenario_ids = scenario_registry.set_index("scn_code").scn_id.to_dict()
    output.mkdir(parents=True, exist_ok=True)
    (output / "prem1_by_scenario").mkdir(exist_ok=True)
    (output / "review_by_scenario").mkdir(exist_ok=True)
    frames, checks = [], []
    parameter_map = None
    entries = [
        e for e in inputs["packages"] if not selected or e["scenario"] in selected
    ]
    if selected and set(selected) != {e["scenario"] for e in entries}:
        raise ValueError("Unknown selected scenario")
    for entry in entries:
        scenario = entry["scenario"]
        if scenario not in scenario_ids:
            raise ValueError(f"Scenario absent from Toby: {scenario}")
        folder = batch / "scenarios" / scenario
        stem = "results_" + Path(entry["package"]).stem
        manifest = json.loads((folder / f"{stem}.manifest.json").read_text())
        verification = json.loads(
            (folder / f"{stem}_independent_validation.json").read_text()
        )
        if manifest["status"] != "complete" or not verification["passed"]:
            raise ValueError(f"Unvalidated results: {scenario}")
        if manifest["calculation"]["package_sha256"] != entry["sha256"]:
            raise ValueError("Unexpected source package")
        if sha256(folder / f"{stem}.gzip") != manifest["raw_sha256"]:
            raise ValueError("Raw result checksum mismatch")
        years = manifest["calculation"]["years"]
        if years != inputs["years"] or len(verification["checks"]) != len(years) * len(
            UNITS
        ):
            raise ValueError("Incomplete year/indicator verification")
        frame = pd.read_parquet(folder / f"{stem}_indicators.parquet")
        expected_scenario = manifest["package_build"]["pathway"] + " - " + scenario
        dense = dense_results(frame, manifest["variables"], years, expected_scenario)
        write_prem1(dense, output / "prem1_by_scenario" / f"{scenario}.csv")
        frames.append(dense)
        mapping = (
            dense[["variable", "impact", "location", "unit"]].drop_duplicates().copy()
        )
        mapping["path"] = [
            parameter_path(r.variable, r.impact, r.location)
            for r in mapping.itertuples()
        ]
        mapping = mapping.merge(
            registry.rename(columns={"unit": "registered_unit"}),
            on="path",
            how="left",
            validate="one_to_one",
        )
        if (
            not mapping.loc[mapping.prm_id.notna(), "unit"]
            .eq(mapping.loc[mapping.prm_id.notna(), "registered_unit"])
            .all()
        ):
            raise ValueError("Units differ from Toby's registered units")
        mapping["prm_id"] = mapping.prm_id.astype("Int64")
        mapping["registered"] = mapping.prm_id.notna()
        mapping = (
            mapping.drop(columns="registered_unit")
            .sort_values(["variable", "impact", "location"])
            .reset_index(drop=True)
        )
        if parameter_map is None:
            parameter_map = mapping
        else:
            pd.testing.assert_frame_equal(parameter_map, mapping)
        review = dense.merge(
            mapping,
            on=["variable", "impact", "location", "unit"],
            validate="many_to_one",
        )
        review["scn_id"] = scenario_ids[scenario]
        # Blank IDs are intentional. This review table is not an rpt1 upload file.
        wide = review.pivot(
            index=["path", "unit"], columns="year", values="value"
        ).reset_index()
        wide = wide.merge(mapping[["path", "prm_id"]], on="path", validate="one_to_one")
        wide["scn_id"] = scenario_ids[scenario]
        wide[["prm_id", "scn_id", "path", "unit", *years]].to_csv(
            output / "review_by_scenario" / f"{scenario}.csv",
            index=False,
            encoding="latin-1",
            float_format="%.16e",
        )
        checks.append(
            {
                "scenario": scenario,
                "scn_id": int(scenario_ids[scenario]),
                "rows": len(dense),
                "explicit_zero_rows": int(dense.value.eq(0).sum()),
                "negative_rows": int(dense.value.lt(0).sum()),
                "source_rows": len(frame),
                "totals_preserved": True,
                "csv_roundtrip_exact": True,
            }
        )
    combined = pd.concat(frames, ignore_index=True)
    write_prem1(combined, output / "premise_prem1_all_scenarios.csv")
    parameter_map.to_csv(output / "parameter_mapping.csv", index=False)
    missing = parameter_map.loc[~parameter_map.registered].copy()
    missing.to_csv(output / "parameters_to_register.csv", index=False)
    missing[["variable"]].drop_duplicates().to_csv(
        output / "variables_to_register.csv", index=False
    )
    # Quantify the lost totals if someone were to keep only already registered IDs.
    enriched = combined.merge(
        parameter_map[["variable", "impact", "location", "registered"]],
        on=["variable", "impact", "location"],
        validate="many_to_one",
    )
    group = ["scenario", "year", "impact", "unit"]
    totals = enriched.groupby(group).value.sum().rename("full_total").to_frame()
    totals["unregistered_contribution"] = (
        enriched.loc[~enriched.registered]
        .groupby(group)
        .value.sum()
        .reindex(totals.index, fill_value=0)
    )
    totals["unregistered_percent_of_total"] = (
        100 * totals.unregistered_contribution / totals.full_total.replace(0, np.nan)
    )
    totals.to_csv(output / "registration_coverage.csv")
    missing_rows = enriched.loc[~enriched.registered]
    active_missing = sorted(
        missing_rows.loc[missing_rows.value.ne(0), "variable"].unique()
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch": str(batch),
        "reference": str(reference),
        "preview": bool(selected),
        "scenario_count": len(entries),
        "years": inputs["years"],
        "indicator_count": len(UNITS),
        "rows": len(combined),
        "encoding": "ISO-8859-1",
        "columns": PREM1_COLUMNS,
        "float_format": "%.16e",
        "csv_roundtrip_exact": True,
        "registered_parameter_series": int(parameter_map.registered.sum()),
        "unregistered_parameter_series": len(missing),
        "unregistered_variables": sorted(missing.variable.unique()),
        "unregistered_variables_with_nonzero_results": active_missing,
        "uploaded": False,
        "server_import_tested": False,
        "server_note": "Public documentation confirms the prem1 schema. The public registry lacks the listed parameter series; verify their registration/import handling before upload. No server writes were made.",
        "scenario_checks": checks,
    }
    (output / "README.txt").write_text(
        "SWEET-SURE / ecoinvent 3.12 / Pathways / Toby export\n\n"
        + ("PREVIEW: selected scenarios only.\n\n" if selected else "")
        + "premise_prem1_all_scenarios.csv follows Toby's documented prem1 upload format.\n"
        "Use https://toby.euler.usi.ch/upl.php?mth=nav and the prem1 section.\n"
        "The same results are also provided as one CSV per scenario in prem1_by_scenario/.\n"
        "Choose the combined file OR the per-scenario files, not both.\n"
        "Files are Latin-1, comma separated, with headers and 17 significant digits.\n"
        "Columns: " + ", ".join(PREM1_COLUMNS) + "\n\n"
        "Values are annual, scenario-weighted final-energy impacts for CH.\n"
        "Location identifies inventory contributions: CH, EU wo CH, RoW.\n"
        "EU wo CH follows REMIND EUR + NEU excluding CH.\n"
        "All seven calculated years are retained; 2045 demand/inventories were interpolated during the package build.\n"
        "Explicit zeros and negative impacts are retained. No values have been rounded to old download precision.\n\n"
        f"The public Toby registry matches {int(parameter_map.registered.sum())} of {len(parameter_map)} parameter series.\n"
        f"It lacks {len(missing)} series across {missing.variable.nunique()} final-energy variables.\n"
        "See parameters_to_register.csv, variables_to_register.csv and registration_coverage.csv.\n"
        "Confirm that these series are registered or handled by prem1 before uploading.\n"
        "The public documentation does not specify whether prem1 creates unknown parameters or skips them.\n"
        "No parameter IDs were invented and no results were uploaded.\n"
        "review_by_scenario/ resembles Toby's report downloads but contains blank IDs for missing series;\n"
        "these are review tables, not upload files.\n\n"
        "Source matrices and raw inventory data are not included.\n"
    )
    report["files_sha256"] = {
        str(p.relative_to(output)): sha256(p)
        for p in output.rglob("*")
        if p.is_file()
        and p.name not in {"export_summary.json", "toby_upload_bundle.zip"}
    }
    write_json(output / "export_summary.json", report)
    with zipfile.ZipFile(
        output / "toby_upload_bundle.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for p in sorted(output.rglob("*")):
            if p.is_file() and p.name != "toby_upload_bundle.zip":
                archive.write(p, str(p.relative_to(output)))
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "scenario_count",
                    "rows",
                    "registered_parameter_series",
                    "unregistered_parameter_series",
                    "unregistered_variables",
                )
            },
            indent=2,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        help="Explicitly create a preview from these completed scenarios",
    )
    args = parser.parse_args()
    export(
        args.batch.resolve(),
        args.reference.resolve(),
        args.output_dir.resolve(),
        args.scenarios,
    )


if __name__ == "__main__":
    main()
