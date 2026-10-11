"""Require a complete, checksum-verified build before calculating indicators."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from run_indicator_batch import YEARS, digest, read_build_packages


def make_build(tmp_path, legacy=False):
    package = tmp_path / "scenario.zip"
    package.write_bytes(b"test package")
    entry = {"scenario": "example", "package": str(package), "sha256": digest(package)}
    if legacy:
        path = tmp_path / "completion_summary.json"
        data = {
            "packages": [entry],
            "validated_packages": 1,
            "years_per_package": YEARS,
        }
    else:
        path = tmp_path / "progress.json"
        data = {
            "status": "complete",
            "plan": {"scenarios": ["example"], "years": YEARS},
            "scenarios": {
                "example": {
                    "status": "complete",
                    "package": str(package),
                    "package_sha256": entry["sha256"],
                }
            },
        }
    path.write_text(json.dumps(data))
    return path, data, entry


@pytest.mark.parametrize("legacy", [False, True])
def test_accepts_validated_build_formats(tmp_path, legacy):
    path, _, entry = make_build(tmp_path, legacy)
    assert read_build_packages(tmp_path) == (path, [entry])


@pytest.mark.parametrize("legacy", [False, True])
def test_rejects_package_changed_after_build(tmp_path, legacy):
    _, _, entry = make_build(tmp_path, legacy)
    Path(entry["package"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Changed package"):
        read_build_packages(tmp_path)


@pytest.mark.parametrize(
    "case", ["running", "failed_scenario", "missing_scenario", "wrong_years"]
)
def test_rejects_incomplete_current_build(tmp_path, case):
    path, data, _ = make_build(tmp_path)
    if case == "running":
        data["status"] = "running"
    elif case == "failed_scenario":
        data["scenarios"]["example"]["status"] = "failed"
    elif case == "missing_scenario":
        data["plan"]["scenarios"].append("missing")
    else:
        data["plan"]["years"] = [2020, 2050]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_build_packages(tmp_path)
