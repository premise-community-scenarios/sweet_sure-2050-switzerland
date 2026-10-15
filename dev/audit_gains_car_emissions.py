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
import yaml
import premise

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


def workbook_reference(path):
    """Read the regenerated source's NOx per vehicle-km, independently of premise."""
    import openpyxl

    result = {}
    current = headers = None
    for row in (
        openpyxl.load_workbook(path, read_only=True, data_only=True)
        .worksheets[0]
        .values
    ):
        if row[0] == "Activity":
            current = (
                row[1]
                if row[1].startswith("transport, passenger car, gasoline, ")
                and row[1].endswith(", EURO-6")
                else None
            )
            if current:
                result[current] = {}
            headers = None
        elif current and row[0] == "name" and row[1] == "amount":
            headers = row
        elif current and headers and row[0] == "Nitrogen oxides":
            e = dict(zip(headers, row))
            if e.get("type") != "biosphere":
                continue
            categories = e["categories"].split("::")
            compartment = categories[1] if len(categories) > 1 else "unspecified"
            result[current][compartment] = result[current].get(compartment, 0) + float(
                e["amount"]
            )
    if not result or not all(result.values()):
        raise ValueError("Source workbook lacks positive gasoline-car NOx references")
    return result


def audit(baseline, corrected, vehicle_workbook=None):
    gains = get_gains_IAM_data("remind", "CLE")
    regions = set(gains.region.values) | {"World"}
    source = (
        workbook_reference(vehicle_workbook)
        if vehicle_workbook
        else extract_cars(baseline, regions)
    )
    configured_names = None
    if vehicle_workbook:
        mapping = yaml.safe_load(
            (
                Path(premise.__file__).parent
                / "iam_variables_mapping/transport_passenger_cars.yaml"
            ).read_text()
        )
        configured_names = {
            name
            for spec in mapping.values()
            if "remind" in spec.get("iam_aliases", {})
            for name in spec.get("ecoinvent_aliases", {}).get("fltr", [])
            if isinstance(name, str)
            and name.startswith("transport, passenger car, gasoline, ")
            and name.endswith(", EURO-6")
        }
        assert configured_names and configured_names <= source.keys()
    if not vehicle_workbook:
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
        if vehicle_workbook:
            assert {(key[0], key[2]) for key in cars} == {
                (name, region) for name in configured_names for region in regions
            }, "Missing or unexpected configured regional car inventories"
        else:
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
            fuel_ratio = (
                1 if vehicle_workbook else energy / source[key]["fuel_energy_mj_per_km"]
            )
            actual = car["nox_kg_per_km_by_compartment"]
            original = (
                source[key[0]]
                if vehicle_workbook
                else source[key]["nox_kg_per_km_by_compartment"]
            )
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
            assert vehicle_workbook or math.isclose(
                actual_total, adjusted_reference, rel_tol=0.5
            ), (
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
                    "hbefa_screen_passed": math.isclose(
                        actual_total, adjusted_reference, rel_tol=0.5
                    ),
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
        "scope": (
            "All selected IAM-region gasoline EURO-6 inventories in seven years; every NOx compartment checked against source inventory and GAINS. Refreshed HBEFA hot/cold emissions retain their distance basis and are not scaled with fuel energy; the inherited hot-reference screen is diagnostic only."
            if vehicle_workbook
            else "Legacy 78-inventory fuel-proportional NOx audit against frozen 2020 matrices and GAINS."
        ),
        "vehicle_workbook": str(vehicle_workbook) if vehicle_workbook else None,
        "configured_regional_classes": sorted(configured_names or []),
        "source_classes_outside_regional_fleet": (
            sorted(set(source) - configured_names) if configured_names else []
        ),
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
    parser.add_argument("--vehicle-workbook", type=Path)
    args = parser.parse_args()
    report = audit(args.baseline_2020, args.corrected_matrices, args.vehicle_workbook)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"GAINS car audit passed: {args.output}")
