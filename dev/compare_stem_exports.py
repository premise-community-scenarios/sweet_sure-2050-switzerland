"""Audit the September STEM export without changing any model inputs or results.

Run with the premise Python environment from any working directory.
"""

import csv
import gzip
import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "dev/STEM raw data"
ARCHIVE = INPUTS / "new data export/OneDrive_2_28-09-2026.zip"
OUT = ROOT / "dev/STEM comparison 2026-09-28"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
YEARS = [2020, 2022, 2025, 2030, 2035, 2040, 2050]
KEYS = ["model", "scenario", "region", "variables", "unit"]


def workbook_cells(data):
    """Read cell values and stored formulas; ignore styles and ZIP metadata."""
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                strings.append("".join(t.text or "" for t in si.findall(".//s:t", NS)))
        rels = {
            r.attrib["Id"]: r.attrib["Target"]
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        }
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS):
            target = rels[sheet.attrib[REL]]
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            cells = {}
            for c in ET.fromstring(z.read(target)).findall(".//s:sheetData/s:row/s:c", NS):
                typ = c.get("t", "n")
                raw = c.findtext("s:v", None, NS)
                value = None
                if typ == "inlineStr":
                    value = "".join(t.text or "" for t in c.findall(".//s:t", NS))
                elif raw is not None:
                    if typ == "s":
                        value = strings[int(raw)]
                    elif typ == "n":
                        value = float(raw)
                    elif typ == "b":
                        value = bool(int(raw))
                    else:
                        value = raw
                f = c.find("s:f", NS)
                formula = None if f is None else (f.text, tuple(sorted(f.attrib.items())))
                if value is not None or formula is not None:
                    cells[c.attrib["r"]] = (typ, value, formula)
            result[sheet.attrib["name"]] = cells
    return result


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def count_differences(old, new):
    comparable = np.isfinite(old) & np.isfinite(new)
    delta = np.abs(new - old)
    exact = comparable & (old != new)
    beyond = comparable & ~np.isclose(old, new, rtol=1e-6, atol=1e-9)
    over_1pct = comparable & (np.abs(old) > 1e-6) & (delta > 1e-6) & (delta > 0.01 * np.abs(old))
    missing = np.isnan(old) != np.isnan(new)
    return {
        "numeric_pairs": int(comparable.sum()),
        "numeric_changed_exact": int(exact.sum()),
        "numeric_changed_beyond_tolerance": int(beyond.sum()),
        "numeric_changed_over_1pct": int(over_1pct.sum()),
        "missing_value_changes": int(missing.sum()),
    }


def reproduce_format(frames):
    """Apply the repository formatter directly to cached raw workbook frames."""
    try:
        from dev.format import format_frame, scenario_names_map
    except ModuleNotFoundError:
        from format import format_frame, scenario_names_map

    formatted = []
    for filename in sorted(frames):
        name = filename.replace("STEM_to_Premise_", "").replace("_2035.xlsx", "")
        for sheet, frame in frames[filename].items():
            if sheet.startswith("SPS"):
                formatted.append(format_frame(frame.copy(deep=True), scenario_names_map[name]))
    result = pd.concat(formatted, ignore_index=True)
    result.columns = result.columns.map(str)
    return result


def compare_tables(old, new, label):
    old = old.set_index(KEYS).sort_index()
    new = new.set_index(KEYS).sort_index()
    assert old.index.is_unique and new.index.is_unique, "Duplicate variable keys"
    common = old.index.intersection(new.index)
    years = [str(y) for y in YEARS]
    a = old.loc[common, years].to_numpy(dtype=float)
    b = new.loc[common, years].to_numpy(dtype=float)
    summary = {"comparison": label, "common_rows": len(common),
               "old_only_rows": len(old.index.difference(new.index)),
               "new_only_rows": len(new.index.difference(old.index)),
               **count_differences(a, b)}
    rows = []
    scen = common.get_level_values("scenario")
    for scenario in sorted(set(scen)):
        mask = scen == scenario
        rows.append({"comparison": label, "scenario": scenario,
                     **count_differences(a[mask], b[mask])})
    return summary, rows


def build_additional_tables():
    """Summarize changes in variables explicitly referenced by premise config."""
    import yaml

    def variable_references(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "variable" and isinstance(child, str):
                    yield child
                yield from variable_references(child)
        elif isinstance(value, list):
            for child in value:
                yield from variable_references(child)

    config = yaml.safe_load((ROOT / "configuration_file/config.yaml").read_text())
    refs = set(variable_references(config))
    old = pd.read_csv(OUT / "scenario_data_existing_reproduced.csv.gz")
    new = pd.read_csv(OUT / "scenario_data_zip_reproduced.csv.gz")
    mapped_old = old.loc[old.variables.isin(refs)]
    mapped_new = new.loc[new.variables.isin(refs)]
    summary, rows = compare_tables(mapped_old, mapped_new, "explicit_config_variables_existing_vs_zip")
    pd.DataFrame(rows).to_csv(OUT / "config_variables_by_scenario.csv", index=False)
    (OUT / "config_variables_summary.json").write_text(json.dumps(summary, indent=2))

    a, b = old.set_index(KEYS).sort_index(), new.set_index(KEYS).sort_index()
    common = a.index.intersection(b.index)
    records = []
    for year in [str(y) for y in YEARS]:
        left = a.loc[common, year].to_numpy(dtype=float)
        right = b.loc[common, year].to_numpy(dtype=float)
        for i in np.flatnonzero(~np.isclose(left, right, rtol=1e-6, atol=1e-9)):
            key = dict(zip(KEYS, common[i]))
            records.append({**key, "year": int(year), "old": left[i], "new": right[i],
                            "delta": right[i]-left[i],
                            "percent_change": 100*(right[i]-left[i])/abs(left[i]) if left[i] else np.nan,
                            "explicit_config_reference": key["variables"] in refs})
    differences = pd.DataFrame(records)
    differences.to_csv(OUT / "processed_differences.csv.gz", index=False)
    examples = differences.loc[
        differences.scenario.str.endswith("bas0")
        & (differences.year == 2050)
        & (differences.variables.isin([
            "Primary Production", "Imports|Electricity", "Electricity generation",
            "Electricity generation|Solar", "Electricity generation|Wind",
            "Electricity generation|Nuclear Fuel", "Imports|Hydrogen",
            "Electricity generation|Solar|Solar PV", "Final energy consumption",
        ]))
    ]
    examples.to_csv(OUT / "baseline_2050_examples.csv", index=False)
    used_examples = differences.loc[
        differences.explicit_config_reference
        & differences.scenario.str.endswith("bas0")
        & (differences.year == 2050)
        & (differences.unit == "PJ/yr.")
        & (differences.old.abs() >= 1)
        & (differences.delta.abs() >= 0.1)
    ].sort_values("percent_change", key=lambda s: s.abs(), ascending=False).head(40)
    used_examples.to_csv(OUT / "mapped_2050_examples.csv", index=False)

    with pd.ExcelWriter(OUT / "STEM_comparison.xlsx", engine="openpyxl") as writer:
        for sheet, source in [
            ("Processed summary", "processed_data_summary.csv"),
            ("By scenario", "processed_data_by_scenario.csv"),
            ("Config variables", "config_variables_by_scenario.csv"),
            ("Workbook sheets", "workbook_sheet_summary.csv"),
            ("By year", "workbook_year_summary.csv"),
            ("Baseline examples", "baseline_2050_examples.csv"),
            ("Mapped examples", "mapped_2050_examples.csv"),
        ]:
            pd.read_csv(OUT / source).to_excel(writer, sheet_name=sheet, index=False)
        for sheet in writer.book:
            sheet.freeze_panes = "C2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                width = min(65, max(12, max(len(str(c.value or "")) for c in column) + 2))
                sheet.column_dimensions[column[0].column_letter].width = width
    print("CONFIG SUMMARY", json.dumps(summary), flush=True)
    print(examples[["scenario", "variables", "unit", "old", "new", "percent_change"]].to_string(index=False), flush=True)


def main():
    OUT.mkdir(exist_ok=True)
    manifest = {"created": datetime.now().astimezone().isoformat(),
                "archive": str(ARCHIVE),
                "archive_sha256": hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
                "rtol": 1e-6, "atol": 1e-9, "files": []}
    summaries, year_rows, frames = [], [], {"old": {}, "new": {}}
    detail_fields = ["file", "sheet", "cell", "variable", "unit", "year", "old", "new", "delta", "percent_change", "beyond_tolerance"]
    with zipfile.ZipFile(ARCHIVE) as archive, gzip.open(OUT / "cell_differences.csv.gz", "wt", newline="") as detail:
        writer = csv.DictWriter(detail, fieldnames=detail_fields)
        writer.writeheader()
        entries = {Path(n).name: n for n in archive.namelist() if n.endswith(".xlsx") and not n.startswith("__MACOSX/")}
        assert len(entries) == sum(n.endswith(".xlsx") and not n.startswith("__MACOSX/") for n in archive.namelist()), "Duplicate basenames"
        local = {p.name for p in INPUTS.glob("*.xlsx")}
        manifest["only_in_archive"] = sorted(set(entries) - local)
        manifest["only_local"] = sorted(local - set(entries))
        for i, name in enumerate(sorted(local & set(entries)), 1):
            old_bytes, new_bytes = (INPUTS / name).read_bytes(), archive.read(entries[name])
            old_sheets, new_sheets = workbook_cells(old_bytes), workbook_cells(new_bytes)
            manifest["files"].append({"name": name,
                "old_sha256": hashlib.sha256(old_bytes).hexdigest(),
                "new_sha256": hashlib.sha256(new_bytes).hexdigest(),
                "old_mtime": datetime.fromtimestamp((INPUTS / name).stat().st_mtime).astimezone().isoformat(),
                "zip_timestamp": archive.getinfo(entries[name]).date_time,
                "old_only_sheets": sorted(set(old_sheets)-set(new_sheets)),
                "new_only_sheets": sorted(set(new_sheets)-set(old_sheets))})
            for sheet in sorted(set(old_sheets) | set(new_sheets)):
                old, new = old_sheets.get(sheet, {}), new_sheets.get(sheet, {})
                stats, years = Counter(), {}
                for cell in sorted(set(old) | set(new)):
                    ot, a, of = old.get(cell, (None, None, None))
                    nt, b, nf = new.get(cell, (None, None, None))
                    stats["formula_changed"] += of != nf
                    col, row = re.fullmatch(r"([A-Z]+)(\d+)", cell).groups()
                    year = new.get(col + "1", (None, None, None))[1] if sheet.startswith("SPS") else None
                    year = int(year) if year in YEARS else None
                    if number(a) and number(b):
                        stats["numeric_pairs"] += 1
                        if year:
                            years.setdefault(year, Counter())["numeric_pairs"] += 1
                        if a != b:
                            beyond = not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
                            over = abs(a) > 1e-6 and abs(b-a) > max(1e-6, 0.01*abs(a))
                            counts = {"numeric_changed_exact": 1, "numeric_changed_beyond_tolerance": int(beyond), "numeric_changed_over_1pct": int(over)}
                            stats.update(counts)
                            if year:
                                years[year].update(counts)
                            writer.writerow({"file": name, "sheet": sheet, "cell": cell,
                                "variable": new.get("D"+row, (None, None, None))[1] if sheet.startswith("SPS") else "",
                                "unit": new.get("E"+row, (None, None, None))[1] if sheet.startswith("SPS") else "",
                                "year": year, "old": a, "new": b, "delta": b-a,
                                "percent_change": 100*(b-a)/abs(a) if a else "",
                                "beyond_tolerance": beyond})
                    elif a != b:
                        stats["numeric_type_or_missing_changed" if number(a) or number(b) else "text_or_other_changed"] += 1
                summaries.append({"file": name, "sheet": sheet, **stats})
                year_rows.extend({"file": name, "sheet": sheet, "year": year, **s} for year, s in sorted(years.items()))
            for version, data, sheets in [("old", old_bytes, old_sheets), ("new", new_bytes, new_sheets)]:
                with pd.ExcelFile(io.BytesIO(data), engine_kwargs={"keep_links": False}) as xls:
                    frames[version][name] = {s: pd.read_excel(xls, sheet_name=s) for s in sheets if s.startswith("SPS")}
            print(f"Compared {i}/{len(entries)}: {name}", flush=True)

    pd.DataFrame(summaries).fillna(0).to_csv(OUT / "workbook_sheet_summary.csv", index=False)
    pd.DataFrame(year_rows).fillna(0).to_csv(OUT / "workbook_year_summary.csv", index=False)
    (OUT / "input_manifest.json").write_text(json.dumps(manifest, indent=2))
    print("Reproducing format.py transformations in memory", flush=True)
    old_processed, new_processed = reproduce_format(frames["old"]), reproduce_format(frames["new"])
    old_text, new_text = old_processed.to_csv(index=False), new_processed.to_csv(index=False)
    for version, content in [("existing", old_text), ("zip", new_text)]:
        with gzip.open(OUT / f"scenario_data_{version}_reproduced.csv.gz", "wt", newline="") as f:
            f.write(content)
    current_bytes = (ROOT / "scenario_data/scenario_data.csv").read_bytes()
    identity = {
        "current_csv_sha256": hashlib.sha256(current_bytes).hexdigest(),
        "reproduced_existing_csv_sha256": hashlib.sha256(old_text.encode()).hexdigest(),
        "reproduced_zip_csv_sha256": hashlib.sha256(new_text.encode()).hexdigest(),
        "current_csv_equals_existing_byte_for_byte": current_bytes == old_text.encode(),
    }
    (OUT / "csv_identity.json").write_text(json.dumps(identity, indent=2))
    # Apply the same CSV parsing to all three tables. This avoids counting
    # numeric versus string metadata (e.g. unit=0), or float serialization,
    # as differences between the saved CSV and freshly computed frames.
    old_processed = pd.read_csv(io.StringIO(old_text))
    new_processed = pd.read_csv(io.StringIO(new_text))
    current = pd.read_csv(ROOT / "scenario_data/scenario_data.csv")
    all_summaries, all_rows = [], []
    for old, new, label in [(current, old_processed, "current_csv_vs_existing_workbooks"),
                            (current, new_processed, "current_csv_vs_zip_workbooks"),
                            (old_processed, new_processed, "existing_vs_zip_after_formatting")]:
        summary, rows = compare_tables(old, new, label)
        all_summaries.append(summary)
        all_rows.extend(rows)
        print(json.dumps(summary), flush=True)
    pd.DataFrame(all_summaries).to_csv(OUT / "processed_data_summary.csv", index=False)
    pd.DataFrame(all_rows).to_csv(OUT / "processed_data_by_scenario.csv", index=False)
    build_additional_tables()
    print("Audit written to", OUT, flush=True)


if __name__ == "__main__":
    main()
