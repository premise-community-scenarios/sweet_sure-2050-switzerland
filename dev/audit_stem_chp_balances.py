"""Read-only CHP source audit, including Excel's cached external STEM tables.

The 110% and 1% cutoffs are review thresholds, not efficiency corrections.
External caches are evidence saved in each export, not live STEM workbooks.
No formula is executed and no external workbook is opened or refreshed.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from zipfile import ZipFile

import pandas as pd
from openpyxl.formula.translate import Translator

from compare_stem_exports import workbook_cells
from format import scenario_names_map
from stem_source_cache import external_caches, raw_series

ROOT = Path(__file__).resolve().parents[1]
TECHNOLOGIES = {"Wood": "CHPWOD", "Gas": "CHPNGA", "Oil": "CHPOIL"}
FUELS = {"Wood": "EWOD", "Gas": "ENGA", "Oil": "ELFO"}
REFERENCE = re.compile(r"'\[(\d+)\]([^']+)'!\$?([A-Z]+)\$?(\d+)")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def close(left, right):
    return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-12)


def variables(fuel):
    plant = f"CHP {fuel} (for District Heating)"
    heat = (
        f"heat generation from CHPs and heat plants|District heating|CHP|{fuel}|{plant}"
    )
    return {
        "electricity": f"Electricity generation|{fuel}|{plant}",
        "heat": heat[0].upper() + heat[1:],
        "fuel_e": f"Fuel input for electricity generation|{fuel}|{plant}",
        "fuel_h": "Fuel input for " + heat,
        "efficiency": f"Efficiency of power plants (electric)|{fuel}|{plant}",
    }


def balance_metrics(electricity, heat, fuel_e, fuel_h, efficiency):
    """Check allocated fuel once, without comparing each output to all the fuel."""
    total = fuel_e + fuel_h
    combined = (electricity + heat) / total if total > 0 else None
    electric = electricity / total if total > 0 else None
    expected_e = (
        total * electricity / (electricity + heat) if electricity + heat > 0 else 0
    )
    return {
        "total_fuel_PJ": total,
        "combined_output_over_input": combined,
        "derived_electric_efficiency": electric,
        "energy_allocation_matches": close(fuel_e, expected_e),
        "reported_efficiency_matches": (
            close(efficiency, electric)
            if electric is not None and isinstance(efficiency, (int, float))
            else None
        ),
        "output_exceeds_input_10pct": combined is not None and combined > 1.1,
        "combined_efficiency_below_1pct": combined is not None and combined < 0.01,
        "output_without_fuel": total <= 0 and electricity + heat > 0,
    }


def formula_text(cells, address, masters):
    formula = cells[address][2]
    if formula is None:
        raise ValueError(f"Expected a source formula at {address}")
    text, attrs = formula
    attrs = dict(attrs)
    if text is not None:
        return "=" + text
    master_address, master_text = masters[attrs["si"]]
    return Translator("=" + master_text, origin=master_address).translate_formula(
        address
    )


def one_reference(formula, caches):
    refs = set(REFERENCE.findall(formula))
    if len(refs) != 1:
        raise ValueError(f"Expected one external source: {formula}")
    link, sheet, col, row = refs.pop()
    cache = caches[int(link)]
    return int(link), sheet, col + row, cache["sheets"][sheet][col + row]


def audit_book(payload, filename, edition, csv_data):
    scenario = scenario_names_map[
        Path(filename).stem.removeprefix("STEM_to_Premise_").removesuffix("_2035")
    ]
    sheets = workbook_cells(payload)
    (sheet_name,) = [name for name in sheets if name.startswith("SPS")]
    cells = sheets[sheet_name]
    caches = external_caches(payload)
    rows = {}
    for address, (_, value, _) in cells.items():
        if re.fullmatch(r"D\d+", address) and isinstance(value, str):
            key = value.strip()
            if key in rows:
                raise ValueError(f"Duplicate variable: {key}")
            rows[key] = address[1:]
    years = {int(cells[c + "1"][1]): c for c in "FGHIJKL"}
    masters = {}
    for address, (_, _, formula) in cells.items():
        if formula and formula[0] is not None and dict(formula[1]).get("t") == "shared":
            masters[dict(formula[1])["si"]] = (address, formula[0])
    balances, traces = [], []
    for fuel, technology in TECHNOLOGIES.items():
        keys = variables(fuel)
        for year, col in years.items():
            values = {key: cells[col + rows[var]][1] for key, var in keys.items()}
            formulas = {
                key: formula_text(cells, col + rows[var], masters)
                for key, var in keys.items()
            }
            for key in ("electricity", "heat", "fuel_e", "fuel_h"):
                if cells["E" + rows[keys[key]]][1] != "PJ/yr.":
                    raise ValueError(f"Unexpected energy unit for {keys[key]}")
            record = {
                "edition": edition,
                "workbook": filename,
                "sheet": sheet_name,
                "scenario": scenario,
                "year": year,
                "fuel": fuel,
                **{
                    key + "_PJ": values[key]
                    for key in ("electricity", "heat", "fuel_e", "fuel_h")
                },
                "reported_electric_efficiency": values["efficiency"],
                **balance_metrics(
                    **{
                        "electricity": values["electricity"],
                        "heat": values["heat"],
                        "fuel_e": values["fuel_e"],
                        "fuel_h": values["fuel_h"],
                        "efficiency": values["efficiency"],
                    }
                ),
            }
            refs = {
                key: one_reference(formulas[key], caches)
                for key in ("electricity", "heat", "fuel_e", "fuel_h")
            }
            if refs["fuel_e"] != refs["fuel_h"]:
                raise ValueError(
                    "The two allocated fuel portions have different sources"
                )
            external = {key: max(float(ref[-1]), 0) for key, ref in refs.items()}
            record["export_matches_external_cache"] = (
                close(values["electricity"], external["electricity"])
                and close(values["heat"], external["heat"])
                and close(record["total_fuel_PJ"], external["fuel_e"])
            )
            for key, kind, commodity in (
                ("electricity", "VAR_fout", "ELC35"),
                ("heat", "VAR_fout", "LTH"),
                ("total_fuel", "VAR_fin", FUELS[fuel]),
            ):
                ref_key = "fuel_e" if key == "total_fuel" else key
                link, source_sheet, source_cell, source_value = refs[ref_key]
                cache = caches[link]
                raw_value, raw_cell, blank = raw_series(
                    cache, technology, kind, commodity
                )[year]
                record["raw_" + key + "_PJ"] = raw_value
                record[key + "_matches_raw_table"] = close(
                    record[key + "_PJ"], max(raw_value, 0)
                )
                traces.append(
                    {
                        "edition": edition,
                        "scenario": scenario,
                        "year": year,
                        "fuel": fuel,
                        "quantity": key,
                        "workbook": filename,
                        "export_sheet": sheet_name,
                        "export_cell": col + rows[keys[ref_key]],
                        "export_formula": formulas[ref_key],
                        "external_workbook": cache["target"],
                        "external_link": link,
                        "summary_sheet": source_sheet,
                        "summary_cell": source_cell,
                        "summary_value": source_value,
                        "raw_sheet": "OUT_Electricity_CHP",
                        "raw_cell": raw_cell,
                        "raw_technology": technology,
                        "raw_kind": kind,
                        "raw_commodity": commodity,
                        "raw_value": raw_value,
                        "raw_numeric_cell_blank": blank,
                        "summary_matches_raw": close(float(source_value), raw_value),
                    }
                )
            record["raw_combined_output_over_input"] = (
                (record["raw_electricity_PJ"] + record["raw_heat_PJ"])
                / record["raw_total_fuel_PJ"]
                if record["raw_total_fuel_PJ"] > 0
                else None
            )
            if fuel == "Wood":
                for label, coproduct_technology, commodity, quantity in (
                    ("biogas_electricity", "SUP_WODBIO", "ELC35", "electricity"),
                    ("biogas_heat", "SUP_WODBIO", "LTH", "heat"),
                    ("biodiesel_heat", "CT_CWODBDS", "LTH", "heat"),
                ):
                    link = refs[quantity][0]
                    cache = caches[link]
                    value, address, blank = raw_series(
                        cache,
                        coproduct_technology,
                        "VAR_fout",
                        commodity,
                        allow_absent_flow=True,
                    )[year]
                    record[label + "_PJ"] = value
                    record[label + "_source"] = (
                        f"[{link}]OUT_Electricity_CHP!{address}"
                        if address
                        else f"[{link}]OUT_Electricity_CHP: flow absent (zero in sum)"
                    )
                record["electricity_aggregation_reconciles"] = close(
                    values["electricity"],
                    record["raw_electricity_PJ"] + record["biogas_electricity_PJ"],
                )
                record["heat_aggregation_reconciles"] = close(
                    values["heat"],
                    record["raw_heat_PJ"]
                    + record["biogas_heat_PJ"]
                    + record["biodiesel_heat_PJ"],
                )
                biodiesel_heat = (
                    "Heat from domestic biodiesel production|Wood|Fischer Tropsch"
                )
                biogas_electricity = (
                    "Electricity from domestic biogas production|Wood|Gasification"
                )
                record["biodiesel_heat_export_PJ"] = cells[col + rows[biodiesel_heat]][
                    1
                ]
                record["biogas_electricity_export_PJ"] = cells[
                    col + rows[biogas_electricity]
                ][1]
                record["biodiesel_heat_also_reported_separately"] = close(
                    record["biodiesel_heat_PJ"], record["biodiesel_heat_export_PJ"]
                )
                record["biogas_electricity_also_reported_separately"] = close(
                    record["biogas_electricity_PJ"],
                    record["biogas_electricity_export_PJ"],
                )
            record["efficiency_formula"] = formulas["efficiency"]
            record["fuel_heat_formula"] = formulas["fuel_h"]
            if edition == "Latest":
                for key, var in keys.items():
                    actual = float(csv_data.loc[(scenario, var), str(year)])
                    expected = values[key]
                    if key == "efficiency":
                        series = pd.Series(
                            [cells[c + rows[var]][1] for c in years.values()]
                        )
                        expected = (
                            pd.to_numeric(series, errors="coerce")
                            .replace(0, float("nan"))
                            .bfill()
                            .ffill()
                            .fillna(0)
                            .iloc[list(years).index(year)]
                        )
                    record[key + "_csv_matches_formatter"] = close(actual, expected)
            balances.append(record)
    return balances, traces


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    before = {
        str(path.relative_to(ROOT)): digest(path)
        for path in (
            args.archive,
            args.scenario_data,
            ROOT / "configuration_file/config.yaml",
            ROOT / "inventories/lci-sweet_sure.xlsx",
        )
    }
    csv_data = pd.read_csv(args.scenario_data, float_precision="round_trip").set_index(
        ["scenario", "variables"]
    )
    if not csv_data.index.is_unique:
        raise ValueError("Scenario/variable keys are not unique")
    balances, traces, books = [], [], []
    with ZipFile(args.archive) as archive:
        members = sorted(
            n
            for n in archive.namelist()
            if n.endswith(".xlsx") and not Path(n).name.startswith("~")
        )
        for number, member in enumerate(members, 1):
            for edition, payload in (
                ("Latest", archive.read(member)),
                ("Previous", (args.previous / Path(member).name).read_bytes()),
            ):
                b, t = audit_book(payload, Path(member).name, edition, csv_data)
                balances.extend(b)
                traces.extend(t)
                books.append(
                    {
                        "edition": edition,
                        "workbook": member,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            if number % 10 == 0:
                print(
                    f"Audited {number}/{len(members)} Latest/Previous workbook pairs",
                    flush=True,
                )
    b, t = pd.DataFrame(balances), pd.DataFrame(traces)
    b.to_csv(args.output / "chp_energy_balances.csv", index=False)
    b[b.fuel.eq("Wood")].to_csv(
        args.output / "wood_output_composition.csv", index=False
    )
    t.to_csv(args.output / "source_cell_trace.csv", index=False)
    b[
        b.output_exceeds_input_10pct
        | b.combined_efficiency_below_1pct
        | b.output_without_fuel
    ].to_csv(args.output / "energy_balance_flags.csv", index=False)
    t[~t.summary_matches_raw].to_csv(
        args.output / "source_summary_discrepancies.csv", index=False
    )
    latest = b[b.edition.eq("Latest")].set_index(["scenario", "year", "fuel"])
    old = b[b.edition.eq("Previous")].set_index(["scenario", "year", "fuel"])
    changes = latest[
        ["electricity_PJ", "heat_PJ", "total_fuel_PJ", "reported_electric_efficiency"]
    ].join(
        old[
            [
                "electricity_PJ",
                "heat_PJ",
                "total_fuel_PJ",
                "reported_electric_efficiency",
            ]
        ],
        lsuffix="_latest",
        rsuffix="_previous",
    )
    for key in (
        "electricity_PJ",
        "heat_PJ",
        "total_fuel_PJ",
        "reported_electric_efficiency",
    ):
        changes[key + "_changed"] = [
            not (close(x, y) or (pd.isna(x) and pd.isna(y)))
            for x, y in zip(
                pd.to_numeric(changes[key + "_latest"], errors="coerce"),
                pd.to_numeric(changes[key + "_previous"], errors="coerce"),
            )
        ]
    changes.to_csv(args.output / "latest_vs_previous.csv")
    summary = {
        "scope": "Wood/Gas/Oil district-heating CHP; original source years only; no input corrections",
        "thresholds": {
            "combined_output_over_input_high": 1.1,
            "combined_output_over_input_low": 0.01,
        },
        "input_hashes": before,
        "workbooks": books,
        "counts": [],
        "csv_mismatches": {
            c: int((latest[c] == False).sum())
            for c in latest
            if c.endswith("_csv_matches_formatter")
        },
        "wood_composition_mismatches": {
            edition: {
                column: int((group[column] == False).sum())
                for column in (
                    "electricity_aggregation_reconciles",
                    "heat_aggregation_reconciles",
                    "biodiesel_heat_also_reported_separately",
                    "biogas_electricity_also_reported_separately",
                )
            }
            for edition, group in b[b.fuel.eq("Wood")].groupby("edition")
        },
    }
    for (edition, fuel), group in b.groupby(["edition", "fuel"]):
        summary["counts"].append(
            {
                "edition": edition,
                "fuel": fuel,
                "cases": len(group),
                **{
                    col: int(group[col].sum())
                    for col in (
                        "output_exceeds_input_10pct",
                        "combined_efficiency_below_1pct",
                        "output_without_fuel",
                    )
                },
                **{
                    col + "_failures": int((group[col] == False).sum())
                    for col in (
                        "energy_allocation_matches",
                        "reported_efficiency_matches",
                        "export_matches_external_cache",
                        "electricity_matches_raw_table",
                        "heat_matches_raw_table",
                        "total_fuel_matches_raw_table",
                    )
                },
            }
        )
    after = {path: digest(ROOT / path) for path in before}
    summary["input_files_unchanged"] = before == after
    if not summary["input_files_unchanged"]:
        raise RuntimeError("An input changed during the audit")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in ("workbooks", "input_hashes")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        default=ROOT / "dev/STEM raw data/new data export/OneDrive_2_28-09-2026.zip",
    )
    parser.add_argument("--previous", type=Path, default=ROOT / "dev/STEM raw data")
    parser.add_argument(
        "--scenario-data", type=Path, default=ROOT / "scenario_data/scenario_data.csv"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/stem_chp_investigation_20260930"
    )
    run(parser.parse_args())
