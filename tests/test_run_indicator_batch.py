"""Require a complete, checksum-verified build before calculating indicators."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from run_indicator_batch import YEARS, digest, read_build_packages, combine


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


@pytest.mark.parametrize("duplicate_check", [False, True])
def test_combines_81_methods_including_zero_results_only_with_complete_checks(
    tmp_path, duplicate_check
):
    import pandas as pd

    methods = {f"method {i}": [f"indicator {i}", "kilogram"] for i in range(81)}
    entries = []
    (tmp_path / "method_registry.json").write_text("{}")
    for scenario in [f"SPS{i}_bas0" for i in range(1, 5)]:
        package = tmp_path / f"{scenario}.zip"
        package.write_bytes(scenario.encode())
        entries.append(
            {"scenario": scenario, "package": str(package), "sha256": digest(package)}
        )
        folder = tmp_path / "scenarios" / scenario
        folder.mkdir(parents=True)
        stem = f"results_{scenario}"
        raw = folder / f"{stem}.gzip"
        raw.write_bytes(b"raw result checksum fixture")
        manifest = {
            "status": "complete",
            "variables": ["EXT - 0 - FE_services_heat"],
            "calculation": {
                "methods": list(methods),
                "package_sha256": digest(package),
            },
            "methods": methods,
            "raw_sha256": digest(raw),
            "validation": {"unlinked_variables": 0},
        }
        (folder / f"{stem}.manifest.json").write_text(json.dumps(manifest))
        records, checks = [], []
        for year in YEARS:
            for i, (method, (label, unit)) in enumerate(methods.items()):
                amounts = [0, 0, 0] if i == 0 else [100.0, -20.0, 10.0]
                for location, value in zip(["CH", "EU wo CH", "RoW"], amounts):
                    records.append(
                        dict(
                            model="remind",
                            scenario=scenario,
                            region="CH",
                            year=year,
                            variable=manifest["variables"][0],
                            sector="services",
                            impact_category=label,
                            unit=unit,
                            location=location,
                            value=value,
                        )
                    )
                checks.append(
                    dict(
                        year=year,
                        method=method,
                        pathways_score=sum(amounts),
                        relative_difference=0,
                    )
                )
        if duplicate_check:
            checks[-1] = checks[0]
        (folder / f"{stem}_independent_validation.json").write_text(
            json.dumps({"passed": True, "checks": checks})
        )
        (folder / "warnings.json").write_text(
            json.dumps({"python_warnings": [], "logging_warnings": []})
        )
        pd.DataFrame(records).to_parquet(
            folder / f"{stem}_indicators.parquet", index=False
        )
    if duplicate_check:
        with pytest.raises(ValueError, match="Incomplete independent verification"):
            combine(tmp_path, entries)
    else:
        combine(tmp_path, entries)
        summary = json.loads((tmp_path / "completion_summary.json").read_text())
        assert summary["indicators"] == 81 and summary["independent_checks"] == 2268
        totals = pd.read_csv(tmp_path / "indicator_totals.csv")
        assert len(totals) == 2268
        assert totals.loc[totals.impact_category.eq("indicator 0"), "value"].eq(0).all()
