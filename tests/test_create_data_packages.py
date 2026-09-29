import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from dev import create_data_packages as script


@pytest.fixture
def args(tmp_path):
    (tmp_path / "scenario_data.csv").write_text(
        "model,scenario,region,variables,unit,2020,2050\n"
        "STEM,SPS1_bas0,CH,Electricity generation,PJ/yr.,1,2\n"
        "STEM,SPS4_bas0,CH,Electricity generation,PJ/yr.,3,4\n"
    )
    (tmp_path / "config.yaml").write_text("{}\n")
    (tmp_path / "inventory.xlsx").write_bytes(b"fixture")
    descriptor = tmp_path / "datapackage.json"
    descriptor.write_text(
        json.dumps(
            {
                "scenarios": ["SPS1_bas0", "SPS4_bas0"],
                "resources": [
                    {"name": "scenario_data", "path": "scenario_data.csv"},
                    {"name": "config", "path": "config.yaml"},
                    {"name": "inventories", "path": "inventory.xlsx"},
                ],
            }
        )
    )
    return script.parse_args(
        ["--datapackage", str(descriptor), "--output-dir", str(tmp_path / "outputs")]
    )


def test_dry_run_accepts_interpolation_without_loading_brightway(args, monkeypatch):
    monkeypatch.setitem(sys.modules, "bw2data", None)
    monkeypatch.setitem(sys.modules, "premise", None)
    result = script.main(
        [
            "--datapackage",
            str(args.datapackage),
            "--output-dir",
            str(args.output_dir),
            "--dry-run",
        ]
    )
    assert result == 0
    assert not args.output_dir.exists()
    plan = script.build_plan(args)
    assert plan["scenarios"] == ["SPS1_bas0", "SPS4_bas0"]
    assert 2045 in plan["interpolated_years"]


@pytest.mark.parametrize("year", [2015, 2055])
def test_years_outside_stem_horizon_are_rejected(args, year):
    args.years = [year]
    with pytest.raises(ValueError, match="input year range"):
        script.build_plan(args)


def test_unknown_or_empty_scenario_data_is_rejected(args):
    args.scenarios = ["SPS1"]
    with pytest.raises(ValueError, match="Unknown scenarios"):
        script.build_plan(args)
    args.scenarios = ["SPS1_bas0"]
    (args.datapackage.parent / "scenario_data.csv").write_text(
        "model,scenario,region,variables,unit,2020,2050\n"
    )
    with pytest.raises(ValueError, match="No input rows"):
        script.build_plan(args)


def test_nonfinite_inputs_are_rejected(args):
    source = args.datapackage.parent / "scenario_data.csv"
    source.write_text(source.read_text().replace(",1,2", ",1,nan"))
    with pytest.raises(ValueError, match="Non-finite input"):
        script.build_plan(args)


def test_configuration_variable_whitespace_mismatch_is_rejected(args):
    (args.datapackage.parent / "config.yaml").write_text(
        "production pathways:\n  electricity:\n    production volume:\n"
        '      variable: "Electricity generation "\n'
    )
    with pytest.raises(ValueError, match="Configuration variables missing"):
        script.build_plan(args)


def test_key_precedence_and_notebook_is_never_executed(tmp_path, monkeypatch):
    notebook = tmp_path / "notebook.ipynb"
    notebook.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": [
                            'raise RuntimeError("must not execute")\n',
                            'ndb = PathwaysDataPackage(key="fixture-notebook-key")\n',
                        ],
                    }
                ]
            }
        )
    )
    monkeypatch.delenv("PREMISE_KEY", raising=False)
    assert script.resolve_key(None, notebook) == "fixture-notebook-key"
    monkeypatch.setenv("PREMISE_KEY", "fixture-environment-key")
    assert script.resolve_key(None, notebook) == "fixture-environment-key"
    assert script.resolve_key("fixture-cli-key", notebook) == "fixture-cli-key"


def test_build_uses_312_and_absolute_paths_and_records_provenance(args, monkeypatch):
    from contextlib import nullcontext
    from dev import stem_efficiency
    from dev import premise_compat

    monkeypatch.setattr(
        stem_efficiency, "absolute_stem_efficiencies", lambda *args: nullcontext()
    )
    monkeypatch.setattr(
        premise_compat, "preserve_pathways_mappings", lambda: nullcontext()
    )
    calls, project_changes = [], []

    class Projects:
        def __contains__(self, name):
            return name == "ecoinvent-3.12-cutoff"

        def set_current(self, name):
            project_changes.append(name)

    class Builder:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def create_datapackage(self, name, contributors):
            assert Path.cwd() == args.output_dir
            (Path.cwd() / f"{name}.zip").write_bytes(b"fixture-output")

    monkeypatch.setitem(
        sys.modules,
        "bw2data",
        SimpleNamespace(
            projects=Projects(),
            databases={"ecoinvent-3.12-cutoff", "ecoinvent-3.12-biosphere"},
        ),
    )
    monkeypatch.setitem(
        sys.modules, "datapackage", SimpleNamespace(Package=lambda p: p)
    )
    monkeypatch.setitem(
        sys.modules,
        "premise",
        SimpleNamespace(PathwaysDataPackage=Builder, __version__="fixture"),
    )
    original_cwd = Path.cwd()
    script.run_build(args, script.build_plan(args), "fixture-key")
    assert Path.cwd() == original_cwd
    assert project_changes == ["ecoinvent-3.12-cutoff"]
    assert len(calls) == 2
    for call in calls:
        assert call["source_db"] == "ecoinvent-3.12-cutoff"
        assert call["source_version"] == "3.12"
        assert call["biosphere_name"] == "ecoinvent-3.12-biosphere"
        assert call["use_absolute_efficiency"] is True
        assert call["key"] == "fixture-key"
        assert call["years"] == script.DEFAULT_YEARS
        assert call["scenarios"][0]["pathway"] == "SSP2-PkBudg1000"
        assert call["scenarios"][0]["external scenarios"][0]["data"] == str(
            args.datapackage
        )
    manifests = list(args.output_dir.glob("*.build.json"))
    assert len(manifests) == 2
    for path in manifests:
        assert "fixture-key" not in path.read_text()
        manifest = json.loads(path.read_text())
        assert manifest["package_sha256"] == script.file_hash(manifest["package"])
        assert manifest["resources"]["scenario_data"]["sha256"] == script.file_hash(
            args.datapackage.parent / "scenario_data.csv"
        )
