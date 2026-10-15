#!/usr/bin/env python3
"""Check static electricity equipment shares and actual exported coefficients."""

import argparse
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import openpyxl
import pandas as pd


def audit(package, workbook):
    records = []
    for sheet in openpyxl.load_workbook(workbook, read_only=True, data_only=True):
        for row in sheet.values:
            if row[0] == "aggregate electric equipment":
                records.append(json.loads(row[1]))
    assert len(records) == 3
    checks = []
    with ZipFile(package) as z:
        for member in [n for n in z.namelist() if n.endswith("/A_matrix.csv")]:
            year = int(member.split("/")[-2])
            a = pd.read_csv(z.open(member), sep=";")
            nodes = pd.read_csv(
                z.open(member.replace("A_matrix.csv", "A_matrix_index.csv")), sep=";"
            )
            for record in records:
                selected = nodes[
                    nodes.name.eq(record["name"])
                    & nodes.location.eq("CH")
                    & nodes.unit.eq("megajoule")
                ]
                assert len(selected) == 1, record["name"]
                inputs = a[
                    a["index of activity"].eq(selected["index"].item()) & a.flip.eq(1)
                ].merge(nodes, left_on="index of product", right_on="index")
                grid = inputs[
                    inputs["reference product"].eq("electricity, low voltage")
                    & inputs.unit.eq("kilowatt hour")
                ]
                assert (
                    len(grid) == 1
                    and "SPS" in grid.name.item()
                    and np.isclose(grid.value.item() * 3.6, 1, rtol=2e-6)
                )
                total = sum(record["energy_PJ"].values())
                for component in record["components"]:
                    e = component["exchange"]
                    expected = (
                        record["energy_PJ"][component["category"]]
                        / total
                        * component["per_MJ_of_category"]
                    )
                    actual = inputs[
                        inputs.name.eq(e["name"])
                        & inputs["reference product"].eq(e["product"])
                        & inputs.unit.eq(e["unit"])
                    ].value.sum()
                    checks.append(
                        dict(
                            year=year,
                            profile=record["id"],
                            category=component["category"],
                            expected=expected,
                            actual=actual,
                            passed=bool(
                                np.isclose(actual, expected, rtol=2e-6, atol=1e-14)
                            ),
                        )
                    )
    frame = pd.DataFrame(checks)
    frame.to_csv(package.with_suffix(".aggregate_electric_checks.csv"), index=False)
    assert not frame.empty and frame.passed.all()
    summary = {
        "passed": True,
        "checks": len(frame),
        "years": sorted(frame.year.unique().tolist()),
        "profiles": records,
        "qualification": "Confirms declared component quantities and energy shares; residual coverage, common-structure exclusions and technology proxies remain explicit.",
    }
    package.with_suffix(".aggregate_electric_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    return {"passed": True, "checks": len(frame)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--workbook", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.package, args.workbook)))
