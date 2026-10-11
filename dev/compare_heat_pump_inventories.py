"""Check that a rebuilt heat-pump package changes only the intended matrix columns."""

import argparse
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd


def read_matrices(package, year):
    with ZipFile(package) as archive:
        (directory,) = [
            str(Path(n).parent)
            for n in archive.namelist()
            if n.endswith(f"/{year}/A_matrix.csv")
        ]
        return {
            name: pd.read_csv(
                archive.open(f"{directory}/{name}.csv"),
                sep=";",
                float_precision="round_trip",
            )
            for name in ["A_matrix", "B_matrix", "A_matrix_index", "B_matrix_index"]
        }


def keys(frame, fields):
    return list(frame.sort_values("index")[fields].itertuples(index=False, name=None))


def compare(before, after, year, output):
    left, right = read_matrices(before, year), read_matrices(after, year)
    afields = ["name", "reference product", "unit", "location"]
    bfields = ["name", "compartment", "subcompartment", "unit"]
    old_activities = keys(left["A_matrix_index"], afields)
    new_activities = keys(right["A_matrix_index"], afields)
    common = set(old_activities) & set(new_activities)
    canonical = dict.fromkeys([*old_activities, *new_activities])
    canonical = {key: i for i, key in enumerate(canonical)}
    all_flows = dict.fromkeys(
        keys(left["B_matrix_index"], bfields) + keys(right["B_matrix_index"], bfields)
    )
    flow_ids = {key: i for i, key in enumerate(all_flows)}
    changed = []
    for matrix, supplier, fields in [
        ("A_matrix", "index of product", afields),
        ("B_matrix", "index of biosphere flow", bfields),
    ]:
        vectors = []
        for data in [left, right]:
            activity_ids = np.array(
                [canonical[k] for k in keys(data["A_matrix_index"], afields)]
            )
            supplier_ids = (
                activity_ids
                if matrix == "A_matrix"
                else np.array(
                    [flow_ids[k] for k in keys(data["B_matrix_index"], bfields)]
                )
            )
            source = data[matrix]
            vector = pd.DataFrame(
                {
                    "activity": activity_ids[source["index of activity"]],
                    "supplier": supplier_ids[source[supplier]],
                    "value": source.value.to_numpy(),
                    "flip": source.flip.to_numpy() if matrix == "A_matrix" else 1,
                }
            )
            vectors.append(vector.groupby(["activity", "supplier", "flip"]).value.sum())
        joined = pd.concat(vectors, axis=1, keys=["before", "after"]).fillna(0)
        selected = joined[
            ~np.isclose(joined.before, joined.after, rtol=1e-10, atol=1e-15)
        ]
        for key in common:
            aid = canonical[key]
            if aid in selected.index.get_level_values("activity"):
                changes = selected.xs(aid, level="activity")
                changed.append(
                    dict(zip(afields, key))
                    | {
                        "matrix": matrix,
                        "changed_exchanges": len(changes),
                        "max_absolute_difference": float(
                            (changes.after - changes.before).abs().max()
                        ),
                    }
                )
    allowed_names = {
        "market for district heating (SPS)",
        "electricity, used in industrial heat pump",
        "electricity, used in industrial heat pump_FE_services_heat_pump",
        "electricity, used in residential heat pump",
        "electricity, used in residential heat pump_FE_residential_water_heating_heat_pump",
    }
    unexpected = [
        r for r in changed if r["name"] not in allowed_names or r["location"] != "CH"
    ]
    added = [
        dict(zip(afields, key)) for key in set(new_activities) - set(old_activities)
    ]
    removed = [
        dict(zip(afields, key)) for key in set(old_activities) - set(new_activities)
    ]
    assert not unexpected, unexpected
    assert not removed, removed
    assert len(added) == 6 and all(r["location"] == "CH" for r in added)
    assert (
        sum(
            r["name"].startswith("heat production, air-water heat pump 10kW, STEM ")
            for r in added
        )
        == 5
    )
    assert (
        sum(
            r["name"]
            == "electricity, used in residential heat pump_FE_residential_water_heating_heat_pump"
            for r in added
        )
        == 1
    )
    result = {
        "passed": True,
        "year": year,
        "before": str(before),
        "after": str(after),
        "common_activities": len(common),
        "changed_existing_activities": changed,
        "added_activities": added,
        "removed_activities": removed,
        "unexpected_changes": unexpected,
        "relative_tolerance": 1e-10,
        "absolute_tolerance": 1e-15,
    }
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "year": year,
                "common_activities": len(common),
                "changed_columns": len(changed),
                "added_activities": len(added),
                "unexpected_changes": len(unexpected),
            },
            indent=2,
        )
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    compare(args.before, args.after, args.year, args.output)
