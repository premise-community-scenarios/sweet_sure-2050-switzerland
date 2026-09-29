#!/usr/bin/env python3
"""Build the notebook's STEM/REMIND Pathways packages with ecoinvent 3.12.

Run from any directory in the premise environment:
    python dev/create_data_packages.py --dry-run
    python dev/create_data_packages.py
    python dev/create_data_packages.py --scenarios SPS1_bas0 --years 2050

Defaults: all scenarios in datapackage.json; REMIND SSP2-PkBudg1000;
2020–2050 every five years (2045 is interpolated); output ZIPs in dev/.
Existing packages with the same names are rebuilt. Each ZIP gets a .build.json
sidecar recording the source settings and input/output hashes.

The IAM decryption key comes from --key, PREMISE_KEY, or the existing notebook's
literal PathwaysDataPackage key, in that order. No notebook cells are executed.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from contextlib import chdir
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "dev/SWEET-SURE - creating data packages.ipynb"
SOURCE_VERSION = "3.12"
DEFAULT_YEARS = list(range(2020, 2051, 5))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="ecoinvent-3.12-cutoff")
    parser.add_argument("--source-db", default="ecoinvent-3.12-cutoff")
    parser.add_argument("--biosphere", default="ecoinvent-3.12-biosphere")
    parser.add_argument("--datapackage", type=Path, default=ROOT / "datapackage.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dev")
    parser.add_argument("--model", default="remind", choices=["remind", "image"])
    parser.add_argument("--pathway", default="SSP2-PkBudg1000")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        metavar="SCENARIO",
        help="Scenario labels, e.g. SPS1_bas0 SPS4_soc2; default: all declared scenarios.",
    )
    parser.add_argument("--years", nargs="+", type=int, default=DEFAULT_YEARS)
    parser.add_argument(
        "--key", help="IAM decryption key; otherwise use PREMISE_KEY or the notebook."
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear premise caches once before building, as in the notebook.",
    )
    parser.add_argument(
        "--dry-run",
        "--no-write",
        dest="dry_run",
        action="store_true",
        help="Validate the external input data and print the plan without loading Brightway or building packages.",
    )
    args = parser.parse_args(argv)
    args.datapackage = args.datapackage.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    return args


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_plan(args):
    import yaml

    descriptor = json.loads(args.datapackage.read_text())
    declared = descriptor["scenarios"]
    selected = args.scenarios or declared
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("Select at least one scenario, without duplicates.")
    unknown = set(selected) - set(declared)
    if unknown:
        raise ValueError(f"Unknown scenarios: {', '.join(sorted(unknown))}")
    if len(args.years) != len(set(args.years)):
        raise ValueError("Output years must not contain duplicates.")

    resources = {}
    for resource in descriptor["resources"]:
        path = (args.datapackage.parent / resource["path"]).resolve()
        if not path.is_file():
            raise ValueError(f"Missing datapackage resource: {path}")
        resources[resource["name"]] = {"path": str(path), "sha256": file_hash(path)}
    for name in ("scenario_data", "config", "inventories"):
        if name not in resources:
            raise ValueError(f"Missing datapackage resource declaration: {name}")

    counts = Counter()
    keys = set()
    variables_by_scenario = {scenario: set() for scenario in selected}
    with Path(resources["scenario_data"]["path"]).open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        metadata = ["model", "scenario", "region", "variables", "unit"]
        if not set(metadata).issubset(fields):
            raise ValueError("Scenario CSV is missing required metadata columns.")
        input_years = sorted(int(column) for column in fields if column.isdigit())
        if not input_years or not all(
            min(input_years) <= y <= max(input_years) for y in args.years
        ):
            raise ValueError(
                f"Output years must lie within the input year range: {input_years}"
            )
        for row in reader:
            if row["scenario"] not in selected:
                continue
            key = tuple(row[column] for column in metadata)
            if not all(key) or key in keys:
                raise ValueError(f"Empty or duplicate scenario metadata: {key}")
            keys.add(key)
            variables_by_scenario[row["scenario"]].add(row["variables"])
            for year in input_years:
                if not math.isfinite(float(row[str(year)])):
                    raise ValueError(f"Non-finite input for {key} in {year}")
            counts[row["scenario"]] += 1
    missing = set(selected) - set(counts)
    if missing:
        raise ValueError(f"No input rows for: {', '.join(sorted(missing))}")

    def referenced_variables(value):
        if isinstance(value, dict):
            for name, child in value.items():
                if name == "variable" and isinstance(child, str):
                    yield child
                yield from referenced_variables(child)
        elif isinstance(value, list):
            for child in value:
                yield from referenced_variables(child)

    config = yaml.safe_load(Path(resources["config"]["path"]).read_text())
    required_variables = set(referenced_variables(config))
    for scenario, available in variables_by_scenario.items():
        missing = required_variables - available
        if missing:
            raise ValueError(
                f"Configuration variables missing from {scenario}: {sorted(missing)}. "
                "Variable labels must match exactly, including whitespace."
            )

    return {
        "project": args.project,
        "source_db": args.source_db,
        "source_version": SOURCE_VERSION,
        "biosphere_name": args.biosphere,
        "model": args.model,
        "pathway": args.pathway,
        "system_model": "cutoff",
        "use_absolute_efficiency": True,
        "years": sorted(args.years),
        "input_years": input_years,
        "interpolated_years": sorted(set(args.years) - set(input_years)),
        "scenarios": list(selected),
        "rows_per_scenario": dict(counts),
        "datapackage": str(args.datapackage),
        "datapackage_sha256": file_hash(args.datapackage),
        "resources": resources,
        "output_dir": str(args.output_dir),
    }


def resolve_key(explicit_key, notebook=NOTEBOOK):
    key = explicit_key or os.environ.get("PREMISE_KEY")
    if key:
        return key
    if notebook.is_file():
        content = json.loads(notebook.read_text())
        for cell in content.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            try:
                tree = ast.parse("".join(cell.get("source", [])))
            except SyntaxError:
                continue
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, "id", getattr(call.func, "attr", None))
                if name != "PathwaysDataPackage":
                    continue
                for keyword in call.keywords:
                    if keyword.arg == "key" and isinstance(keyword.value, ast.Constant):
                        if (
                            isinstance(keyword.value.value, (str, bytes))
                            and keyword.value.value
                        ):
                            return keyword.value.value
    raise ValueError("Set PREMISE_KEY or pass --key to use encrypted IAM data.")


def run_build(args, plan, key):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # premise writes logs, temporary matrices, and final ZIPs relative to cwd.
    with chdir(args.output_dir):
        import bw2data as bd
        from datapackage import Package
        import premise

        if args.project not in bd.projects:
            raise ValueError(f"Brightway project does not exist: {args.project}")
        bd.projects.set_current(args.project)
        for name in (args.source_db, args.biosphere):
            if name not in bd.databases:
                raise ValueError(
                    f"Database {name!r} is missing from project {args.project!r}."
                )
        if args.clear_cache:
            premise.clear_cache()

        package = Package(str(args.datapackage))
        for index, scenario in enumerate(plan["scenarios"], 1):
            name = f"{args.model}-{args.pathway}-stem-{scenario}"
            print(f"[{index}/{len(plan['scenarios'])}] Building {name}", flush=True)
            builder = premise.PathwaysDataPackage(
                scenarios=[
                    {
                        "model": args.model,
                        "pathway": args.pathway,
                        "external scenarios": [{"scenario": scenario, "data": package}],
                    }
                ],
                years=plan["years"],
                source_db=args.source_db,
                source_version=SOURCE_VERSION,
                source_type="brightway",
                system_model="cutoff",
                biosphere_name=args.biosphere,
                key=key,
                use_absolute_efficiency=True,
            )
            try:
                from dev.stem_efficiency import absolute_stem_efficiencies
                from dev.premise_compat import preserve_pathways_mappings
            except ModuleNotFoundError:
                from stem_efficiency import absolute_stem_efficiencies
                from premise_compat import preserve_pathways_mappings
            with absolute_stem_efficiencies(
                args.source_db, args.output_dir / f"{name}.efficiencies.json"
            ), preserve_pathways_mappings():
                builder.create_datapackage(
                    name=name,
                    contributors=[{"name": "Romain", "email": "r_s at me.com"}],
                )
            archive = args.output_dir / f"{name}.zip"
            if not archive.is_file():
                raise RuntimeError(
                    f"premise did not create the expected package: {archive}"
                )
            manifest = {
                **plan,
                "scenario": scenario,
                "premise_version": str(premise.__version__),
                "build_script_sha256": file_hash(__file__),
                "efficiency_adapter_sha256": file_hash(
                    Path(__file__).with_name("stem_efficiency.py")
                ),
                "pathways_adapter_sha256": file_hash(
                    Path(__file__).with_name("premise_compat.py")
                ),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "package": str(archive),
                "package_sha256": file_hash(archive),
            }
            archive.with_suffix(".build.json").write_text(
                json.dumps(manifest, indent=2) + "\n"
            )
            del builder
            print(f"Completed: {archive}", flush=True)


def main(argv=None):
    args = parse_args(argv)
    plan = build_plan(args)
    print(json.dumps(plan, indent=2), flush=True)
    if args.dry_run:
        print(
            "Input validation passed. No packages built; Brightway and IAM access were not exercised."
        )
        return 0
    run_build(args, plan, resolve_key(args.key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
