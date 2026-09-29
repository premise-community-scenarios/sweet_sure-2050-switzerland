"""Audit a built STEM ZIP against its inputs, PV shares, and efficiency records.

Usage: python dev/validate_stem_build.py path/to/package.zip
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import zipfile

import numpy as np
from openpyxl import load_workbook
import pandas as pd
import yaml

try:
    from dev.audit_pathways_package import audit
except ModuleNotFoundError:
    from audit_pathways_package import audit


def validate(path):
    manifest = json.loads(path.with_suffix(".build.json").read_text())
    for resource in manifest["resources"].values():
        digest = hashlib.sha256(Path(resource["path"]).read_bytes()).hexdigest()
        assert (
            digest == resource["sha256"]
        ), f"Input changed since build: {resource['path']}"
    config = yaml.safe_load(Path(manifest["resources"]["config"]["path"]).read_text())
    frame = pd.read_csv(manifest["resources"]["scenario_data"]["path"])
    data = frame[
        frame.scenario.eq(manifest["scenario"]) & frame.region.eq("CH")
    ].set_index("variables")
    input_years = sorted(int(c) for c in frame.columns if c.isdigit())

    def value(variable, year):
        values = data.loc[variable, [str(y) for y in input_years]].to_numpy(dtype=float)
        assert values.ndim == 1
        return float(np.interp(year, input_years, values))

    aliases = {}
    used = Counter()
    for pathway, settings in config["production pathways"].items():
        alias = settings["ecoinvent alias"]
        key = alias["name"], alias["reference product"]
        name = alias["name"] + (
            f"_{pathway}" if used[key] and not alias.get("new dataset") else ""
        )
        aliases[pathway] = name
        if not alias.get("new dataset"):
            used[key] += 1

    adjustments = json.loads(path.with_suffix(".efficiencies.json").read_text())
    efficiency_checks = []
    missing_efficiency_targets = []
    for year in manifest["years"]:
        for pathway, settings in config["production pathways"].items():
            for efficiency in settings.get("efficiency", []):
                target = value(efficiency["variable"], year)
                if not efficiency.get("absolute"):
                    continue
                if target == 0:
                    production = value(settings["production volume"]["variable"], year)
                    if production > 0:
                        missing_efficiency_targets.append(
                            {
                                "year": year,
                                "pathway": pathway,
                                "electricity_generation_pj_per_year": production,
                                "reported_efficiency": 0,
                                "action": "retained proxy; no usable STEM target",
                            }
                        )
                    continue
                matches = [
                    r
                    for r in adjustments
                    if r["year"] == year
                    and r["activity"].lower() == aliases[pathway].lower()
                    and r["location"] == "CH"
                    and efficiency["variable"] in r["variables"]
                ]
                assert len(matches) == 1, (year, pathway, matches)
                record = matches[0]
                assert np.isclose(
                    record["stem_target_efficiency"], target, rtol=1e-10, atol=0
                )
                assert np.isclose(
                    record["verified_physical_efficiency"], target, rtol=1e-9, atol=0
                )
                efficiency_checks.append(
                    {
                        "year": year,
                        "pathway": pathway,
                        "target": target,
                        "verified_at_adjustment": record[
                            "verified_physical_efficiency"
                        ],
                        "basis": record["basis"],
                    }
                )

    matrix_audit = audit(path)
    assert sorted(r["year"] for r in matrix_audit["years"]) == manifest["years"]
    path.with_suffix(".audit.json").write_text(
        json.dumps(matrix_audit, indent=2) + "\n"
    )
    market = next(
        m
        for m in config["markets"]
        if m["name"] == "market for electricity, high voltage (SPS)"
    )
    pv_checks = []
    build_log = (path.parent / "build.log").read_text()
    omitted_market_names = set(
        re.findall(r"No suppliers found for (.+?) in CH\. No market created", build_log)
    )
    zero_supply_omissions = []
    with zipfile.ZipFile(path) as archive:
        for record in matrix_audit["years"]:
            directory, year = record["matrix_directory"], record["year"]
            with archive.open(f"{directory}/A_matrix_index.csv") as stream:
                ai = pd.read_csv(stream, sep=";")
            with archive.open(f"{directory}/A_matrix.csv") as stream:
                a = pd.read_csv(
                    stream,
                    sep=";",
                    usecols=["index of activity", "index of product", "value", "flip"],
                )
            for omitted_name in sorted(omitted_market_names):
                omitted = next(
                    m for m in config["markets"] if m["name"] == omitted_name
                )
                supply = sum(
                    value(
                        config["production pathways"][p]["production volume"][
                            "variable"
                        ],
                        year,
                    )
                    for p in omitted["includes"]
                )
                if supply == 0:
                    assert not (
                        ai["name"].eq(omitted_name) & ai.location.eq("CH")
                    ).any()
                    zero_supply_omissions.append(
                        {
                            "year": year,
                            "market": omitted_name,
                            "stem_supply": 0,
                            "dangling_links": 0,
                        }
                    )
            market_id = ai.loc[
                ai["name"].eq(market["name"]) & ai.location.eq("CH"), "index"
            ].item()
            inputs = a[a["index of activity"].eq(market_id) & a.flip.eq(1)].merge(
                ai, left_on="index of product", right_on="index"
            )
            total = sum(
                value(
                    config["production pathways"][p]["production volume"]["variable"],
                    year,
                )
                for p in market["includes"]
            )
            for pathway, settings in config["production pathways"].items():
                if not pathway.startswith("pv "):
                    continue
                expected = (
                    value(settings["production volume"]["variable"], year) / total
                )
                supplier = inputs[
                    inputs["name"].str.lower().eq(aliases[pathway].lower())
                    & inputs["reference product"].eq(
                        settings["ecoinvent alias"]["reference product"]
                    )
                    & inputs.location.eq("CH")
                ]
                actual = float(supplier.value.sum())
                assert len(supplier) == (1 if expected > 0 else 0), (
                    year,
                    pathway,
                    supplier,
                )
                assert np.isclose(actual, expected, rtol=1e-9, atol=1e-12), (
                    year,
                    pathway,
                    expected,
                    actual,
                )
                pv_checks.append(
                    {
                        "year": year,
                        "pathway": pathway,
                        "supplier": aliases[pathway],
                        "expected_share": expected,
                        "exported_share": actual,
                    }
                )

    unlinked = path.parent / "unlinked.log"
    assert (
        unlinked.is_file() and unlinked.stat().st_size == 0
    ), "Inventory import reported unlinked exchanges"
    log = build_log
    failures = re.findall(
        r"Traceback|Error processing dataset|Cannot find the biosphere flow|Could not find a biosphere flow|No candidate found for|Could not find unit for|KeyError for",
        log,
    )
    assert not failures, failures
    findings = []
    reports = sorted(path.parent.glob("export/change reports/*.xlsx"))
    assert reports, "Missing premise validation report"
    for report in reports:
        workbook = load_workbook(report, read_only=True, data_only=True)
        rows = iter(workbook["Validation Findings"].values)
        header = next(rows)
        findings.extend(dict(zip(header, row)) for row in rows if row[0] is not None)
        workbook.close()
    counts = Counter((r["severity"], r["rule ID"]) for r in findings)
    assert not any(
        r["severity"] == "error" for r in findings
    ), "premise validation errors"
    result = {
        "package": str(path.resolve()),
        "scenario": manifest["scenario"],
        "years": manifest["years"],
        "input_hashes_match": True,
        "unlinked_exchanges": 0,
        "matrix_checks_passed": True,
        "efficiency_adjustment_checks": efficiency_checks,
        "missing_efficiency_targets": missing_efficiency_targets,
        "pv_share_checks": pv_checks,
        "zero_supply_market_omissions": zero_supply_omissions,
        "premise_validation_findings": findings,
        "findings_by_rule": [
            {"severity": s, "rule": r, "count": n}
            for (s, r), n in sorted(counts.items())
        ],
        "warning_free": not findings
        and not missing_efficiency_targets
        and not re.search(r"Warning|warning", log),
        "note": "Efficiency checks verify each build-time adjustment against the STEM input. Waste combustion uses the declared inventory baseline; this is not an independent measurement of waste fuel. Matrix checks test linking and solvability, not all LCA modelling assumptions.",
    }
    output = path.with_suffix(".validation.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "efficiency_checks": len(efficiency_checks),
                "pv_share_checks": len(pv_checks),
                "findings_by_rule": result["findings_by_rule"],
                "warning_free": result["warning_free"],
                "report": str(output),
            },
            indent=2,
        )
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    validate(parser.parse_args().package)
