import json
from pathlib import Path
import threading
import time

import pytest

from dev import build_all_data_packages as batch


def test_resume_rejects_modified_frozen_input(tmp_path):
    resource = tmp_path / "scenario.csv"
    resource.write_text("original")
    state = {
        "plan": {
            "resources": {
                "scenario_data": {
                    "path": str(resource),
                    "sha256": batch.file_hash(resource),
                }
            }
        }
    }
    batch.write_json(tmp_path / "progress.json", state)
    assert batch.prepare(tmp_path) == state
    resource.write_text("changed")
    with pytest.raises(ValueError, match="Frozen batch inputs changed"):
        batch.prepare(tmp_path)


def test_completed_zip_must_retain_its_checksum(tmp_path):
    package = tmp_path / "test.zip"
    package.write_bytes(b"verified output")
    state = {
        "software_hashes": {},
        "scenarios": {
            "one": {
                "status": "complete",
                "package": str(package),
                "package_sha256": batch.file_hash(package),
            }
        },
    }
    batch.verify_completed(tmp_path, state)
    package.write_bytes(b"changed output")
    with pytest.raises(ValueError, match="Completed package changed"):
        batch.verify_completed(tmp_path, state)


def test_resume_cannot_change_the_scope_of_a_selected_batch(tmp_path):
    state = {"plan": {"scenarios": ["SPS1_bas0", "SPS2_bas0"], "resources": {}}}
    batch.write_json(tmp_path / "progress.json", state)
    assert batch.prepare(tmp_path, ["SPS2_bas0", "SPS1_bas0"]) == state
    with pytest.raises(ValueError, match="scenario selection changed"):
        batch.prepare(tmp_path, ["SPS1_bas0", "SPS4_bas0"])


def test_workers_isolate_caches_and_retry_only_failed_scenario(tmp_path, monkeypatch):
    (tmp_path / "packages").mkdir()
    state = {
        "plan": {
            "model": "remind",
            "pathway": "test",
            "resources": {
                "scenario_data": {"path": str(tmp_path / "scenario_data.csv")},
                "inventories": {"path": str(tmp_path / "inventories.xlsx")},
            },
        },
        "software_hashes": {},
        "scenarios": {
            name: {"status": "pending", "attempts": 0} for name in ["one", "two"]
        },
    }
    monkeypatch.setattr(batch, "resolve_key", lambda _: "fixture-key")
    calls, cache_directories = [], set()
    call_lock = threading.Lock()
    fail = {"one"}

    class Child:
        def __init__(self, argv, cwd, env, stdout, stderr):
            self.pid = 999999
            self.scenario = Path(cwd).name
            self.script = Path(argv[2]).name
            with call_lock:
                calls.append((self.scenario, self.script))
                cache_directories.add(
                    json.loads((cwd / "variables.yaml").read_text())[
                        "USER_DATA_BASE_DIR"
                    ]
                )
            assert env["PREMISE_KEY"] == "fixture-key"
            assert env["PYTHONPATH"] == str(tmp_path / "software")
            if self.script == "create_data_packages.py":
                stem = f"remind-test-stem-{self.scenario}"
                (cwd / f"{stem}.zip").write_bytes(self.scenario.encode())
                for suffix in [
                    ".build.json",
                    ".validation.json",
                    ".coverage.json",
                    ".audit.json",
                    ".efficiencies.json",
                    ".heat_pumps.json",
                    ".heat_pump_validation.json",
                    ".wood_coproducts.json",
                    ".wood_coproduct_validation.json",
                    ".vehicle_energy.json",
                    ".vehicle_checks.csv",
                    ".vehicle_validation.json",
                    ".electric_boilers.json",
                    ".electric_boiler_checks.csv",
                    ".electric_boiler_validation.json",
                    ".aggregate_electric_checks.csv",
                    ".aggregate_electric_validation.json",
                    ".stationary_routing_checks.csv",
                    ".stationary_routing_validation.json",
                    ".motor_magnet_checks.csv",
                    ".motor_magnet_validation.json",
                ]:
                    batch.write_json(
                        cwd / f"{stem}{suffix}",
                        {"findings_by_rule": [], "missing_efficiency_targets": []},
                    )

        def wait(self):
            time.sleep(0.02)
            return (
                2
                if self.scenario in fail
                and self.script == "validate_package_coverage.py"
                else 0
            )

    monkeypatch.setattr(batch.subprocess, "Popen", Child)
    assert batch.run_batch(tmp_path, state, workers=2, retry_failed=False) == 1
    assert state["scenarios"]["one"]["status"] == "failed"
    assert state["scenarios"]["two"]["status"] == "complete"
    assert len(cache_directories) == 2
    assert not (tmp_path / "packages/remind-test-stem-one.zip").exists()
    assert (tmp_path / "packages/remind-test-stem-two.zip").read_bytes() == b"two"
    completed_calls = calls.count(("two", "create_data_packages.py"))
    fail.clear()
    assert batch.run_batch(tmp_path, state, workers=2, retry_failed=True) == 0
    assert calls.count(("two", "create_data_packages.py")) == completed_calls
    assert state["scenarios"]["one"]["attempts"] == 2
    assert state["counts"]["complete"] == 2
    assert "fixture-key" not in (tmp_path / "progress.json").read_text()


def test_resume_refuses_to_duplicate_a_live_child(tmp_path, monkeypatch):
    state = {
        "software_hashes": {},
        "scenarios": {"one": {"status": "building", "child_pid": 123}},
    }
    monkeypatch.setattr(batch, "resolve_key", lambda _: "fixture-key")
    monkeypatch.setattr(batch.os, "kill", lambda pid, signal: None)
    with pytest.raises(RuntimeError, match="still running"):
        batch.run_batch(tmp_path, state, workers=1, retry_failed=False)


def test_dropped_biosphere_flow_quarantines_cache_and_prevents_resume(
    tmp_path, monkeypatch
):
    run = tmp_path / "scenarios" / "one"
    run.mkdir(parents=True)
    cache = tmp_path / "worker_caches" / "0"
    (run / "build.log").write_text(
        "Could not find a biosphere flow for a resource. Flow ignored.\n"
    )
    with pytest.raises(batch.InventoryImportFailure, match="quarantined"):
        batch.check_inventory_import(run, cache)
    report = json.loads((cache / "IMPORT_FAILURES.json").read_text())
    assert report["scenario"] == "one"
    state = {"software_hashes": {}, "scenarios": {"one": {"status": "failed"}}}
    monkeypatch.setattr(batch, "resolve_key", lambda _: "fixture-key")
    with pytest.raises(batch.InventoryImportFailure, match="fresh batch"):
        batch.run_batch(tmp_path, state, workers=1, retry_failed=True)


def test_pruning_keeps_source_caches_and_only_removes_disposable_stores(tmp_path):
    cache = tmp_path / "worker_caches" / "0"
    checkpoint = cache / "cached_files" / ("a" * 32 + ".inventory-store")
    checkpoint.mkdir(parents=True)
    (checkpoint / "data").write_bytes(b"temporary")
    source = cache / "cache" / "source.pickle"
    source.parent.mkdir()
    source.write_bytes(b"keep source")
    baseline = cache / "cached_files" / "validation-baseline.pkl"
    baseline.write_bytes(b"keep baseline")
    other = cache / "cached_files" / "named.inventory-store"
    other.mkdir()
    removed = batch.prune_worker_checkpoints(tmp_path, cache)
    assert len(removed) == 1 and removed[0]["bytes"] == 9
    assert not checkpoint.exists()
    assert (
        source.read_bytes() == b"keep source" and baseline.exists() and other.exists()
    )


def test_pruning_refuses_external_symlink_and_quarantined_caches(tmp_path):
    cache = tmp_path / "worker_caches" / "0"
    cache.mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()
    (cache / "cached_files").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        batch.prune_worker_checkpoints(tmp_path, cache)
    with pytest.raises(ValueError, match="outside"):
        batch.prune_worker_checkpoints(tmp_path, outside)
    (cache / "IMPORT_FAILURES.json").write_text("{}")
    with pytest.raises(batch.InventoryImportFailure, match="quarantined"):
        batch.prune_worker_checkpoints(tmp_path, cache)
