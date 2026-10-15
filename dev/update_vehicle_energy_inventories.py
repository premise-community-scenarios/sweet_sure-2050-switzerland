#!/usr/bin/env python3
"""Rebuild scoped per-MJ road-transport blocks from the installed vehicle LCIs.

Only targeted XML rows are replaced: untouched sheets, formulas and cached
formula values survive. --write updates both source workbooks after validation.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from types import SimpleNamespace

import openpyxl
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ET.register_namespace("", NS)
FILES = [
    "lci-pass_cars.xlsx",
    "lci-trucks.xlsx",
    "lci-buses.xlsx",
    "lci-two_wheelers.xlsx",
    "lci-rail-freight.xlsx",
]


def read_workbook(path):
    result = []
    for sheet in openpyxl.load_workbook(path, read_only=True, data_only=True):
        current = header = None
        for row_number, row in enumerate(sheet.values, 1):
            if row[0] == "Activity":
                if current:
                    current["end"] = row_number - 1
                current = {
                    "name": row[1],
                    "exchanges": [],
                    "sheet": sheet.title,
                    "start": row_number,
                }
                result.append(current)
                header = None
            elif current and row[0] == "name" and row[1] == "amount":
                header = row
            elif current and row[0] and header:
                exc = {k: v for k, v in zip(header, row) if k and v is not None}
                if exc.get("type") in ("production", "technosphere", "biosphere"):
                    if "reference product" in exc:
                        exc["product"] = exc.pop("reference product")
                    current["exchanges"].append(exc)
            elif current and row[0] and row[0] != "Exchanges":
                current[row[0]] = row[1]
        if current:
            current["end"] = sheet.max_row
    return result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vehicle_sources(folder):
    from premise.inventory_imports import migrate_import_db

    datasets = []
    for filename in FILES:
        data = read_workbook(folder / filename)
        if filename in (
            "lci-buses.xlsx",
            "lci-two_wheelers.xlsx",
            "lci-rail-freight.xlsx",
        ):
            for ds in data:
                for e in ds["exchanges"]:
                    if "product" in e:
                        e["reference product"] = e["product"]
            # These source workbooks use ecoinvent 3.7 supplier identifiers;
            # destination workbooks declare 3.10. Apply the official migration.
            migrate_import_db(
                SimpleNamespace(data=data),
                "3.9" if filename == "lci-rail-freight.xlsx" else "3.7",
                "3.10",
            )
            for ds in data:
                for e in ds["exchanges"]:
                    if "reference product" in e:
                        e["product"] = e.pop("reference product")
        datasets.extend(data)
    # Match the importer's local-product completion by exact name/location/unit.
    # The rail workbook has inconsistent punctuation in locomotive products.
    local = {}
    for ds in datasets:
        local.setdefault((ds["name"], ds["location"], ds["unit"]), []).append(ds)
    for ds in datasets:
        for e in ds["exchanges"]:
            if e["type"] != "technosphere":
                continue
            matches = local.get((e["name"], e.get("location"), e["unit"]), [])
            if len(matches) == 1:
                e["product"] = matches[0]["reference product"]
    return datasets


def block_rows(dataset):
    rows = [["Activity", dataset["name"]]]
    for key, value in dataset.items():
        if (
            key not in ("name", "exchanges", "sheet", "start", "end", "append")
            and value is not None
        ):
            rows.append([key, value])
    headers = [
        "name",
        "amount",
        "location",
        "unit",
        "categories",
        "type",
        "reference product",
        "comment",
        "uncertainty type",
    ]
    rows.extend([["Exchanges"], headers])
    for e in dataset["exchanges"]:
        rows.append(
            [
                e.get("product") if key == "reference product" else e.get(key)
                for key in headers
            ]
        )
    rows.append([])
    return rows


def xml_row(values, index):
    row = ET.Element(f"{{{NS}}}row", {"r": str(index)})
    for col, value in enumerate(values, 1):
        if value is None:
            continue
        cell = ET.SubElement(
            row, f"{{{NS}}}c", {"r": f"{get_column_letter(col)}{index}"}
        )
        if isinstance(value, (float, int)):
            ET.SubElement(cell, f"{{{NS}}}v").text = str(value)
        else:
            cell.set("t", "inlineStr")
            ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = str(
                value
            )
    return row


def write_blocks(path, replacements):
    """Retain unrelated XML and cached values, translating surviving references."""
    sheets = openpyxl.load_workbook(path, read_only=True).sheetnames
    target = path.with_suffix(".updated.xlsx")
    detached_formulas = []
    with ZipFile(path) as original, ZipFile(target, "w") as output:
        for info in original.infolist():
            data = original.read(info.filename)
            match = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", info.filename)
            selected = (
                []
                if not match
                else [
                    d for d in replacements if d["sheet"] == sheets[int(match[1]) - 1]
                ]
            )
            if selected:
                root = ET.fromstring(data)
                sheet_data = root.find(f"{{{NS}}}sheetData")
                old_rows = {int(r.get("r")): r for r in sheet_data}
                selected = {d["start"]: d for d in selected}
                sheet_data.clear()
                old = new = 1
                row_map = {}
                while old <= max(max(old_rows), max(selected)):
                    row_map[old] = new
                    if old in selected:
                        ds = selected[old]
                        for row in block_rows(ds):
                            sheet_data.append(xml_row(row, new))
                            new += 1
                        old = ds["end"] + 1
                    else:
                        if old in old_rows:
                            row = old_rows[old]
                            row.set("r", str(new))
                            for cell in row:
                                cell.set("r", re.sub(r"\d+$", str(new), cell.get("r")))
                            sheet_data.append(row)
                        old += 1
                        new += 1
                for cell in sheet_data.iter(f"{{{NS}}}c"):
                    formula = cell.find(f"{{{NS}}}f")
                    if formula is None:
                        continue
                    references = re.findall(
                        r"\$?[A-Z]{1,3}\$?(\d+)", formula.text or ""
                    )
                    if any(int(r) not in row_map for r in references):
                        cached = cell.find(f"{{{NS}}}v")
                        if cached is None or cached.text is None:
                            raise ValueError(
                                f"Uncached cross-block formula: {formula.text}"
                            )
                        detached_formulas.append(
                            {
                                "cell": cell.get("r"),
                                "original formula": formula.text,
                                "retained cached value": cached.text,
                                "reason": "Legacy proxy references replaced vehicle block; frozen pending proxy review",
                            }
                        )
                        cell.remove(formula)
                        continue

                    def translate(match):
                        old_row = int(match.group(2))
                        if old_row not in row_map:
                            raise ValueError(
                                f"Formula refers inside a replaced block: {formula.text}"
                            )
                        return match.group(1) + str(row_map[old_row])

                    formula.text = re.sub(
                        r"(\$?[A-Z]{1,3}\$?)(\d+)", translate, formula.text or ""
                    )
                dimension = root.find(f"{{{NS}}}dimension")
                if dimension is not None:
                    dimension.set("ref", f"A1:Z{new - 1}")
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            output.writestr(info, data)
    # Read before replacing the original, catching malformed blocks/cells.
    parsed = read_workbook(target)
    assert len(parsed) == len(read_workbook(path)) + sum(
        d.get("append", False) for d in replacements
    )
    for ds in replacements:
        matches = [
            d for d in parsed if d["name"] == ds["name"] and d["sheet"] == ds["sheet"]
        ]
        assert len(matches) == 1 and matches[0]["exchanges"] == ds["exchanges"], ds[
            "name"
        ]
    target.replace(path)
    return detached_formulas


def main():
    from premise.vehicle_energy_use import normalize, select_source, source_name

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--premise", type=Path, default=ROOT.parent / "premise")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "results/sps_updated/vehicle_inventory_audit.json",
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    folder = args.premise / "premise/data/additional_inventories"
    database = vehicle_sources(folder)
    report = {
        "sources": {f: digest(folder / f) for f in FILES},
        "workbooks": {},
        "records": [],
        "unresolved": [],
    }
    prepared = []
    for path in (
        ROOT / "inventories/lci-sweet_sure.xlsx",
        folder / "lci-final-energy.xlsx",
    ):
        replacements = []
        report["workbooks"][str(path)] = {"before": digest(path)}
        datasets = read_workbook(path)
        if path.name == "lci-sweet_sure.xlsx":
            existing = {d["name"] for d in datasets}
            additions = []
            for fuel in (
                "electricity",
                "hydrogen",
                "petrol",
                "diesel",
                "compressed gas",
            ):
                verb = "used" if fuel in ("electricity", "hydrogen") else "burned"
                pairs = [
                    (
                        f"{fuel}, {verb} in passenger car vehicle",
                        f"{fuel}, {verb} in light duty vehicle",
                    )
                ]
                if fuel == "petrol":
                    pairs.append((pairs[0][0], "petrol, burned in heavy duty vehicle"))
                if fuel in ("hydrogen", "diesel", "compressed gas"):
                    pairs.append(
                        (
                            f"{fuel}, {verb} in single deck coach or urban bus",
                            f"{fuel}, {verb} in dedicated coach",
                        )
                    )
                for old_name, new_name in pairs:
                    if new_name not in existing:
                        ds = deepcopy(
                            next(d for d in datasets if d["name"] == old_name)
                        )
                        ds.update(name=new_name, append=True)
                        additions.append(ds)
            for old_name in (
                "electricity, used in passenger train",
                "hydrogen, used in a fuel cell in a passenger train",
            ):
                for new_name in (
                    old_name,
                    old_name.replace("passenger train", "freight train"),
                ):
                    if new_name not in existing:
                        ds = deepcopy(
                            next(
                                d
                                for d in read_workbook(folder / "lci-final-energy.xlsx")
                                if d["name"] == old_name
                            )
                        )
                        ds.update(
                            name=new_name, location="CH", sheet="lci", append=True
                        )
                        additions.append(ds)
            end = max(d["end"] for d in datasets)
            for i, ds in enumerate(additions):
                ds.update(start=end + i + 1, end=end + i + 1)
            datasets.extend(additions)
        for ds in datasets:
            name = source_name(ds["name"])
            if name is None:
                if (
                    any(t in ds["name"] for t in ("motorcycle", "train", "tram"))
                    and ds["unit"] == "megajoule"
                ):
                    report["unresolved"].append(
                        {
                            "workbook": path.name,
                            "name": ds["name"],
                            "reason": "No selected native source; retain explicit existing proxy pending coverage review",
                        }
                    )
                continue
            source = select_source(database, name, ds["location"])
            record = normalize(ds, source, database)
            # Serialize exactly the supported importer columns; source-specific
            # metadata stays in the activity-level provenance record.
            for e in ds["exchanges"]:
                if isinstance(e.get("categories"), (tuple, list)):
                    e["categories"] = "::".join(e["categories"])
                for key in list(e):
                    if e[key] is None or key not in {
                        "name",
                        "amount",
                        "location",
                        "unit",
                        "categories",
                        "type",
                        "product",
                        "comment",
                        "uncertainty type",
                    }:
                        del e[key]
            replacements.append(ds)
            report["records"].append(
                {"workbook": path.name, "wrapper": ds["name"], **record}
            )
        prepared.append((path, replacements))
    if args.write:
        for path, replacements in prepared:
            report["workbooks"][str(path)]["detached legacy proxy formulas"] = (
                write_blocks(path, replacements)
            )
            report["workbooks"][str(path)]["after"] = digest(path)
    report["written"] = args.write
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "updated blocks": len(report["records"]),
                "unresolved": len(report["unresolved"]),
                "written": args.write,
                "report": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
