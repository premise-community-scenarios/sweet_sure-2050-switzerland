"""Reject unsupported changes to shared magnets and motor lithium allocations.

Compare exported coefficients with original Brightway inventory inputs, without
using the material transformation or its change report as the expected value.
This is a targeted recipe check, not a certification of all motor materials.
"""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
import yaml

MAGNET = "permanent magnet production, for electric motor"
MOTORS = {
    "electric motor production, vehicle",
    "electric motor production, vehicle (electric powertrain)",
    "electric motor production, for electric scooter",
}


def check_premise_source(root):
    """Fail before an expensive build when known unsafe upstream code is used."""
    folder = root / "premise"
    if not (folder / "vehicle_energy_use.py").is_file():
        raise ValueError("Premise is missing the energy-specific vehicle wrappers")
    rules = yaml.safe_load((folder / "data/metals/metal_products.yaml").read_text())
    for element in ("Lithium", "Neodymium", "Boron"):
        selected = [
            r
            for r in rules["rules"]
            if r["technology"] == "EV" and r["element"] == element and r["enabled"]
        ]
        if not selected or not all(r.get("preserve_reason") for r in selected):
            raise ValueError(
                f"Premise lacks the reviewed EV {element} preservation rule"
            )
    conversions = yaml.safe_load(
        (folder / "data/metals/technology_conversion_factors.yaml").read_text()
    )
    for name in MOTORS:
        selected = [
            r for r in conversions["conversions"] if r["activity"]["name"] == name
        ]
        if (
            len(selected) != 1
            or selected[0]["intensity_unit"] != "kg metal/kg motor"
            or selected[0]["factor"] != 1
        ):
            raise ValueError(
                f"Premise has an incompatible motor intensity conversion: {name}"
            )


def selected_amounts(name, inputs):
    """Aggregate provider splits by product; retain signed coefficients."""
    amounts = defaultdict(float)
    for product, unit, amount in inputs:
        if unit != "kilogram":
            continue
        if name == MAGNET and product in ("neodymium oxide", "boric oxide"):
            amounts[product] += amount
        if name in MOTORS:
            if product == "permanent magnet, for electric motor":
                amounts[product] += amount
            if product.lower().startswith("lithium"):
                amounts["all direct lithium products"] += amount
    if name == MAGNET:
        for product in ("neodymium oxide", "boric oxide"):
            amounts[product] += 0.0
    else:
        amounts["all direct lithium products"] += 0.0
        amounts["permanent magnet, for electric motor"] += 0.0
    return dict(amounts)


def source_reference(project, database):
    import bw2data as bd

    bd.projects.set_current(project)
    records = []
    for activity in bd.Database(database):
        if activity["name"] not in MOTORS | {MAGNET}:
            continue
        if activity["unit"] != "kilogram" or not activity[
            "reference product"
        ].startswith(("electric motor", "permanent magnet")):
            continue
        assert activity.rp_exchange()["amount"] == 1
        records.append(
            {
                "name": activity["name"],
                "product": activity["reference product"],
                "location": activity["location"],
                "unit": activity["unit"],
                "amounts": selected_amounts(
                    activity["name"],
                    [
                        (e.input["reference product"], e["unit"], e["amount"])
                        for e in activity.technosphere()
                    ],
                ),
            }
        )
    assert len(records) == 5 and {r["name"] for r in records} == MOTORS | {MAGNET}
    return {
        "project": project,
        "database": database,
        "basis": "original inventory per kg equipment",
        "records": sorted(records, key=lambda r: (r["name"], r["location"])),
    }


def compare(nodes, matrix, reference, year):
    rows = []
    for ref in reference["records"]:
        selected = nodes[
            nodes.name.eq(ref["name"])
            & nodes["reference product"].eq(ref["product"])
            & nodes.location.eq(ref["location"])
            & nodes.unit.eq(ref["unit"])
        ]
        assert len(selected) == 1, (ref["name"], ref["location"], year)
        root = selected["index"].item()
        column = matrix[matrix["index of activity"].eq(root)]
        production = column[column["index of product"].eq(root)]
        assert (
            len(production) == 1
            and production.flip.item() == 0
            and production.value.item() == 1
        )
        inputs = column[column["index of product"].ne(root)].merge(
            nodes, left_on="index of product", right_on="index"
        )
        # flip=1 is consumption; flip=0 is a signed substitution output.
        actual = selected_amounts(
            ref["name"],
            [
                (
                    r["reference product"],
                    r["unit"],
                    r["value"] * (1 if r["flip"] else -1),
                )
                for r in inputs.to_dict("records")
            ],
        )
        for product, expected in ref["amounts"].items():
            amount = actual.get(product, 0.0)
            rows.append(
                {
                    "year": year,
                    "name": ref["name"],
                    "location": ref["location"],
                    "input": product,
                    "expected_kg_per_kg": expected,
                    "actual_kg_per_kg": amount,
                    "passed": bool(np.isclose(amount, expected, rtol=2e-6, atol=1e-12)),
                }
            )
    return rows


def audit(package, reference_path):
    reference = json.loads(reference_path.read_text())
    rows = []
    with ZipFile(package) as z:
        for member in sorted(n for n in z.namelist() if n.endswith("/A_matrix.csv")):
            year = int(Path(member).parent.name)
            matrix = pd.read_csv(z.open(member), sep=";", usecols=[0, 1, 2, 10])
            nodes = pd.read_csv(
                z.open(member.replace("A_matrix.csv", "A_matrix_index.csv")), sep=";"
            )
            rows.extend(compare(nodes, matrix, reference, year))
    assert rows and len({r["year"] for r in rows}) == 7
    frame = pd.DataFrame(rows)
    frame.to_csv(package.with_suffix(".motor_magnet_checks.csv"), index=False)
    summary = {
        "checks": len(rows),
        "failures": int((~frame.passed).sum()),
        "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
        "scope": "Original Nd/B shared-magnet recipe, motor magnet mass, and absence of vehicle-level lithium overlays",
    }
    package.with_suffix(".motor_magnet_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    if summary["failures"]:
        raise AssertionError(
            f"Unsupported motor/magnet recipe changes: {summary['failures']}"
        )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.package, args.reference)))
