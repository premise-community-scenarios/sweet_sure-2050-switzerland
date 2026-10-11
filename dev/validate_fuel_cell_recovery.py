"""Verify a fuel-cell recovery rebuild against every baseline A/B coefficient."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .compare_heat_pump_inventories import read_matrices
    from .stem_fuel_cells import EFFICIENCY, RECOVERED
except ImportError:
    from compare_heat_pump_inventories import read_matrices
    from stem_fuel_cells import EFFICIENCY, RECOVERED


def vector(data, matrix):
    activities = data["A_matrix_index"].set_index("index")
    afields = ["name", "reference product", "unit", "location"]
    activity_keys = activities[afields].apply(tuple, axis=1)
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
    baseline = json.loads(before.with_suffix(".build.json").read_text())
    assert baseline["scenario"] == manifest["scenario"]
    assert set(manifest["years"]) <= set(baseline["years"])
    records = json.loads(after.with_suffix(".efficiencies.json").read_text())
    old_records = json.loads(before.with_suffix(".efficiencies.json").read_text())
    results = []
    for year in manifest["years"]:
        (record,) = [
            r
            for r in records
            if r["year"] == year and EFFICIENCY + RECOVERED in r["variables"]
        ]
        assert not any(
            r["year"] == year and r["activity"] == record["activity"]
            for r in old_records
        )
        assert not record["efficiency_bounds"]["clipped"]
        factor = record["old_physical_efficiency"] / record["stem_target_efficiency"]
        key = (
            record["activity"],
            record["product"],
            "kilowatt hour",
            record["location"],
        )
        left, right = read_matrices(before, year), read_matrices(after, year)
        for index, fields in [
            ("A_matrix_index", ["name", "reference product", "unit", "location"]),
            ("B_matrix_index", ["name", "compartment", "subcompartment", "unit"]),
        ]:
            assert set(
                left[index].fillna("")[fields].itertuples(index=False, name=None)
            ) == set(right[index].fillna("")[fields].itertuples(index=False, name=None))
        checks = []
        for matrix in ("A_matrix", "B_matrix"):
            joined = pd.concat(
                [vector(left, matrix), vector(right, matrix)],
                axis=1,
                keys=["before", "after"],
            ).fillna(0)
            scale = np.array(
                [
                    factor if activity == key and flip else 1
                    for activity, _, flip in joined.index
                ]
            )
            expected = joined.before.to_numpy() * scale
            valid = np.isclose(joined.after.to_numpy(), expected, rtol=1e-9, atol=1e-15)
            assert valid.all(), (year, matrix, joined.loc[~valid].head(10).to_dict())
            changed = ~np.isclose(joined.before, joined.after, rtol=1e-10, atol=1e-15)
            checks.append(
                {
                    "matrix": matrix,
                    "exchanges_checked": len(joined),
                    "changed_exchanges": int(changed.sum()),
                    "unexpected_changes": 0,
                }
            )
        results.append(
            {
                "year": year,
                "activity": key,
                "before_physical_efficiency": record["old_physical_efficiency"],
                "recovered_physical_efficiency": record["stem_target_efficiency"],
                "exchange_ratio_after_over_before": factor,
                "checks": checks,
            }
        )
    after.with_suffix(".fuel_cell_validation.json").write_text(
        json.dumps(results, indent=2) + "\n"
    )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.before, args.after), indent=2))
