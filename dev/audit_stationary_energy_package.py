"""Check all Swiss endpoints and explicitly corrected heat-provider locations."""

import argparse
import json
from pathlib import Path
from zipfile import ZipFile
import numpy as np
import openpyxl
import pandas as pd
import yaml


def audit(package, workbook):
    proof = []
    for sheet in openpyxl.load_workbook(workbook, read_only=True, data_only=True):
        for row in sheet.values:
            if row[0] == "Swiss heat provider routing":
                proof.extend(json.loads(row[1]))
    assert len(proof) == 5
    checks, endpoints = [], 0
    with ZipFile(package) as z:
        mapping = yaml.safe_load(z.read("mapping/mapping.yaml"))
        for member in [n for n in z.namelist() if n.endswith("/A_matrix.csv")]:
            year = int(member.split("/")[-2])
            a = pd.read_csv(z.open(member), sep=";", float_precision="round_trip")
            nodes = pd.read_csv(
                z.open(member.replace("A_matrix.csv", "A_matrix_index.csv")), sep=";"
            )

            def find(name, product):
                rows = nodes[
                    nodes.name.eq(name)
                    & nodes["reference product"].eq(product)
                    & nodes.unit.eq("megajoule")
                    & nodes.location.eq("CH")
                ]
                assert len(rows) == 1, (year, name, "missing unique Swiss endpoint")
                return rows["index"].item()

            count = 0
            for variable, spec in mapping.items():
                if variable.startswith("EXT - 0 - FE_"):
                    for ds in spec["dataset"]:
                        find(ds["name"], ds["reference product"])
                    count += 1
            assert count == 101
            endpoints += count
            for record in proof:
                root = find(record["wrapper"], record["wrapper_product"])
                supplier = find(record["supplier"], record["product"])
                actual = a[
                    a["index of activity"].eq(root)
                    & a["index of product"].eq(supplier)
                    & a.flip.eq(1)
                ].value.sum()
                checks.append(
                    {
                        "year": year,
                        "wrapper": record["wrapper"],
                        "supplier": record["supplier"],
                        "expected": record["amount"],
                        "actual": actual,
                        "passed": bool(
                            np.isclose(actual, record["amount"], rtol=2e-6, atol=1e-14)
                        ),
                    }
                )
    frame = pd.DataFrame(checks)
    frame.to_csv(package.with_suffix(".stationary_routing_checks.csv"), index=False)
    assert not frame.empty and frame.passed.all(), frame[~frame.passed].to_dict(
        "records"
    )
    summary = {
        "passed": True,
        "Swiss endpoint checks": endpoints,
        "heat-provider checks": len(frame),
        "qualification": "Supplier locations and retained heat coefficients checked; technology and allocation proxies remain explicit.",
    }
    package.with_suffix(".stationary_routing_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--workbook", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.package, args.workbook)))
