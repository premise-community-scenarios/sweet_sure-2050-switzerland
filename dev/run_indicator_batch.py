#!/usr/bin/env python3
"""Run and independently verify every package from a completed STEM build.

Each scenario has its own subprocess, runtime directory, logs and resumable
results. Scientific scripts and Pathways are frozen before starting the batch.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import traceback

YEARS = [2020, 2025, 2030, 2035, 2040, 2045, 2050]


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read_build_packages(build):
    """Read either the legacy summary or the current validated build manifest."""
    summary_path = build / "completion_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        packages = summary["packages"]
        if len(packages) != summary["validated_packages"]:
            raise ValueError("Incomplete package build summary")
        years = summary["years_per_package"]
    else:
        summary_path = build / "progress.json"
        summary = json.loads(summary_path.read_text())
        scenarios = summary["scenarios"]
        if (
            summary["status"] != "complete"
            or set(scenarios) != set(summary["plan"]["scenarios"])
            or any(entry["status"] != "complete" for entry in scenarios.values())
        ):
            raise ValueError("Incomplete package build manifest")
        packages = [
            {
                "scenario": name,
                "package": entry["package"],
                "sha256": entry["package_sha256"],
            }
            for name, entry in scenarios.items()
        ]
        years = summary["plan"]["years"]
    if not packages or len({e["scenario"] for e in packages}) != len(packages):
        raise ValueError("Empty or duplicated package scenarios")
    if years != YEARS:
        raise ValueError("Unexpected package years")
    for entry in packages:
        if digest(entry["package"]) != entry["sha256"]:
            raise ValueError(f"Changed package: {entry['scenario']}")
    return summary_path, packages


def prepare(build, output, method_set="legacy"):
    summary_path, packages = read_build_packages(build)
    signature_path = output / "inputs.json"
    if signature_path.exists():
        signature = json.loads(signature_path.read_text())
        if signature.get("method_set", "legacy") != method_set:
            raise ValueError("Frozen indicator method set changed")
        if signature["build_summary_sha256"] != digest(summary_path):
            raise ValueError("Package build summary has changed")
        for name, checksum in signature["snapshot_sha256"].items():
            if digest(output / name) != checksum:
                raise ValueError(f"Frozen input changed: {name}")
        return packages
    software = output / "software"
    software.mkdir()
    scripts = software / "dev"
    scripts.mkdir()
    for name in (
        "generate_indicators.py",
        "indicator_coverage.py",
        "verify_indicator_totals.py",
        "SWEET-SURE - indicators generation.ipynb",
        "run_indicator_batch.py",
        "indicator_methods.py",
    ):
        shutil.copy2(Path(__file__).parent / name, scripts / name)
    pathways = Path(importlib.util.find_spec("pathways").origin).parent
    shutil.copytree(
        pathways,
        software / "pathways",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(
        build / "inputs/configuration_file/config.yaml", output / "config.yaml"
    )
    method_files = []
    if method_set == "ef31-relics":
        from indicator_methods import freeze_methods

        source = Path(__file__).resolve().parents[1] / "configuration_file"
        for name in ("relics_mapping.yaml", "RELICS-LICENSE"):
            shutil.copy2(source / name, output / name)
            method_files.append(output / name)
        freeze_methods(
            software / "pathways/data/lcia_ei312.json",
            output / "relics_mapping.yaml",
            packages,
            output,
        )
        method_files.append(output / "method_registry.json")
    files = list(software.rglob("*")) + [output / "config.yaml"] + method_files
    signature = {
        "created_at": now(),
        "build": str(build),
        "build_summary_path": str(summary_path),
        "build_summary_sha256": digest(summary_path),
        "years": YEARS,
        "method_set": method_set,
        "python": sys.executable,
        "pathways_original_source": str(pathways),
        "snapshot_sha256": {
            str(p.relative_to(output)): digest(p) for p in files if p.is_file()
        },
        "packages": [
            {k: e[k] for k in ("scenario", "package", "sha256")} for e in packages
        ],
    }
    write_json(signature_path, signature)
    return packages


def combine(output, entries):
    import numpy as np
    import pandas as pd

    frames, reports = [], []
    methods = None
    variables = None
    for entry in entries:
        folder = output / "scenarios" / entry["scenario"]
        stem = "results_" + Path(entry["package"]).stem
        manifest = json.loads((folder / f"{stem}.manifest.json").read_text())
        validation = json.loads(
            (folder / f"{stem}_independent_validation.json").read_text()
        )
        if manifest["status"] != "complete" or not validation["passed"]:
            raise ValueError(f"Unvalidated scenario: {entry['scenario']}")
        if manifest["calculation"]["package_sha256"] != entry["sha256"]:
            raise ValueError("Package does not match batch inputs")
        if digest(folder / f"{stem}.gzip") != manifest["raw_sha256"]:
            raise ValueError("Raw result checksum mismatch")
        current_methods = manifest["calculation"]["methods"]
        if methods is None:
            methods = current_methods
            variables = manifest["variables"]
        elif methods != current_methods or variables != manifest["variables"]:
            raise ValueError("Methods or energy variables differ between scenarios")
        expected_checks = {(year, method) for year in YEARS for method in methods}
        actual_checks = {(c["year"], c["method"]) for c in validation["checks"]}
        if actual_checks != expected_checks or len(validation["checks"]) != len(
            expected_checks
        ):
            raise ValueError("Incomplete independent verification")
        frame = pd.read_parquet(folder / f"{stem}_indicators.parquet")
        exported_totals = frame.groupby(["year", "impact_category"]).value.sum()
        for check in validation["checks"]:
            label, unit = manifest["methods"][check["method"]]
            if set(frame.loc[frame.impact_category.eq(label), "unit"]) != {unit}:
                raise ValueError(f"Incorrect exported unit: {label}")
            np.testing.assert_allclose(
                exported_totals.loc[check["year"], label],
                check["pathways_score"],
                rtol=1e-10,
                atol=1e-8,
            )
        frames.append(frame)
        warnings = json.loads((folder / "warnings.json").read_text())
        inventory_report = Path(entry["package"]).with_suffix(".validation.json")
        inventory_warnings = (
            json.loads(inventory_report.read_text())["findings_by_rule"]
            if inventory_report.exists()
            else None
        )
        reports.append(
            {
                "scenario": entry["scenario"],
                "rows": len(frame),
                "validation": manifest["validation"],
                "independent_checks": len(validation["checks"]),
                "max_relative_difference": max(
                    x["relative_difference"] for x in validation["checks"]
                ),
                "python_warnings": len(warnings["python_warnings"]),
                "logging_warnings": len(warnings["logging_warnings"]),
                "inventory_findings_by_rule": inventory_warnings,
                "inventory_warning_count": (
                    sum(
                        r["count"]
                        for r in inventory_warnings
                        if r["severity"] == "warning"
                    )
                    if inventory_warnings is not None
                    else None
                ),
            }
        )
    df = pd.concat(frames, ignore_index=True)
    keys = [c for c in df.columns if c != "value"]
    if df.duplicated(keys).any() or not np.isfinite(df.value).all():
        raise ValueError("Duplicated or nonfinite combined results")
    if len(df.scenario.unique()) != len(entries) or set(df.year) != set(YEARS):
        raise ValueError("Missing scenarios or years")
    df.to_parquet(output / "df_final.parquet", index=False)
    df.to_csv(output / "df_final.csv", index=False)
    df.to_excel(output / "df_final.xlsx", index=False)
    totals = df.groupby(
        ["model", "scenario", "region", "year", "impact_category", "unit"],
        as_index=False,
    ).value.sum()
    if len(totals) != len(entries) * len(YEARS) * len(methods):
        raise ValueError("Incomplete combined indicator totals")
    totals.to_csv(output / "indicator_totals.csv", index=False)
    write_json(
        output / "completion_summary.json",
        {
            "finished_at": now(),
            "scenarios": len(entries),
            "years": YEARS,
            "indicators": len(methods),
            "variables": variables,
            "scenario_names": sorted(df.scenario.unique().tolist()),
            "method_registry": (
                "method_registry.json"
                if (output / "method_registry.json").exists()
                else None
            ),
            "dense": (output / "method_registry.json").exists(),
            "rows": len(df),
            "independent_checks": len(totals),
            "unlinked_variables": sum(
                r["validation"]["unlinked_variables"] for r in reports
            ),
            "nonfinite_results": 0,
            "packages": reports,
            "inventory_warning_count": (
                sum(r["inventory_warning_count"] for r in reports)
                if all(r["inventory_warning_count"] is not None for r in reports)
                else None
            ),
            "outputs_sha256": {
                p.name: digest(p)
                for p in (
                    output / "df_final.parquet",
                    output / "df_final.csv",
                    output / "df_final.xlsx",
                    output / "indicator_totals.csv",
                    *(
                        [output / "method_registry.json"]
                        if (output / "method_registry.json").exists()
                        else []
                    ),
                )
            },
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--method-set", choices=["legacy", "ef31-relics"], default="legacy"
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("workers must be positive")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".batch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        entries = prepare(args.build.resolve(), output, args.method_set)
        path = output / "progress.json"
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "started_at": now(),
                "scenarios": {e["scenario"]: {"status": "pending"} for e in entries},
            }
        )
        state.update(status="running", pid=os.getpid(), workers=args.workers)
        guard = threading.Lock()

        def update(name=None, **values):
            with guard:
                if name:
                    state["scenarios"][name].update(values)
                else:
                    state.update(values)
                state["updated_at"] = now()
                write_json(path, state)
                counts = {
                    s: sum(r["status"] == s for r in state["scenarios"].values())
                    for s in (
                        "pending",
                        "calculating",
                        "verifying",
                        "complete",
                        "failed",
                    )
                }
                (output / "STATUS.md").write_text(
                    f"Updated: {state['updated_at']}\n\nStatus: {state['status']}\n\n"
                    + "\n".join(f"- {k}: {v}" for k, v in counts.items())
                    + "\n"
                )
                if name:
                    print(now(), name, values.get("status"), flush=True)

        def process(entry):
            name = entry["scenario"]
            folder = output / "scenarios" / name
            folder.mkdir(parents=True, exist_ok=True)
            stem = "results_" + Path(entry["package"]).stem
            scripts = output / "software/dev"
            env = dict(
                os.environ,
                PYTHONPATH=str(output / "software"),
                PYTHONUNBUFFERED="1",
                OMP_NUM_THREADS="1",
                OPENBLAS_NUM_THREADS="1",
                MKL_NUM_THREADS="1",
            )
            try:
                update(name, status="calculating", started_at=now())
                command = [
                    sys.executable,
                    str(scripts / "generate_indicators.py"),
                    entry["package"],
                    "--output-dir",
                    str(folder),
                    "--config",
                    str(output / "config.yaml"),
                    "--years",
                    *map(str, YEARS),
                    "--no-html",
                    "--resume",
                ]
                if args.method_set == "ef31-relics":
                    command.extend(
                        ["--method-registry", str(output / "method_registry.json")]
                    )
                with (folder / "calculation.log").open("a") as log:
                    subprocess.run(
                        command,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
                update(name, status="verifying")
                command = [
                    sys.executable,
                    str(scripts / "verify_indicator_totals.py"),
                    str(folder / f"{stem}.manifest.json"),
                    "--years",
                    *map(str, YEARS),
                    "--rtol",
                    "1e-8",
                    "--solver",
                    "superlu",
                    "--demand-mode",
                    "separate",
                ]
                with (folder / "independent_verification.log").open("a") as log:
                    subprocess.run(
                        command,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
                update(name, status="complete", finished_at=now())
            except Exception:
                update(name, status="failed", error=traceback.format_exc())
                raise

        update()
        failures = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process, entry): entry for entry in entries}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    failures.append(str(exc))
        if failures:
            update(status="failed", errors=failures)
            raise RuntimeError(
                f"{len(failures)} scenarios failed; inspect scenario logs"
            )
        update(status="combining")
        combine(output, entries)
        update(status="complete", finished_at=now())


if __name__ == "__main__":
    main()
