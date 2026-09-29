"""Build and validate all STEM packages with isolated workers and resumable state.

Run with the premise Python environment. Inputs and premise source are frozen
under the output directory. Validated ZIPs are collected in packages/.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

try:
    from dev.create_data_packages import (
        ROOT,
        build_plan,
        file_hash,
        parse_args,
        resolve_key,
    )
except ModuleNotFoundError:
    from create_data_packages import (
        ROOT,
        build_plan,
        file_hash,
        parse_args,
        resolve_key,
    )

SCRIPTS = [
    "build_all_data_packages.py",
    "create_data_packages.py",
    "stem_efficiency.py",
    "premise_compat.py",
    "validate_stem_build.py",
    "audit_pathways_package.py",
    "validate_package_coverage.py",
    "audit_gains_car_emissions.py",
]
PREMISE_ROOT = ROOT.parent / "premise"
TESTED_RUN = ROOT / "dev/smoke_sps1_ei312_20260928/mapping_fixed_run"
NOX_BASELINE = (
    ROOT
    / "dev/smoke_sps1_ei312_20260928/complete_run/pathways_temp/inventories/remind/SSP2-PkBudg1000-SPS1_bas0/2020"
)


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def prepare(output):
    state_path = output / "progress.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        for resource in state["plan"]["resources"].values():
            if file_hash(resource["path"]) != resource["sha256"]:
                raise ValueError("Frozen batch inputs changed; refusing to resume")
        return state
    plan = build_plan(parse_args(["--output-dir", str(output)]))
    if any((output / name).exists() for name in ["inputs", "software", "packages"]):
        raise ValueError(
            "Incomplete batch setup already exists; inspect it before reusing this directory"
        )
    descriptor = json.loads(Path(plan["datapackage"]).read_text())
    inputs = output / "inputs"
    inputs.mkdir()
    shutil.copy2(plan["datapackage"], inputs / "datapackage.json")
    for resource in descriptor["resources"]:
        destination = inputs / resource["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / resource["path"], destination)
    software = output / "software"
    (software / "dev").mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy2(ROOT / "dev" / name, software / "dev" / name)
    shutil.copytree(
        PREMISE_ROOT / "premise",
        software / "premise",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    baseline = inputs / "nox_baseline_2020"
    baseline.mkdir()
    for name in [
        "A_matrix.csv",
        "B_matrix.csv",
        "A_matrix_index.csv",
        "B_matrix_index.csv",
    ]:
        path = NOX_BASELINE / name
        shutil.copy2(path, baseline / path.name)
    frozen_plan = build_plan(
        parse_args(
            [
                "--datapackage",
                str(inputs / "datapackage.json"),
                "--output-dir",
                str(output),
            ]
        )
    )
    source_hashes = {
        str(path.relative_to(software)): file_hash(path)
        for path in software.rglob("*")
        if path.is_file()
    }
    (output / "packages").mkdir()
    state = {
        "created_at": now(),
        "status": "prepared",
        "plan": frozen_plan,
        "python": sys.executable,
        "premise_commit": subprocess.check_output(
            ["git", "-C", str(PREMISE_ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "software_hashes": source_hashes,
        "scenarios": {
            name: {"status": "pending", "attempts": 0} for name in plan["scenarios"]
        },
    }
    write_json(state_path, state)
    return state


def verify_completed(output, state):
    for scenario, entry in state["scenarios"].items():
        if entry["status"] == "complete":
            path = Path(entry["package"])
            if not path.is_file() or file_hash(path) != entry["package_sha256"]:
                raise ValueError(f"Completed package changed: {scenario}")
    for relative, expected in state["software_hashes"].items():
        if file_hash(output / "software" / relative) != expected:
            raise ValueError(f"Frozen software changed: {relative}")


def reuse_tested(run, plan):
    stem = "remind-SSP2-PkBudg1000-stem-SPS1_bas0"
    build_path = TESTED_RUN / f"{stem}.build.json"
    if not build_path.exists():
        return False
    build = json.loads(build_path.read_text())
    for name, resource in plan["resources"].items():
        if build["resources"][name]["sha256"] != resource["sha256"]:
            return False
    for field in [
        "project",
        "source_db",
        "biosphere_name",
        "model",
        "pathway",
        "years",
        "use_absolute_efficiency",
    ]:
        if build[field] != plan[field]:
            return False
    for name, field in [
        ("create_data_packages.py", "build_script_sha256"),
        ("stem_efficiency.py", "efficiency_adapter_sha256"),
        ("premise_compat.py", "pathways_adapter_sha256"),
    ]:
        if file_hash(run.parents[1] / "software/dev" / name) != build[field]:
            return False
    package = TESTED_RUN / f"{stem}.zip"
    if file_hash(package) != build["package_sha256"]:
        raise ValueError("Previously tested SPS1 package checksum changed")
    provenance = json.loads((TESTED_RUN / "software_provenance.json").read_text())
    premise_sources = provenance["repositories"]["premise"]["source_sha256"]
    if (
        not {"premise/inventory_imports.py", "premise/new_database.py"}
        <= premise_sources.keys()
    ):
        return False  # Older provenance cannot establish which imports/cache were used.
    for relative, expected in premise_sources.items():
        if file_hash(run.parents[1] / "software" / relative) != expected:
            return False
    for suffix in [".zip", ".efficiencies.json"]:
        shutil.copy2(TESTED_RUN / f"{stem}{suffix}", run / f"{stem}{suffix}")
    for name in ["build.log", "unlinked.log"]:
        shutil.copy2(TESTED_RUN / name, run / name)
    shutil.copytree(
        TESTED_RUN / "export/change reports",
        run / "export/change reports",
        dirs_exist_ok=True,
    )
    build.update(
        resources=plan["resources"],
        datapackage=plan["datapackage"],
        output_dir=str(run),
        package=str(run / f"{stem}.zip"),
        reused_from=str(package),
    )
    write_json(run / f"{stem}.build.json", build)
    return True


class InventoryImportFailure(RuntimeError):
    """The worker cache contains inventories with dropped biosphere flows."""


def check_inventory_import(run, cache):
    failures = [
        line
        for line in (run / "build.log").read_text().splitlines()
        if re.search(
            r"Could not find a biosphere flow|Cannot find the biosphere flow", line
        )
    ]
    if failures:
        cache.mkdir(parents=True, exist_ok=True)
        write_json(
            cache / "IMPORT_FAILURES.json", {"scenario": run.name, "messages": failures}
        )
        raise InventoryImportFailure(
            f"Inventory import dropped biosphere flows; worker cache is quarantined: {cache}"
        )


def run_batch(output, state, workers, retry_failed, scenarios=None):
    key = resolve_key(None)
    verify_completed(output, state)
    selected = set(state["scenarios"] if scenarios is None else scenarios)
    unknown = selected - state["scenarios"].keys()
    if unknown:
        raise ValueError(f"Unknown scenarios: {sorted(unknown)}")
    failed_caches = list((output / "worker_caches").glob("*/IMPORT_FAILURES.json"))
    if failed_caches:
        raise InventoryImportFailure(
            f"Previously failed imports must be resolved in a fresh batch: {failed_caches}"
        )
    for scenario, entry in state["scenarios"].items():
        if entry.get("child_pid"):
            try:
                os.kill(entry["child_pid"], 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError(
                    f"Previous worker for {scenario} is still running (PID {entry['child_pid']}); refusing a concurrent rebuild"
                )
    lock = threading.RLock()
    pending = queue.Queue()
    for scenario, entry in state["scenarios"].items():
        if (
            scenario in selected
            and entry["status"] != "complete"
            and (entry["status"] != "failed" or retry_failed)
        ):
            pending.put(scenario)
    state.update(
        status="running", coordinator_pid=os.getpid(), workers=workers, resumed_at=now()
    )
    coordinator_hash = file_hash(__file__)
    coordinator = output / "coordinators" / f"{coordinator_hash}.py"
    coordinator.parent.mkdir(exist_ok=True)
    if not coordinator.exists():
        shutil.copy2(__file__, coordinator)
    state.setdefault("coordinator_runs", []).append(
        {
            "started_at": now(),
            "source": str(coordinator),
            "sha256": coordinator_hash,
            "selected_scenarios": sorted(selected),
        }
    )

    def update(scenario=None, **values):
        with lock:
            if scenario:
                state["scenarios"][scenario].update(values)
            else:
                state.update(values)
            counts = {
                label: sum(e["status"] == label for e in state["scenarios"].values())
                for label in [
                    "complete",
                    "failed",
                    "pending",
                    "building",
                    "validating",
                    "coverage",
                    "nox",
                ]
            }
            state["counts"] = counts
            state["updated_at"] = now()
            write_json(output / "progress.json", state)
            rows = [
                f"Batch status: **{state['status']}**. Completed **{counts['complete']}/{len(state['scenarios'])}**; failed **{counts['failed']}**. Updated {state['updated_at']}.\n",
                "All packages use frozen ecoinvent 3.12 / STEM inputs and cover 2020–2050 in five-year steps. Completed ZIPs are in `packages/`.\n",
                "| Scenario | Status | Output or log |",
                "| --- | --- | --- |",
            ]
            for name, entry in state["scenarios"].items():
                path = (
                    entry.get("package")
                    or entry.get("log")
                    or str(output / "scenarios" / name / "build.log")
                )
                rows.append(f"| {name} | {entry['status']} | [Open]({path}) |")
            temporary = output / "STATUS.md.tmp"
            temporary.write_text("\n".join(rows) + "\n")
            temporary.replace(output / "STATUS.md")
            if scenario:
                print(
                    f"{now()} {scenario}: {values.get('status', state['scenarios'][scenario]['status'])}; {counts['complete']}/{len(state['scenarios'])} complete",
                    flush=True,
                )

    update()

    def worker(worker_number):
        environment = os.environ.copy()
        environment.update(
            PREMISE_KEY=key,
            PYTHONPATH=str(output / "software"),
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
        )
        while True:
            try:
                scenario = pending.get_nowait()
            except queue.Empty:
                return
            run = output / "scenarios" / scenario
            run.mkdir(parents=True, exist_ok=True)
            cache = output / "worker_caches" / str(worker_number)
            write_json(
                run / "variables.yaml",
                {"USER_DATA_BASE_DIR": str(cache)},
            )
            scripts = output / "software/dev"
            stem = (
                f"{state['plan']['model']}-{state['plan']['pathway']}-stem-{scenario}"
            )
            package = run / f"{stem}.zip"
            started = time.monotonic()
            update(
                scenario,
                status="building",
                worker=worker_number,
                started_at=now(),
                attempts=state["scenarios"][scenario]["attempts"] + 1,
            )

            def execute(script, arguments, log_name):
                with (run / log_name).open("w") as log:
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            str(scripts / script),
                            *map(str, arguments),
                        ],
                        cwd=run,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    update(scenario, child_pid=child.pid, log=str(run / log_name))
                    code = child.wait()
                if script == "create_data_packages.py":
                    check_inventory_import(run, cache)
                if code:
                    raise RuntimeError(f"{script} exited {code}; see {run / log_name}")

            try:
                reused = scenario == "SPS1_bas0" and reuse_tested(run, state["plan"])
                if not reused:
                    execute(
                        "create_data_packages.py",
                        [
                            "--datapackage",
                            output / "inputs/datapackage.json",
                            "--output-dir",
                            run,
                            "--scenarios",
                            scenario,
                        ],
                        "build.log",
                    )
                check_inventory_import(run, cache)
                update(scenario, status="validating", reused_tested_package=reused)
                execute("validate_stem_build.py", [package], "validation.log")
                update(scenario, status="coverage")
                execute("validate_package_coverage.py", [package], "coverage.log")
                update(scenario, status="nox")
                matrix_dir = (
                    TESTED_RUN / "pathways_temp" if reused else run / "pathways_temp"
                )
                execute(
                    "audit_gains_car_emissions.py",
                    [
                        output / "inputs/nox_baseline_2020",
                        matrix_dir,
                        "--output",
                        run / "gains_car_audit.json",
                    ],
                    "gains_car_audit.log",
                )
                validation = json.loads(
                    package.with_suffix(".validation.json").read_text()
                )
                destination = output / "packages" / package.name
                temporary = destination.with_suffix(".zip.tmp")
                shutil.copy2(package, temporary)
                temporary.replace(destination)
                for suffix in [
                    ".build.json",
                    ".validation.json",
                    ".coverage.json",
                    ".audit.json",
                    ".efficiencies.json",
                ]:
                    shutil.copy2(
                        package.with_suffix(suffix), destination.with_suffix(suffix)
                    )
                update(
                    scenario,
                    status="complete",
                    package=str(destination),
                    package_sha256=file_hash(destination),
                    finished_at=now(),
                    elapsed_seconds=round(time.monotonic() - started, 2),
                    child_pid=None,
                    validation_warnings=validation["findings_by_rule"],
                    missing_efficiency_targets=validation["missing_efficiency_targets"],
                )
            except Exception as error:
                update(
                    scenario,
                    status="failed",
                    error=f"{type(error).__name__}: {error}",
                    child_pid=None,
                    finished_at=now(),
                )
                if isinstance(error, InventoryImportFailure):
                    return  # Further scenarios must not reuse this worker's cache.
            finally:
                pending.task_done()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(worker, range(workers)))
    complete = all(
        entry["status"] == "complete" for entry in state["scenarios"].values()
    )
    selected_complete = all(
        state["scenarios"][s]["status"] == "complete" for s in selected
    )
    status = (
        "complete" if complete else ("partial" if selected_complete else "incomplete")
    )
    update(status=status, finished_at=now())
    return 0 if selected_complete else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "dev/data_packages_ei312_20260928"
    )
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=3)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        help="Build only these scenarios; a later run can resume the rest.",
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".batch.lock").open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("This batch already has a running coordinator")
        state = prepare(output)
        if args.prepare_only:
            print(f"Prepared {len(state['scenarios'])} scenarios in {output}")
            return 0
        return run_batch(output, state, args.workers, args.retry_failed, args.scenarios)


if __name__ == "__main__":
    raise SystemExit(main())
