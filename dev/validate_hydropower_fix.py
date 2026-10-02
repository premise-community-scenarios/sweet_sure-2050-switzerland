"""Compare rebuilt hydropower providers with their previous exported inventories."""

import argparse
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd


def read_matrices(package, year):
    with ZipFile(package) as archive:
        (directory,) = [
            str(Path(name).parent)
            for name in archive.namelist()
            if name.endswith(f"/{year}/A_matrix.csv")
        ]
        return {
            name: pd.read_csv(
                archive.open(f"{directory}/{name}.csv"),
                sep=";",
                float_precision="round_trip",
            )
            for name in ("A_matrix", "B_matrix", "A_matrix_index", "B_matrix_index")
        }


def vector(data, matrix):
    """Align coefficients by inventory identity rather than exported row order."""
    activities = data["A_matrix_index"].set_index("index")
    activity_keys = activities[["name", "reference product", "unit", "location"]].apply(
        tuple, axis=1
    )
    if matrix == "A_matrix":
        suppliers = activity_keys
        supplier_index = "index of product"
    else:
        suppliers = data["B_matrix_index"].fillna("").set_index("index")
        suppliers = suppliers[["name", "compartment", "subcompartment", "unit"]].apply(
            tuple, axis=1
        )
        supplier_index = "index of biosphere flow"
    source = data[matrix]
    result = pd.DataFrame(
        {
            "activity": source["index of activity"].map(activity_keys),
            "supplier": source[supplier_index].map(suppliers),
            "flip": source["flip"] if matrix == "A_matrix" else 1,
            "value": source.value,
        }
    )
    assert not result.isna().any().any()
    return result.groupby(["activity", "supplier", "flip"]).value.sum()


def validate(before, after):
    manifest = json.loads(after.with_suffix(".build.json").read_text())
    records = json.loads(after.with_suffix(".efficiencies.json").read_text())
    results = []
    for year in manifest["years"]:
        (record,) = [
            r
            for r in records
            if r["year"] == year
            and r["basis"] == "explicitly selected electricity input/output"
        ]
        pump = (
            record["activity"],
            record["product"],
            "kilowatt hour",
            record["location"],
        )
        left, right = read_matrices(before, year), read_matrices(after, year)
        checks = []
        for matrix in ["A_matrix", "B_matrix"]:
            joined = pd.concat(
                [vector(left, matrix), vector(right, matrix)],
                axis=1,
                keys=["before", "after"],
            ).fillna(0)
            changed = ~np.isclose(joined.before, joined.after, rtol=1e-8, atol=1e-12)
            changes = joined.loc[changed].reset_index()
            changes.to_csv(
                after.with_suffix(f".{year}.{matrix}_changes.csv"), index=False
            )
            unexpected = []
            for _, row in changes.iterrows():
                activity, supplier = row.activity, row.supplier
                if matrix == "A_matrix":
                    allowed = (
                        activity == pump
                        and row.flip == 1
                        and supplier[1] == "electricity, high voltage"
                        and supplier[2] == "kilowatt hour"
                    )
                    if allowed:
                        assert np.isclose(
                            row.after, row.before * record["scaling_factor"], rtol=1e-7
                        )
                else:
                    allowed = (
                        activity[3] == "CH"
                        and activity[2] == "kilowatt hour"
                        and any(
                            activity[0].startswith(name)
                            for name in [
                                "electricity production, hydro, pumped storage",
                                "electricity production, hydro, reservoir, alpine region",
                            ]
                        )
                        and supplier[0] == "Water"
                        and supplier[1] in ("air", "water")
                    )
                if not allowed:
                    unexpected.append((activity, supplier, row.before, row.after))
            assert not unexpected, (matrix, unexpected[:10])
            checks.append(
                {
                    "matrix": matrix,
                    "exchanges_compared": len(joined),
                    "changed_exchanges": int(changed.sum()),
                    "unexpected_changes": 0,
                }
            )
        results.append({"year": year, "pump": pump, "checks": checks})
    output = after.with_suffix(".hydropower_comparison.json")
    output.write_text(json.dumps(results, indent=2) + "\n")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.before, args.after), indent=2))
