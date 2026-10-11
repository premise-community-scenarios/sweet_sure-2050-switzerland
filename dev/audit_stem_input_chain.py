"""Independently reconcile the original STEM workbook archive with build inputs."""

import argparse
from collections import Counter
import io
import json
from pathlib import Path
import re
import zipfile

import numpy as np
import pandas as pd
import yaml

from workflow_audit_core import digest


def scenario_code(filename):
    match = re.fullmatch(
        r"STEM_to_Premise_(SPS[1-4])(?:_(FIN|SOC|NUC)_(Low|Mid|High))?_2035.xlsx",
        Path(filename).name,
    )
    if match is None:
        raise ValueError(filename)
    family, shock, level = match.groups()
    return family + (
        "_bas0"
        if shock is None
        else "_" + shock.lower() + str(["Low", "Mid", "High"].index(level) + 1)
    )


def source_frame(payload):
    excel = pd.ExcelFile(io.BytesIO(payload))
    sheets = [s for s in excel.sheet_names if s.startswith("SPS")]
    assert len(sheets) == 1
    raw = pd.read_excel(excel, sheet_name=sheets[0])
    raw = raw.rename(
        columns={"Variable": "variables", "Unit": "unit", "Region": "region"}
    )
    raw["variables"] = raw.variables.str.strip()
    assert raw.variables.is_unique
    years = sorted(c for c in raw if isinstance(c, int))
    data = raw.set_index("variables")
    data[years] = data[years].apply(pd.to_numeric, errors="coerce")
    eff = data.index.str.startswith("Efficiency")
    data.loc[eff, years] = (
        data.loc[eff, years].replace(0, np.nan).bfill(axis=1).ffill(axis=1)
    )
    data[years] = data[years].fillna(0)
    return data, years, sheets[0]


def expected_format(data, years):
    result = data[["region", "unit", *years]].copy()
    result["unit"] = result.unit.replace(["-", "- ", " ", "n.a."], "0").fillna("0")
    result.loc["Imports|Electricity", years] = np.maximum(
        data.loc["Imports|Electricity", years].to_numpy(dtype=float)
        - data.loc["Exports|Electricity", years].to_numpy(dtype=float),
        0,
    )
    for renewable in ["Renewable", "Non Renewable"]:
        for suffix in ["", " CCS"]:
            prefix = f"Electricity generation|Wastes|{renewable}|"
            electric = prefix + "Wastes Incineration (electric only)" + suffix
            chp = prefix + "CHP Wastes (for District Heating)" + suffix
            result.loc[chp, years] = data.loc[chp, years].to_numpy(
                dtype=float
            ) + data.loc[electric, years].to_numpy(dtype=float)
            result = result.drop(index=electric)
    for reactor, share in [("Pressure water", 0.6), ("Boiling water", 0.4)]:
        key = "Electricity generation|Nuclear fuel|" + reactor
        result.loc[key] = data.loc[
            "Electricity generation|Nuclear Fuel", result.columns
        ]
        result.loc[key, years] = (
            data.loc["Electricity generation|Nuclear Fuel", years].to_numpy(dtype=float)
            * share
        )
    for key in [
        "Production|Electricity|Medium to high",
        "Production|Electricity|Low to medium",
    ]:
        result.loc[key] = ["CH", "TWh", *[1.0] * len(years)]
    return result


def audit(batch, archive, output):
    output.mkdir(parents=True, exist_ok=True)
    batch_inputs = json.loads((batch / "inputs.json").read_text())
    build = Path(batch_inputs["build"])
    frozen = build / "inputs"
    csv_path = frozen / "scenario_data/scenario_data.csv"
    config_path = frozen / "configuration_file/config.yaml"
    data = pd.read_csv(csv_path, float_precision="round_trip")
    config = yaml.safe_load(config_path.read_text())
    summaries, discrepancies, chains, identities = [], [], [], []
    source_by_scenario = {}
    with zipfile.ZipFile(archive) as raw_zip:
        for member in sorted(
            n
            for n in raw_zip.namelist()
            if n.endswith(".xlsx") and not Path(n).name.startswith("~")
        ):
            sc = scenario_code(member)
            raw, years, sheet = source_frame(raw_zip.read(member))
            formatted = expected_format(raw, years)
            source_by_scenario[sc] = (raw, years)
            actual = data[data.scenario.eq(sc)].set_index("variables")
            assert actual.index.is_unique and set(actual.index) == set(formatted.index)
            assert set(actual.model) == {"STEM"}
            actual = actual.loc[formatted.index]
            assert (
                actual.unit.eq(formatted.unit).all()
                and actual.region.eq(formatted.region).all()
            )
            a = actual[[str(y) for y in years]].to_numpy(float)
            e = formatted[years].to_numpy(float)
            changed = ~np.isclose(a, e, rtol=1e-12, atol=1e-12)
            for row, col in zip(*np.where(changed)):
                discrepancies.append(
                    {
                        "scenario": sc,
                        "variable": formatted.index[row],
                        "year": years[col],
                        "expected": e[row, col],
                        "actual": a[row, col],
                    }
                )
            summaries.append(
                {
                    "scenario": sc,
                    "workbook": member,
                    "sheet": sheet,
                    "rows": len(formatted),
                    "numeric_cells": a.size,
                    "discrepancies": int(changed.sum()),
                }
            )
            print(
                f"STEM workbook {len(summaries)}/40: {sc}; mismatches={changed.sum()}",
                flush=True,
            )
    for entry in batch_inputs["packages"]:
        sc = entry["scenario"]
        package = Path(entry["package"])
        assert digest(package) == entry["sha256"]
        manifest = json.loads(
            next((batch / "scenarios" / sc).glob("*.manifest.json")).read_text()
        )
        for resource in manifest["package_build"]["resources"].values():
            assert digest(resource["path"]) == resource["sha256"]
        assert manifest["package_build"]["source_version"] == "3.12"
        with zipfile.ZipFile(package) as z:
            desc = json.loads(z.read("datapackage.json"))
            resources = {r["name"]: r["path"] for r in desc["resources"]}
            package_data = pd.read_csv(
                z.open(resources["scenario_data"]), float_precision="round_trip"
            )
            mapping = yaml.safe_load(z.read(resources["mapping"]))
            assert all(
                sum(n.endswith(f"/{year}/A_matrix.csv") for n in z.namelist()) == 1
                for year in batch_inputs["years"]
            )
        fe = package_data[package_data.variables.str.startswith("EXT - 0 - FE_")]
        assert set(fe.region) == {"CH"} and set(fe.unit) == {"PJ/yr."}
        raw, years = source_by_scenario[sc]
        expected_aliases = {}
        used = Counter()
        for key, settings in config["production pathways"].items():
            alias = settings["ecoinvent alias"]
            identity = alias["name"], alias["reference product"]
            expected_aliases[key] = alias["name"] + (
                f"_{key}" if used[identity] and not alias.get("new dataset") else ""
            )
            if not alias.get("new dataset"):
                used[identity] += 1
        demands = pd.read_csv(
            next((batch / "scenarios" / sc).glob("*_demands.csv")),
            float_precision="round_trip",
        )
        assert len(demands) == len(fe) == 101 * 7
        for row in fe.to_dict("records"):
            key = row["variables"].removeprefix("EXT - 0 - ")
            settings = config["production pathways"][key]
            stem_variable = settings["production volume"]["variable"]
            expected = float(
                np.interp(
                    row["year"], years, raw.loc[stem_variable, years].to_numpy(float)
                )
            )
            check = demands[
                (demands.variable == row["variables"]) & (demands.year == row["year"])
            ]
            assert len(check) == 1
            check = check.iloc[0]
            assert np.isclose(row["value"], expected, rtol=1e-12, atol=1e-12)
            assert np.isclose(
                check.scenario_demand, max(0, expected), rtol=1e-12, atol=1e-12
            )
            assert expected >= -1e-12
            expected_factor = {"megajoule": 1e9, "kilowatt hour": 1e9 / 3.6}[check.unit]
            assert np.isclose(
                check.conversion_factor, expected_factor, rtol=1e-14, atol=0
            )
            alias = mapping[row["variables"]]["dataset"]
            assert len(alias) == 1
            assert alias[0]["name"] == expected_aliases[key]
            assert (
                alias[0]["reference product"]
                == settings["ecoinvent alias"]["reference product"]
            )
            assert (
                alias[0]["name"],
                alias[0]["reference product"],
                alias[0]["unit"],
            ) == (check["name"], check.reference_product, check.unit)
            chains.append(
                {
                    "scenario": sc,
                    "year": row["year"],
                    "variable": row["variables"],
                    "stem_variable": stem_variable,
                    "raw_interpolated_PJ": expected,
                    "package_PJ": row["value"],
                    "calculation_PJ": check.scenario_demand,
                    "conversion_factor": check.conversion_factor,
                    "supplier_name": check["name"],
                    "supplier_product": check.reference_product,
                    "supplier_unit": check.unit,
                    "supplier_location": check.location,
                    "interpolated": row["year"] not in years,
                }
            )
        print(f"Package chain: {sc}; 707 demands checked", flush=True)
    pd.DataFrame(summaries).to_csv(output / "stem_workbook_checks.csv", index=False)
    pd.DataFrame(
        discrepancies, columns=["scenario", "variable", "year", "expected", "actual"]
    ).to_csv(output / "stem_format_discrepancies.csv", index=False)
    pd.DataFrame(chains).to_csv(
        output / "all_final_energy_input_chains.csv", index=False
    )
    summary = {
        "raw_archive": str(archive),
        "raw_archive_sha256": digest(archive),
        "frozen_scenario_data_sha256": digest(csv_path),
        "frozen_config_sha256": digest(config_path),
        "workbooks": len(summaries),
        "raw_numeric_cells": sum(r["numeric_cells"] for r in summaries),
        "format_discrepancies": len(discrepancies),
        "final_energy_chains_checked": len(chains),
        "package_demand_mapping_unit_checks_passed": True,
        "script_sha256": digest(Path(__file__)),
    }
    (output / "input_chain_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path("dev/STEM raw data/new data export/OneDrive_2_28-09-2026.zip"),
    )
    args = parser.parse_args()
    audit(args.batch, args.archive, args.batch / "workflow_audit")
