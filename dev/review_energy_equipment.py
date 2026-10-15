#!/usr/bin/env python3
"""Review new coefficients, endpoint sensitivities and exact old/new attribution.

No sensitivity changes the central results. Component coverage and parameter
validity are reported separately from arithmetic agreement.
"""

import argparse
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import openpyxl
import pandas as pd
import yaml

from audit_metal_quantities import Inventory, ROOT, digest, write_json
from audit_metal_boundary_review import formula_fraction
from audit_metal_coefficients import RAW, children, raw_demand_checks
from audit_stem_input_chain import scenario_code, source_frame


def first_tier(inv, root):
    return {
        tuple(inv.nodes.loc[i, ["name", "reference product", "unit", "location"]]): (
            i,
            c,
        )
        for i, c in children(inv, root)
    }


def heat_equipment_paths(inv, root, max_depth=12):
    """Trace positive heat-provider links to identifiable equipment inputs.

    This coverage screen excludes fuel/electricity supply chains. It neither
    certifies technology suitability nor substitutes for the full signed LCI.
    Each path retains its native-unit coefficient, including market shares.
    """
    equipment_words = (
        "boiler",
        "furnace",
        "stove",
        "wood heater",
        "heat pump",
        "collector",
        "hot water tank",
        "power plant",
        "cogeneration",
        "co-generation",
        "fuel cell",
        "generator",
        "chimney",
        "heat distribution",
        "pipe",
    )
    stack, rows = [(root, 1.0, ())], []
    while stack:
        current, coefficient, ancestors = stack.pop()
        name = inv.nodes.loc[current, "name"]
        path = (*ancestors, current)
        candidates, found = [], False
        for supplier, amount in children(inv, current):
            if amount <= 0:
                continue
            node = inv.nodes.loc[supplier]
            key = node["name"].lower()
            quantity = coefficient * amount
            if node["unit"] not in ("megajoule", "kilowatt hour") and any(
                word in key for word in equipment_words
            ):
                found = True
                rows.append(
                    {
                        "provider": name,
                        "equipment": node["name"],
                        "product": node["reference product"],
                        "unit": node["unit"],
                        "input_per_MJ": quantity,
                        "status": "identified equipment input",
                        "path": " -> ".join(
                            inv.nodes.loc[i, "name"] for i in (*path, supplier)
                        ),
                    }
                )
            elif node["unit"] == "megajoule" and (
                "heat" in node["reference product"].lower()
                or any(word in key for word in ("heat", "burned", "solar"))
            ):
                candidates.append((supplier, quantity))
        if not found and not candidates:
            rows.append(
                {
                    "provider": name,
                    "equipment": "",
                    "product": "",
                    "unit": "",
                    "input_per_MJ": None,
                    "status": "no equipment identified by heat-path screen",
                    "path": " -> ".join(inv.nodes.loc[i, "name"] for i in path),
                }
            )
        for supplier, quantity in candidates:
            if supplier in path or len(path) >= max_depth:
                rows.append(
                    {
                        "provider": name,
                        "equipment": "",
                        "product": "",
                        "unit": "",
                        "input_per_MJ": None,
                        "status": "cycle or depth limit; unresolved",
                        "path": " -> ".join(
                            inv.nodes.loc[i, "name"] for i in (*path, supplier)
                        ),
                    }
                )
            else:
                stack.append((supplier, quantity, path))
    return rows


def attribution(old, new, old_root, new_root, old_q, new_q):
    left, right = first_tier(old, old_root), first_tier(new, new_root)
    k = len(new.methods)
    result = {
        label: np.zeros(k)
        for label in [
            "demand",
            "foreground_coefficients",
            "upstream_intensities",
            "added_suppliers",
            "removed_suppliers",
            "direct_boundary",
        ]
    }
    result["demand"] = (new_q - old_q) * old.Z[:, old_root]
    for key in left.keys() & right.keys():
        oi, oc = left[key]
        ni, nc = right[key]
        result["foreground_coefficients"] += new_q * (nc - oc) * old.Z[:, oi]
        result["upstream_intensities"] += new_q * nc * (new.Z[:, ni] - old.Z[:, oi])
    for key in right.keys() - left.keys():
        i, c = right[key]
        result["added_suppliers"] += new_q * c * new.Z[:, i]
    for key in left.keys() - right.keys():
        i, c = left[key]
        result["removed_suppliers"] -= new_q * c * old.Z[:, i]
    result["direct_boundary"] = new_q * (
        new.C[:, new_root] / new.A[new_root, new_root]
        - old.C[:, old_root] / old.A[old_root, old_root]
    )
    return result


def linked_supplier(inv, parent, identity):
    """Resolve source metadata through actual inputs after import/regionalization.

    Brightway normalizes some product capitalization. Location can also change;
    require one matching positive input to the consuming activity rather than
    selecting a similarly named but unconnected global inventory.
    """
    name, product, _, unit = identity
    expected = tuple(value.strip().casefold() for value in (name, product, unit))
    matches = []
    for supplier, coefficient in children(inv, parent):
        node = inv.nodes.loc[supplier]
        actual = tuple(
            node[field].strip().casefold()
            for field in ("name", "reference product", "unit")
        )
        if coefficient > 0 and actual == expected:
            matches.append((supplier, coefficient))
    if len(matches) != 1:
        raise ValueError(
            f"Expected one connected supplier for {identity}; found {len(matches)}"
        )
    return matches[0]


def chemical_checks(results, output):
    config = yaml.safe_load((results / "refined_metal_inputs.yaml").read_text())
    methods = [
        m
        for m in json.loads((results / "method_registry.json").read_text())["methods"]
        if "definition" in m
    ]
    rows = []
    for m in methods:
        alloy = m["definition"].get("kind") == "alloy_mass"
        entry = next(
            x
            for x in config["alloy_indicators" if alloy else "indicators"]
            if x["name"] == m["label"]
        )
        for p in m["definition"]["products"]:
            settings = {} if alloy else entry["products"][p["product"]]
            expected = (
                1
                if alloy
                else (
                    settings["factor"]
                    if "factor" in settings
                    else formula_fraction(
                        settings["formula"], entry["symbol"], config["atomic_weights"]
                    )
                )
            )
            rows.append(
                dict(
                    indicator=m["indicator"],
                    product=p["product"],
                    expected=expected,
                    actual=p["factor"],
                    passed=bool(
                        np.isclose(expected, p["factor"], rtol=1e-13, atol=1e-15)
                    ),
                )
            )
    assert all(r["passed"] for r in rows)
    pd.DataFrame(rows).to_csv(output / "chemical_factor_checks.csv", index=False)
    return methods, len(rows)


def boiler_source_checks(base, output):
    """Validate stock and energy against original STEM exports and VAR_CAP formulas."""
    rows = []
    with ZipFile(RAW) as archive:
        for member in archive.namelist():
            if Path(member).name not in [
                f"STEM_to_Premise_SPS{i}_2035.xlsx" for i in range(1, 5)
            ]:
                continue
            scenario = scenario_code(member)
            payload = archive.read(member)
            raw, years, sheet = source_frame(payload)
            workbook = openpyxl.load_workbook(
                BytesIO(payload), read_only=True, data_only=False
            )
            formulas = {
                r[3]: r[5:] for r in workbook[sheet].values if len(r) > 5 and r[3]
            }
            package = next((base / "build/packages").glob(f"*-{scenario}.zip"))
            for record in json.loads(
                package.with_suffix(".electric_boilers.json").read_text()
            ):
                variable, year = record["capacity_variable"], record["year"]
                source_formulas = [
                    str(getattr(f, "text", f)).lower()
                    for f in formulas[variable]
                    if str(getattr(f, "text", f)).startswith("=")
                ]
                assert source_formulas and all(
                    '"var_cap"' in f for f in source_formulas
                )
                capacity, energy = [
                    float(np.interp(year, years, raw.loc[v, years].to_numpy(float)))
                    for v in (variable, record["energy_variable"])
                ]
                passed = bool(
                    np.isclose(capacity, record["capacity_GW"], rtol=1e-12, atol=1e-12)
                    and np.isclose(
                        energy, record["electricity_PJ"], rtol=1e-12, atol=1e-12
                    )
                )
                hours = energy * 1e9 / (capacity * 1e6 * 3.6) if capacity else 0
                rows.append(
                    {
                        "scenario": scenario,
                        "year": year,
                        "profile": record["id"],
                        "raw_capacity_GW": capacity,
                        "raw_energy_PJ": energy,
                        "capacity_source": "VAR_CAP installed stock; not capacity additions",
                        "capacity_source_formulas": json.dumps(source_formulas),
                        "equivalent_purchased_electricity_full_load_hours": hours,
                        "utilization_review_flag": hours > 8760 * 1.01,
                        "passed": passed,
                        "source_workbook": member,
                    }
                )
            workbook.close()
    assert len(rows) == 4 * 7 * 4 and all(r["passed"] for r in rows)
    pd.DataFrame(rows).to_csv(
        output / "raw_boiler_stock_energy_checks.csv", index=False
    )
    return rows


def run(base, previous, output):
    output.mkdir(parents=True, exist_ok=True)
    results = base / "refined_metals"
    methods, n_factors = chemical_checks(results, output)
    raw_checks = raw_demand_checks(base, results, output)
    boiler_checks = boiler_source_checks(base, output)
    old_methods = [
        m
        for m in json.loads(
            (previous / "refined_metals/method_registry.json").read_text()
        )["methods"]
        if "definition" in m
    ]
    assert [m["definition"] for m in methods] == [
        m["definition"] for m in old_methods
    ], "Methods changed: old/new attribution requires the same boundary"
    old_totals = pd.read_csv(previous / "refined_metals/indicator_totals.csv")
    new_totals = pd.read_csv(results / "indicator_totals.csv")
    keys = [c for c in old_totals if c != "value"]
    comparison = new_totals.merge(
        old_totals, on=keys, suffixes=("_new", "_old"), validate="one_to_one"
    )
    comparison["difference"] = comparison.value_new - comparison.value_old
    comparison["relative_change"] = np.where(
        comparison.value_old.ne(0), comparison.difference / comparison.value_old, np.nan
    )
    comparison.to_csv(output / "old_new_indicator_totals.csv", index=False)
    branches, effects, decomposed, balances, heat_paths = [], [], [], [], []
    coverage = pd.read_csv(base / "vehicle_audit/equipment_coverage.csv")
    stationary = set(
        coverage.loc[
            coverage.status.str.startswith(
                ("linked energy", "gas-engine CHP", "hydrogen heat", "heat-pump")
            ),
            "pathway",
        ]
    )
    for scenario in ["SPS1_bas0", "SPS2_bas0", "SPS3_bas0", "SPS4_bas0"]:
        package = next((base / "build/packages").glob(f"*-{scenario}.zip"))
        previous_package = next((previous / "build/packages").glob(f"*-{scenario}.zip"))
        demand = pd.read_csv(
            results / "scenarios" / scenario / "pathways_demands.csv",
            float_precision="round_trip",
        )
        old_demand = pd.read_csv(
            previous / "refined_metals/scenarios" / scenario / "pathways_demands.csv",
            float_precision="round_trip",
        )
        boilers = json.loads(package.with_suffix(".electric_boilers.json").read_text())
        for year in [2020, 2050]:
            inv = Inventory(package, year, methods)
            old = Inventory(previous_package, year, old_methods)
            context = dict(scenario=scenario, year=year)
            group = demand[demand.year.eq(year)]
            old_group = old_demand[old_demand.year.eq(year)].set_index("variable")
            system = np.zeros(len(methods))
            roots = {}
            for row in group.to_dict("records"):
                root, q = inv.demand(row)
                roots[row["variable"]] = (root, q)
                system += q * inv.Z[:, root]
            for row in group.to_dict("records"):
                root, q = roots[row["variable"]]
                if row["variable"].removeprefix("EXT - 0 - ") in stationary:
                    heat_paths.extend(
                        {**context, "variable": row["variable"], **p}
                        for p in heat_equipment_paths(inv, root)
                    )
                prior = old_group.loc[row["variable"]].to_dict()
                prior["variable"] = row["variable"]
                old_root, old_q = old.demand(prior)
                parts = attribution(old, inv, old_root, root, old_q, q)
                expected = q * inv.Z[:, root] - old_q * old.Z[:, old_root]
                actual = sum(parts.values())
                # Model scales span grams to megatonnes; retain residuals.
                passed = np.isclose(actual, expected, rtol=1e-7, atol=1e-4)
                for k, m in enumerate(methods):
                    decomposed.append(
                        {
                            **context,
                            "variable": row["variable"],
                            "indicator": m["indicator"],
                            "difference_kg": expected[k],
                            **{name: v[k] for name, v in parts.items()},
                            "residual_kg": actual[k] - expected[k],
                            "passed": bool(passed[k]),
                        }
                    )
                for i, c in children(inv, root):
                    supplier = inv.nodes.loc[i]
                    if (
                        any(token in row["variable"] for token in ("rail", "tram"))
                        and supplier["reference product"] == "diesel"
                        and supplier["unit"] == "kilogram"
                    ):
                        for k, m in enumerate(methods):
                            effects.append(
                                {
                                    **context,
                                    "variable": row["variable"],
                                    "sensitivity": "rail_shunting_fuel_input_excluded",
                                    "indicator": m["indicator"],
                                    "change_kg": -q * c * inv.Z[k, i],
                                    "system_kg": system[k],
                                }
                            )
                    for k, m in enumerate(methods):
                        branches.append(
                            {
                                **context,
                                "variable": row["variable"],
                                "supplier": supplier["name"],
                                "product": supplier["reference product"],
                                "unit": supplier["unit"],
                                "annual_MJ": q,
                                "input_per_MJ": c,
                                "annual_input": q * c,
                                "indicator": m["indicator"],
                                "kg_per_unit": inv.Z[k, i],
                                "branch_kg": q * c * inv.Z[k, i],
                            }
                        )
                    if (
                        "energy equipment production," in supplier["name"]
                        or supplier["name"] == "compact fluorescent lamp production"
                    ):
                        for multiplier in [0.5, 2.0]:
                            delta = q * c * (multiplier - 1) * inv.Z[:, i]
                            for k, m in enumerate(methods):
                                effects.append(
                                    {
                                        **context,
                                        "variable": row["variable"],
                                        "sensitivity": f"aggregate_equipment_intensity_x{multiplier}",
                                        "indicator": m["indicator"],
                                        "change_kg": delta[k],
                                        "system_kg": system[k],
                                    }
                                )
                    if supplier["reference product"] == "electricity storage capacity":
                        mix = children(inv, i)
                        lis = sum(
                            v for j, v in mix if "Li-sulfur" in inv.nodes.loc[j, "name"]
                        )
                        if 0 < lis < 1:
                            without = sum(
                                v * inv.Z[:, j]
                                for j, v in mix
                                if "Li-sulfur" not in inv.nodes.loc[j, "name"]
                            ) / (1 - lis)
                            delta = q * c * (without - inv.Z[:, i])
                            for k, m in enumerate(methods):
                                effects.append(
                                    {
                                        **context,
                                        "variable": row["variable"],
                                        "sensitivity": "battery_no_LiS_renormalized",
                                        "indicator": m["indicator"],
                                        "change_kg": delta[k],
                                        "system_kg": system[k],
                                    }
                                )
                    if (
                        supplier["reference product"].startswith("heat")
                        and "heat_pump" in row["variable"]
                        and not row["variable"].endswith("_gas")
                    ):
                        power = c * sum(
                            v
                            for j, v in children(inv, i)
                            if inv.nodes.loc[j, "reference product"]
                            == "electricity, low voltage"
                            and inv.nodes.loc[j, "unit"] == "kilowatt hour"
                        )
                        if power:
                            balances.append(
                                {
                                    **context,
                                    "variable": row["variable"],
                                    "MJ_electricity_per_MJ_final": power * 3.6,
                                    "passed": bool(
                                        np.isclose(power * 3.6, 1, rtol=2e-6)
                                    ),
                                }
                            )
            for boiler in [p for p in boilers if p["year"] == year]:
                root, q = roots["EXT - 0 - " + boiler["pathway"]]
                h = boiler["hardware"]
                i, coefficient = linked_supplier(inv, root, h)
                units = coefficient * q
                scenarios = []
                for life in boiler["lifetime_range"]:
                    scenarios.append(
                        (
                            f"boiler_lifetime_{life}years",
                            units * (boiler["lifetime_years"] / life - 1) * inv.Z[:, i],
                        )
                    )
                for label, mass in boiler["materials_kg"].items():
                    if label == "unmodelled":
                        continue
                    original = boiler["material_suppliers"][label]
                    original_id, _ = linked_supplier(inv, i, original)
                    targets = []
                    if label in ("stainless", "low_alloy"):
                        targets = [
                            (
                                "market for steel, low-alloyed"
                                if label == "stainless"
                                else "market for steel, chromium steel 18/8"
                            )
                        ]
                    elif label == "electronics":
                        targets = [
                            "market for cable, unspecified",
                            "market for electronic component, passive, unspecified",
                        ]
                    for target in targets:
                        candidates = inv.nodes[
                            inv.nodes.name.eq(target) & inv.nodes.unit.eq("kilogram")
                        ]
                        chosen = None
                        for loc in [
                            inv.nodes.loc[original_id, "location"],
                            "RER",
                            "GLO",
                            "RoW",
                        ]:
                            ids = candidates[candidates.location.eq(loc)].index
                            if len(ids) == 1:
                                chosen = ids[0]
                                break
                        assert chosen is not None, target
                        scenarios.append(
                            (
                                f"boiler_{label}_mapped_to_{target}",
                                units
                                * mass
                                * (inv.Z[:, chosen] - inv.Z[:, original_id]),
                            )
                        )
                for sensitivity, delta in scenarios:
                    for k, m in enumerate(methods):
                        effects.append(
                            {
                                **context,
                                "variable": boiler["pathway"],
                                "sensitivity": sensitivity,
                                "indicator": m["indicator"],
                                "change_kg": delta[k],
                                "system_kg": system[k],
                            }
                        )
            for name, rows in [
                ("equipment_branches", branches),
                ("coefficient_sensitivities", effects),
                ("old_new_attribution", decomposed),
                ("heat_pump_energy_balance", balances),
                ("heat_equipment_paths", heat_paths),
            ]:
                pd.DataFrame(rows).to_parquet(output / f"{name}.parquet", index=False)
            print(f"Reviewed {scenario}/{year}", flush=True)
    assert all(r["passed"] for r in balances)
    summary = {
        "chemical_factor_checks": n_factors,
        "raw_STEM_demand_checks": len(raw_checks),
        "raw_STEM_demand_failures": sum(not r["passed"] for r in raw_checks),
        "raw_boiler_stock_energy_checks": len(boiler_checks),
        "boiler_utilization_review_flags": sum(
            r["utilization_review_flag"] for r in boiler_checks
        ),
        "heat_pump_checks": len(balances),
        "heat_pump_failures": sum(not r["passed"] for r in balances),
        "heat_equipment_paths": len(heat_paths),
        "unresolved_heat_paths": sum(
            r["status"] != "identified equipment input" for r in heat_paths
        ),
        "attribution_checks": len(decomposed),
        "attribution_failures": sum(not r["passed"] for r in decomposed),
        "max_attribution_residual_kg": max(abs(r["residual_kg"]) for r in decomposed),
        "interpretation": "Unapplied, one-at-a-time sensitivities, not confidence intervals. Old/new attribution order: demand, foreground coefficients at old supplier intensities, upstream changes, added/removed suppliers; additions include their new supply chains. Never sum different sensitivity cases.",
        "limitations": "Boilers retain composition and technology proxies; aggregates retain explicitly uncovered energy components. Results are not complete physical system demand or annual equipment purchases.",
        "script_sha256": digest(__file__),
    }
    write_json(output / "equipment_review_summary.json", summary)
    if summary["attribution_failures"]:
        raise ValueError("Old/new attribution residual requires review")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / "results/sps_updated")
    parser.add_argument("--previous", type=Path, default=ROOT / "results/sps_base")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/sps_updated/metal_audit"
    )
    args = parser.parse_args()
    run(args.base, args.previous, args.output)
