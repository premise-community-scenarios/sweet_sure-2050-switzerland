"""Verify explicit efficiency bounds against complete baseline A/B matrices."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .compare_heat_pump_inventories import read_matrices, keys
except ImportError:
    from compare_heat_pump_inventories import read_matrices, keys


def validate(before, after):
    manifest = json.loads(after.with_suffix(".build.json").read_text())
    records = json.loads(after.with_suffix(".efficiencies.json").read_text())
    result = []
    for year in manifest["years"]:
        left, right = read_matrices(before, year), read_matrices(after, year)
        afields = ["name", "reference product", "unit", "location"]
        bfields = ["name", "compartment", "subcompartment", "unit"]
        activities = keys(left["A_matrix_index"], afields)
        assert set(activities) == set(keys(right["A_matrix_index"], afields))
        canonical = {key: i for i, key in enumerate(activities)}
        flows = keys(left["B_matrix_index"].fillna(""), bfields)
        assert set(flows) == set(keys(right["B_matrix_index"].fillna(""), bfields))
        flow_ids = {key: i for i, key in enumerate(flows)}
        factors = {}
        clipped = []
        for record in records:
            if record["year"] != year or not record["efficiency_bounds"]["clipped"]:
                continue
            matches = [
                key
                for key in activities
                if key[0] == record["activity"]
                and key[1] == record["product"]
                and key[3] == record["location"]
            ]
            assert len(matches) == 1, matches
            factor = (
                record["stem_target_efficiency"] / record["applied_target_efficiency"]
            )
            factors[canonical[matches[0]]] = factor
            clipped.append(
                {
                    "activity": record["activity"],
                    "location": record["location"],
                    "requested": record["stem_target_efficiency"],
                    "applied": record["applied_target_efficiency"],
                    "exchange_ratio_after_over_before": factor,
                    "combined_efficiency_check": record["efficiency_bounds"][
                        "combined_efficiency_check"
                    ],
                }
            )
        matrix_checks = []
        for matrix, supplier in (
            ("A_matrix", "index of product"),
            ("B_matrix", "index of biosphere flow"),
        ):
            vectors = []
            for data in (left, right):
                aids = np.array(
                    [canonical[k] for k in keys(data["A_matrix_index"], afields)]
                )
                sids = (
                    aids
                    if matrix == "A_matrix"
                    else np.array(
                        [
                            flow_ids[k]
                            for k in keys(data["B_matrix_index"].fillna(""), bfields)
                        ]
                    )
                )
                source = data[matrix]
                vectors.append(
                    pd.DataFrame(
                        {
                            "activity": aids[source["index of activity"]],
                            "supplier": sids[source[supplier]],
                            "flip": (
                                source.flip.to_numpy() if matrix == "A_matrix" else 1
                            ),
                            "value": source.value.to_numpy(),
                        }
                    )
                    .groupby(["activity", "supplier", "flip"])
                    .value.sum()
                )
            joined = pd.concat(vectors, axis=1, keys=["before", "after"]).fillna(0)
            ratios = np.array(
                [factors.get(a, 1) if flip else 1 for a, _, flip in joined.index]
            )
            expected = joined.before.to_numpy() * ratios
            valid = np.isclose(joined.after.to_numpy(), expected, rtol=1e-9, atol=1e-15)
            assert valid.all(), (year, matrix, joined.loc[~valid].head(10).to_dict())
            changed = ~np.isclose(joined.before, joined.after, rtol=1e-10, atol=1e-15)
            changed_activities = set(joined.index.get_level_values("activity")[changed])
            assert changed_activities <= factors.keys()
            matrix_checks.append(
                {
                    "matrix": matrix,
                    "exchanges_checked": len(joined),
                    "changed_exchanges": int(changed.sum()),
                    "changed_activities": len(changed_activities),
                    "unexpected_changes": 0,
                }
            )
        result.append(
            {
                "year": year,
                "activities": len(activities),
                "clipped": clipped,
                "checks": matrix_checks,
            }
        )
    after.with_suffix(".efficiency_bounds_validation.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.before, args.after), indent=2))
