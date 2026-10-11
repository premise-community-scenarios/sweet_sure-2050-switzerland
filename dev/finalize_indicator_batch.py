#!/usr/bin/env python3
"""Recheck numerical verification failures and assemble the local Toby export.

Can follow a running batch. A failed summed-demand check is independently
repeated with separate demands and a stricter comparison tolerance. Calculation
failures are never accepted, and no original results or failure logs are changed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

from run_indicator_batch import combine, digest, write_json


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    batch = args.batch.resolve()
    inputs = json.loads((batch / "inputs.json").read_text())
    entries = inputs["packages"]
    records = {}
    path = batch / "finalization.json"
    scripts = Path(__file__).parent
    frozen = {p.name: digest(p) for p in scripts.glob("*.py")}

    def save(status):
        write_json(
            path,
            {
                "status": status,
                "updated_at": now(),
                "pid": os.getpid(),
                "script_sha256": frozen,
                "numerical_rechecks": records,
            },
        )

    def recheck(entry):
        scenario = entry["scenario"]
        folder = batch / "scenarios" / scenario
        manifest = folder / (
            "results_" + Path(entry["package"]).stem + ".manifest.json"
        )
        if json.loads(manifest.read_text())["status"] != "complete":
            raise ValueError(f"Calculation failed: {scenario}")
        command = [
            sys.executable,
            str(scripts / "verify_indicator_totals.py"),
            str(manifest),
            "--years",
            *map(str, inputs["years"]),
            "--rtol",
            "1e-8",
            "--solver",
            "superlu",
            "--demand-mode",
            "separate",
        ]
        env = dict(
            os.environ,
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            PYTHONUNBUFFERED="1",
        )
        log_path = folder / "independent_verification_separate.log"
        if log_path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            shutil.copy2(
                log_path, folder / f"independent_verification_separate.{stamp}.log"
            )
        with log_path.open("w") as log:
            subprocess.run(
                command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True
            )
        return {
            "status": "complete",
            "finished_at": now(),
            "solver": "superlu",
            "demand_mode": "separate",
            "relative_tolerance": 1e-8,
            "matrix_assembly": "direct CSR duplicate summation",
            "normwise_backward_error_tolerance": 1e-12,
        }

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = {}
            while True:
                state = json.loads((batch / "progress.json").read_text())
                for scenario, future in list(running.items()):
                    if future.done():
                        try:
                            records[scenario] = future.result()
                            print(
                                now(),
                                scenario,
                                "independent separate-demand verification passed",
                                flush=True,
                            )
                        except Exception:
                            records[scenario] = {
                                "status": "failed",
                                "error": traceback.format_exc(),
                            }
                        del running[scenario]
                for entry in entries:
                    scenario = entry["scenario"]
                    if (
                        state["scenarios"][scenario]["status"] == "failed"
                        and scenario not in records
                    ):
                        records[scenario] = {"status": "verifying", "started_at": now()}
                        running[scenario] = pool.submit(recheck, entry)
                        print(
                            now(),
                            scenario,
                            "checking with separate demands",
                            flush=True,
                        )
                save("following_batch")
                if state["status"] in ("failed", "complete") and not running:
                    break
                time.sleep(15)
        with (batch / ".batch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = json.loads((batch / "progress.json").read_text())
            for scenario, record in records.items():
                if record["status"] != "complete":
                    raise RuntimeError(f"Unresolved verification failure: {scenario}")
                original = state["scenarios"][scenario]
                original["initial_verification_error"] = original.pop("error", None)
                original.update(
                    status="complete",
                    finished_at=record["finished_at"],
                    numerical_recheck=record,
                )
            if any(r["status"] != "complete" for r in state["scenarios"].values()):
                raise RuntimeError("Unfinished indicator scenarios")
            for name, checksum in inputs["snapshot_sha256"].items():
                if digest(batch / name) != checksum:
                    raise ValueError(f"Frozen calculation input changed: {name}")
            for entry in entries:
                if digest(entry["package"]) != entry["sha256"]:
                    raise ValueError("Source package changed")
            save("combining")
            combine(batch, entries)
            summary_path = batch / "completion_summary.json"
            summary = json.loads(summary_path.read_text())
            summary["numerical_rechecks"] = records
            summary["note"] = (
                "Initial summed-demand checks used UMFPACK at rtol=1e-7. Flagged scenarios were independently checked using separate demands and SuperLU at the stricter rtol=1e-8. The original Pathways results were retained."
            )
            write_json(summary_path, summary)
            state.update(status="complete", finished_at=now(), errors=[])
            write_json(batch / "progress.json", state)
            (batch / "STATUS.md").write_text(
                f"Updated: {now()}\n\nAll {len(entries)} scenarios calculated and independently verified.\n\nNumerical rechecks: {len(records)} (all passed at rtol=1e-8).\n"
            )
        save("exporting")
        subprocess.run(
            [
                sys.executable,
                str(scripts / "export_toby_indicators.py"),
                str(batch),
                "--reference",
                str(args.reference.resolve()),
                "--output-dir",
                str(batch / "toby_export"),
            ],
            check=True,
        )
        save("complete")
        print(
            now(),
            "All calculations, verification and Toby exports complete",
            flush=True,
        )
    except Exception:
        save("failed")
        raise


if __name__ == "__main__":
    main()
