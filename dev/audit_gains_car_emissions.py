"""Verify all gasoline EURO-6 NOx compartments against independent GAINS targets.

Use the prior package's 2020 inventory as the baseline, adjust for fuel use in
each rebuilt year, and apply the configured GAINS factor to every compartment.
This checks final emissions against the appropriate post-GAINS reference.
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from premise.car_energy import fuel_balance
from premise.data_collection import get_gains_IAM_data
from premise.emissions import Emissions
from premise.validation import load_car_exhaust_pollutants


def extract_cars(directory, regions):
    ai, bi, a, b = [
        pd.read_csv(directory / f"{name}.csv", sep=";")
        for name in ("A_matrix_index", "B_matrix_index", "A_matrix", "B_matrix")
    ]
    suppliers = ai.set_index("index").to_dict("index")
    selected = {
        index: {**supplier, "exchanges": []}
        for index, supplier in suppliers.items()
        if supplier["name"].startswith("transport, passenger car, gasoline, ")
        and supplier["name"].endswith(", EURO-6")
        and supplier["location"] in regions
    }
    selected_a = a.loc[a["index of activity"].isin(selected)]
    for activity, product, amount, flip in selected_a[
        ["index of activity", "index of product", "value", "flip"]
    ].itertuples(index=False, name=None):
        supplier = suppliers[product]
        selected[activity]["exchanges"].append(
            {
                "name": supplier["name"],
                "unit": supplier["unit"],
                "type": "technosphere" if flip else "production",
                "amount": amount,
            }
        )
    nox_ids = set(
        bi.loc[bi["name"].eq("Nitrogen oxides") & bi["compartment"].eq("air"), "index"]
    )
    selected_b = b.loc[
        b["index of activity"].isin(selected)
        & b["index of biosphere flow"].isin(nox_ids)
    ].merge(bi, left_on="index of biosphere flow", right_on="index")
    emissions = {
        activity: group.groupby("subcompartment")["value"].sum().to_dict()
        for activity, group in selected_b.groupby("index of activity")
    }
    return {
        tuple(dataset[field] for field in ("name", "reference product", "location")): {
            "fuel_energy_mj_per_km": fuel_balance(dataset)[0],
            "nox_kg_per_km_by_compartment": emissions.get(index, {}),
        }
        for index, dataset in selected.items()
    }


def audit(baseline, corrected):
    gains = get_gains_IAM_data("remind", "CLE")
    regions = set(gains.region.values) | {"World"}
    source = extract_cars(baseline, regions)
    assert (
        len(source) == 78
    ), f"Expected 78 gasoline EURO-6 inventories, got {len(source)}"
    hbefa_kg_per_mj = (
        load_car_exhaust_pollutants()["gasoline"]["6.2"]["Nitrogen oxides"] / 1000
    )
    results, per_year = [], []
    members = sorted(corrected.glob("**/A_matrix.csv"))
    assert len(members) == 7, "Expected seven years of rebuilt matrices"
    for member in members:
        year = int(member.parent.name)
        cars = extract_cars(member.parent, regions)
        assert cars.keys() == source.keys()
        updater = object.__new__(Emissions)
        updater.year = year
        factors = updater.prepare_data(gains)
        compartment_count = 0
        for key, car in cars.items():
            raw_factor = float(
                factors.sel(
                    region=key[2], pollutant="NOx", sector="End_Use_Transport_LDT_LLF"
                )
            )
            factor = raw_factor if 0 < raw_factor < 1 else 1.0
            energy = car["fuel_energy_mj_per_km"]
            fuel_ratio = energy / source[key]["fuel_energy_mj_per_km"]
            actual = car["nox_kg_per_km_by_compartment"]
            original = source[key]["nox_kg_per_km_by_compartment"]
            assert actual.keys() == original.keys()
            compartments = []
            for compartment, amount in actual.items():
                expected = original[compartment] * fuel_ratio * factor
                assert np.isclose(amount, expected, rtol=1e-6, atol=1e-15), (
                    year,
                    key,
                    compartment,
                    amount,
                    expected,
                )
                compartments.append(
                    {
                        "compartment": compartment,
                        "actual_kg_per_km": amount,
                        "expected_kg_per_km": expected,
                    }
                )
                compartment_count += 1
            actual_total = sum(actual.values())
            adjusted_reference = hbefa_kg_per_mj * energy * factor
            assert actual_total > 0
            assert math.isclose(actual_total, adjusted_reference, rel_tol=0.5), (
                year,
                key,
                actual_total,
                adjusted_reference,
            )
            results.append(
                {
                    "year": year,
                    "activity": key,
                    "gains_factor": factor,
                    "actual_total_mg_per_km": actual_total * 1e6,
                    "gains_adjusted_hbefa_reference_mg_per_km": adjusted_reference
                    * 1e6,
                    "compartments": compartments,
                }
            )
        row = {
            "year": year,
            "car_inventories_checked": len(cars),
            "nox_compartments_checked": compartment_count,
            "gains_adjusted_reference_failures": 0,
        }
        per_year.append(row)
        print(json.dumps(row), flush=True)
    return {
        "baseline_2020": str(baseline.resolve()),
        "corrected_matrices": str(corrected.resolve()),
        "scope": "All 78 IAM-region gasoline EURO-6 inventories in each of seven years. Every NOx air compartment checked at relative tolerance 1e-6 against independent 2020 inventory / fuel-use / GAINS expectations. Aggregate emissions also checked against the HBEFA reference adjusted by GAINS, using the existing 50% reference tolerance.",
        "years": per_year,
        "activity_year_checks": len(results),
        "compartment_checks": sum(row["nox_compartments_checked"] for row in per_year),
        "all_checks_passed": True,
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline_2020", type=Path)
    parser.add_argument("corrected_matrices", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.baseline_2020, args.corrected_matrices)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"GAINS car audit passed: {args.output}")
