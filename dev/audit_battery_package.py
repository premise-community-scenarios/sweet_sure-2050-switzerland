#!/usr/bin/env python3
"""Check exported battery capacity per MJ against vehicle lifetime specifications.

Expected coefficients do not call premise's sizing or normalization routines.
For fixed-range vehicles, capacity and lifetime energy scale together. For
fixed-capacity motorcycles and trolleybuses, use the observed service energy.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pandas as pd
import yaml

from update_vehicle_energy_inventories import FILES, read_workbook, vehicle_sources


def number(comment, pattern):
    match = re.search(pattern, comment, re.I)
    if match is None:
        raise ValueError(f"Missing physical specification: {pattern}")
    return float(match.group(1))


def specification(source):
    raw = source.get("vehicle battery sizing")
    comment = source.get("comment", "")
    if raw:
        spec = json.loads(raw) if isinstance(raw, str) else raw
        capacity = spec["capacity kWh"]
        replacements = spec["replacements"]
        lifetime = spec["lifetime km"]
        services = (
            spec["passengers"]
            if source["unit"] == "person kilometer"
            else spec.get("cargo mass kg", 1000) / 1000
        )
    else:
        capacity = number(
            comment,
            r"(?:Battery capacity:|Fuel tank capacity:)\s*([\d.]+)\s*(?:\[)?kWh",
        )
        lifetime = number(comment, r"(?:Km over lifetime:|Lifetime:)\s*([\d.]+)")
        match = re.search(r"Battery replacement throughout lifetime: ([\d.]+)", comment)
        replacements = float(match.group(1)) if match else 0
        services = (
            number(comment, r"Number of passengers onboard: ([\d.]+)")
            if source["unit"] == "person kilometer"
            else 1
        )
    grid = sum(
        e["amount"]
        for e in source["exchanges"]
        if e["type"] == "technosphere"
        and e.get("product", "").startswith("electricity,")
        and e["unit"] == "kilowatt hour"
    )
    production = sum(
        e["amount"] for e in source["exchanges"] if e["type"] == "production"
    )
    return {
        "baseline_capacity_kWh": capacity,
        "replacements": replacements,
        "lifetime_km": lifetime,
        "services_per_km": services,
        "baseline_grid_kWh_per_service": grid / production,
        "fixed_range": bool(raw),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--premise", type=Path, required=True)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scenarios", nargs="+", default=[f"SPS{i}_bas0" for i in range(1, 5)]
    )
    args = parser.parse_args()
    sys.path.insert(0, str(args.premise.resolve()))
    from premise.inventory_imports import migrate_import_db

    inventory_folder = args.premise / "premise/data/additional_inventories"
    sources = vehicle_sources(inventory_folder)
    by_name = {d["name"]: d for d in sources}
    capacity_data = read_workbook(inventory_folder / "lci-battery-capacity.xlsx")
    for dataset in capacity_data:
        for exchange in dataset["exchanges"]:
            exchange["reference product"] = exchange.get("product")
    # The workbook uses 3.10 identities; 3.12 adds "rechargeable" to NMC532.
    migrate_import_db(SimpleNamespace(data=capacity_data), "3.10", "3.12")
    capacity_sources = {
        d["name"]: d
        for d in capacity_data
        if d["name"]
        in {
            "market for battery capacity, Li-ion, NMC532",
            "market for battery capacity, Li-ion, NMC532, stationary",
        }
    }
    assert len(capacity_sources) == 2
    density = yaml.safe_load(
        (args.premise / "premise/data/battery/energy_density.yaml").read_text()
    )["NMC532"]["target"]
    records, market_records, hashes = [], [], {}
    packages = sorted(args.packages.glob("*.zip"))
    assert {p.stem.split("stem-")[1] for p in packages} == set(args.scenarios)
    assert len(packages) == len(set(args.scenarios)), "Unexpected package selection"
    for package in packages:
        with package.open("rb") as stream:
            hashes[package.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        evidence = json.loads(package.with_suffix(".vehicle_energy.json").read_text())
        with ZipFile(package) as archive:
            for year in range(2020, 2051, 5):
                (member,) = [
                    n for n in archive.namelist() if n.endswith(f"/{year}/A_matrix.csv")
                ]
                matrix = pd.read_csv(
                    archive.open(member), sep=";", float_precision="round_trip"
                )
                nodes = pd.read_csv(
                    archive.open(member.replace("A_matrix.csv", "A_matrix_index.csv")),
                    sep=";",
                )

                def inputs_for(idx):
                    return matrix[
                        matrix["index of activity"].eq(idx) & matrix.flip.eq(1)
                    ].merge(nodes, left_on="index of product", right_on="index")

                for name, source in capacity_sources.items():
                    markets = nodes[nodes.name.eq(name) & nodes.location.eq("GLO")]
                    assert len(markets) == 1
                    inputs = inputs_for(markets["index"].item())
                    expected_density = np.interp(
                        year,
                        [2020, 2050],
                        [density[2020]["mean"], density[2050]["mean"]],
                    )
                    for exchange in source["exchanges"]:
                        if (
                            exchange["type"] != "technosphere"
                            or exchange["unit"] != "kilogram"
                        ):
                            continue
                        expected = (
                            exchange["amount"]
                            * density[2020]["mean"]
                            / expected_density
                        )
                        actual = inputs.loc[
                            inputs.name.eq(exchange["name"])
                            & inputs.unit.eq("kilogram"),
                            "value",
                        ].sum()
                        market_records.append(
                            {
                                "scenario": package.stem.split("stem-")[1],
                                "year": year,
                                "check": "NMC532 density",
                                "activity": name,
                                "supplier": exchange["name"],
                                "expected": expected,
                                "actual": actual,
                                "passed": bool(
                                    np.isclose(actual, expected, rtol=2e-6, atol=1e-14)
                                ),
                            }
                        )
                for name in (
                    "EV charger, level 3, plugin, 200 kW",
                    "EV charger, level 3, with pantograph, 450 kW",
                ):
                    chargers = nodes[nodes.name.eq(name) & nodes.location.eq("RER")]
                    assert len(chargers) == 1
                    inputs = inputs_for(chargers["index"].item())
                    storage = inputs[
                        inputs["reference product"].isin(
                            ["storage battery", "electricity storage capacity"]
                        )
                    ]
                    market_records.append(
                        {
                            "scenario": package.stem.split("stem-")[1],
                            "year": year,
                            "check": "No stationary storage in charger",
                            "activity": name,
                            "supplier": "storage battery / capacity",
                            "expected": 0,
                            "actual": float(storage.value.abs().sum()),
                            "passed": not storage.value.ne(0).any(),
                        }
                    )
                selected = [
                    r
                    for r in evidence
                    if r["year"] == year
                    and r["location"] == "CH"
                    and r["wrapper"].startswith("electricity")
                    and any(
                        e["supplier"][1] == "electricity storage capacity"
                        for e in r["retained equipment"]
                    )
                ]
                assert len(selected) >= 7, (package.name, year, len(selected))
                assert len({r["wrapper"] for r in selected}) == len(selected)
                for record in selected:
                    spec = specification(by_name[record["source"][0]])
                    grid = record["purchased MJ per source service"] / 3.6
                    denominator_grid = (
                        spec["baseline_grid_kWh_per_service"]
                        if spec["fixed_range"]
                        else grid
                    )
                    expected = (
                        spec["baseline_capacity_kWh"]
                        * (1 + spec["replacements"])
                        / (
                            spec["lifetime_km"]
                            * spec["services_per_km"]
                            * denominator_grid
                            * 3.6
                        )
                    )
                    matches = nodes[
                        nodes.name.eq(record["wrapper"])
                        & nodes.location.eq("CH")
                        & nodes.unit.eq("megajoule")
                    ]
                    assert len(matches) == 1
                    idx = matches["index"].item()
                    inputs = inputs_for(idx)
                    battery = inputs[
                        inputs["reference product"].eq("electricity storage capacity")
                        & inputs.unit.eq("kilowatt hour")
                    ]
                    assert not battery.empty
                    actual = battery.value.sum()
                    records.append(
                        {
                            "scenario": package.stem.split("stem-")[1],
                            "year": year,
                            "wrapper": record["wrapper"],
                            "source": record["source"][0],
                            **spec,
                            "scenario_grid_kWh_per_service": grid,
                            "expected_kWh_capacity_per_MJ": expected,
                            "exported_kWh_capacity_per_MJ": actual,
                            "relative_error": actual / expected - 1,
                            "capacity_market": "; ".join(sorted(battery.name)),
                            "passed": bool(
                                np.isclose(actual, expected, rtol=2e-6, atol=1e-14)
                            ),
                        }
                    )
    result = pd.DataFrame(records)
    market_checks = pd.DataFrame(market_records)
    args.output.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output / "battery_capacity_per_MJ.csv", index=False)
    market_checks.to_csv(args.output / "battery_market_checks.csv", index=False)
    summary = {
        "passed": bool(result.passed.all() and market_checks.passed.all()),
        "checks": len(result),
        "density_checks": int(market_checks.check.eq("NMC532 density").sum()),
        "charger_storage_checks": int(
            market_checks.check.eq("No stationary storage in charger").sum()
        ),
        "scenarios": sorted(result.scenario.unique()),
        "years": sorted(int(y) for y in result.year.unique()),
        "maximum_relative_error": float(result.relative_error.abs().max()),
        "formula": "capacity kWh * (1 + replacements) / (lifetime km * grid kWh/km * 3.6)",
        "fixed_range": "Capacity and lifetime energy scale together; their ratio remains at the source design value.",
        "fixed_capacity": "The nominal capacity stays fixed; scenario service energy comes from the build evidence.",
        "scope": "Exported direct capacity-market inputs for battery-electric road vehicles; physical battery mass is supplied by the upstream capacity markets.",
        "packages_sha256": hashes,
        "source_files_sha256": {
            name: hashlib.sha256((inventory_folder / name).read_bytes()).hexdigest()
            for name in [*FILES, "lci-battery-capacity.xlsx"]
        },
        "density_definition_sha256": hashlib.sha256(
            (args.premise / "premise/data/battery/energy_density.yaml").read_bytes()
        ).hexdigest(),
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.output / "battery_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    print(json.dumps(summary, indent=2))
    assert summary[
        "passed"
    ], "Exported battery allocation differs from vehicle specifications"


if __name__ == "__main__":
    main()
