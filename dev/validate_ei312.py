"""Build the external scenario against ecoinvent 3.12 without writing a database.

Run from the repository root in an environment with premise and Brightway.
The original Excel inventory is migrated from its declared 3.10 version by premise.
"""

import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path

import bw2data as bd
import numpy as np
import pandas as pd
from datapackage import Package
from premise import NewDatabase
from premise.utils import load_database
import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="ecoinvent-3.12-cutoff")
    parser.add_argument("--source-db", default="ecoinvent-3.12-cutoff")
    parser.add_argument("--biosphere", default="biosphere")
    parser.add_argument("--scenario", default="SPS1_bas0")
    parser.add_argument("--year", type=int, default=2050)
    parser.add_argument("--model", default="remind")
    parser.add_argument("--pathway", default="SSP2-NPi")
    parser.add_argument("--key", default=os.environ.get("PREMISE_KEY"))
    parser.add_argument("--use-cache", action="store_true", help="Reuse premise source and bundled inventory caches")
    parser.add_argument("--report", type=Path, default=Path("dev/ei312-validation.json"))
    args = parser.parse_args()
    if not args.key:
        parser.error("Set PREMISE_KEY or supply --key for encrypted IAM data.")
    if args.project not in bd.projects:
        parser.error(f"Brightway project does not exist: {args.project}")
    bd.projects.set_current(args.project)
    for name in (args.source_db, args.biosphere):
        if name not in bd.databases:
            parser.error(f"Database does not exist: {name}")
    package = Package("datapackage.json")
    if args.scenario not in package.descriptor["scenarios"]:
        parser.error(f"Unknown external scenario: {args.scenario}")
    ndb = NewDatabase(
        scenarios=[{
            "model": args.model, "pathway": args.pathway, "year": args.year,
            "external scenarios": [{"scenario": args.scenario, "data": package}],
        }],
        source_db=args.source_db, source_version="3.12", system_model="cutoff",
        biosphere_name=args.biosphere, key=args.key,
        use_absolute_efficiency=True, generate_reports=False,
        use_cached_database=args.use_cache, use_cached_inventories=args.use_cache,
    )
    ndb.update(["external"])
    scenario = load_database(ndb.scenarios[0], original_database=[], warning=False)
    database = scenario["database"]
    report = {
        "project": args.project, "source_db": args.source_db,
        "biosphere": args.biosphere, "ecoinvent_version": "3.12",
        "inventory_source_version": package.descriptor["ecoinvent"]["version"],
        "premise_version": importlib.metadata.version("premise"),
        "scenario": args.scenario, "year": args.year,
        "model": args.model, "pathway": args.pathway,
        "updates": ["external"], "database_written": False,
        "datasets": len(database),
        "source_datasets": bd.databases[args.source_db].get("number"),
        "descriptor_sha256": hashlib.sha256(Path("datapackage.json").read_bytes()).hexdigest(),
        "resource_sha256": {
            r.descriptor["path"]: hashlib.sha256(Path(r.descriptor["path"]).read_bytes()).hexdigest()
            for r in package.resources
        },
    }
    keys = Counter((d["name"], d["reference product"], d["location"]) for d in database)
    biosphere = list(bd.Database(args.biosphere))
    biosphere_keys = {flow.key for flow in biosphere}
    biosphere_metadata = Counter(
        (flow["name"], tuple(flow.get("categories", ())), flow["unit"])
        for flow in biosphere
    )
    unresolved = []
    invalid_biosphere = []
    nonfinite = []
    for dataset in database:
        for exchange in dataset.get("exchanges", []):
            if not math.isfinite(float(exchange["amount"])):
                nonfinite.append(dataset["name"])
            if exchange["type"] == "technosphere":
                key = (exchange["name"], exchange.get("product"), exchange.get("location"))
                if keys[key] != 1:
                    unresolved.append({"consumer": dataset["name"], "supplier": key})
            elif exchange["type"] == "biosphere":
                # Source exchanges can be unlinked until export. Match their
                # complete flow metadata when no UUID is retained in memory.
                code = exchange.get("input", (None, None))[1]
                categories = exchange.get("categories", ())
                if isinstance(categories, str):
                    categories = categories.split("::")
                metadata = (exchange["name"], tuple(categories), exchange["unit"])
                linked = (args.biosphere, code) in biosphere_keys if code else biosphere_metadata[metadata] == 1
                if not linked:
                    invalid_biosphere.append({"consumer": dataset["name"], "flow": metadata, "code": code})
    report["unresolved_technosphere"] = unresolved
    report["unresolved_biosphere"] = invalid_biosphere
    report["nonfinite_amounts"] = nonfinite
    report["sps_markets"] = sorted({d["name"] for d in database if "(SPS)" in d["name"] and d["location"] == "CH"})
    config = yaml.safe_load(package.get_resource("config").raw_read())
    frame = pd.read_csv(package.get_resource("scenario_data").source)
    frame = frame[(frame.scenario == args.scenario) & (frame.region == "CH")].set_index("variables")
    years = sorted(int(c) for c in frame.columns if c.isdigit())
    if not min(years) <= args.year <= max(years):
        raise ValueError("Validation year is outside the external scenario time series")
    expected = set()
    zero_volume = set()
    for market in config["markets"]:
        variables = [config["production pathways"][p]["production volume"]["variable"] for p in market["includes"]]
        totals = frame.loc[variables, [str(y) for y in years]].sum().to_numpy(dtype=float)
        volume = np.interp(args.year, years, totals)
        if "CH" not in market.get("except regions", []) and volume > 0:
            expected.add(market["name"])
        elif volume == 0:
            zero_volume.add(market["name"])
    actual = {d["name"] for d in database if d["location"] == "CH"}
    report["missing_markets"] = sorted(expected - actual)
    report["zero_volume_markets"] = sorted(zero_volume)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Built {len(database)} datasets; {len(unresolved)} unresolved technosphere exchanges. Report: {args.report}")
    if unresolved or invalid_biosphere or nonfinite or report["missing_markets"]:
        raise ValueError(f"Build validation failed; see {args.report}")


if __name__ == "__main__":
    main()
