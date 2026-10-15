"""Source-coefficient evidence and explicit, unapplied material sensitivities.

Run with the Pathways environment after audit_metal_quantities.py.
These counterfactuals are additive first-tier demand changes, not replacements
of the official scenarios, forecasts of sales, or stock-flow calculations.
"""

from pathlib import Path
from zipfile import ZipFile
import json
import re
import shutil

import numpy as np
import openpyxl
import pandas as pd
import yaml

from audit_metal_quantities import Inventory, ROOT, digest, write_json
from audit_stem_input_chain import source_frame, scenario_code

OUT = ROOT / "results/sps_base/metal_audit_20261012"
BASE = ROOT / "results/sps_base"
RESULTS = BASE / "refined_metals"
XL = BASE / "build/inputs/inventories/lci-sweet_sure.xlsx"
RAW = ROOT / "dev/STEM raw data/new data export/OneDrive_2_28-09-2026.zip"


def workbook_evidence():
    formula = list(
        openpyxl.load_workbook(XL, data_only=False, read_only=True)["lci"].values
    )
    cached = list(
        openpyxl.load_workbook(XL, data_only=True, read_only=True)["lci"].values
    )
    rows, blocks = [], []
    current, headers = None, None
    for i, (f, c) in enumerate(zip(formula, cached), 1):
        if c[0] == "Activity":
            current = {
                "activity": c[1],
                "activity_row": i,
                "metadata": {},
                "exchanges": [],
            }
            blocks.append(current)
            headers = None
        elif current is not None and c[0] == "name" and c[1] == "amount":
            headers = c
        elif current is not None and c[0] and headers is None:
            current["metadata"][c[0]] = c[1]
        elif current is not None and c[0] and headers is not None:
            exc = {str(k): v for k, v in zip(headers, c) if k is not None}
            exc["row"], exc["amount_formula"] = i, f[1]
            current["exchanges"].append(exc)
            if exc.get("type") == "technosphere":
                rows.append(
                    {
                        "activity": current["activity"],
                        "activity_row": current["activity_row"],
                        **exc,
                    }
                )
    write_json(
        OUT / "foreground_workbook_evidence.json",
        {"sha256": digest(XL), "path": str(XL), "blocks": blocks},
    )
    pd.DataFrame(rows).to_csv(OUT / "foreground_coefficients.csv", index=False)
    return blocks


def raw_demand_checks(base=BASE, results=RESULTS, out=OUT, raw_path=RAW):
    config = yaml.safe_load(
        (base / "build/inputs/configuration_file/config.yaml").read_text()
    )
    rows = []
    with ZipFile(raw_path) as archive:
        for member in archive.namelist():
            if not re.fullmatch(
                r"STEM_to_Premise_SPS[1-4]_2035.xlsx", Path(member).name
            ):
                continue
            scenario = scenario_code(member)
            raw, years, sheet = source_frame(archive.read(member))
            demands = pd.read_csv(
                results / "scenarios" / scenario / "pathways_demands.csv",
                float_precision="round_trip",
            )
            for r in demands.to_dict("records"):
                source_variable = config["production pathways"][
                    r["variable"].removeprefix("EXT - 0 - ")
                ]["production volume"]["variable"]
                raw_value = float(
                    np.interp(
                        r["year"],
                        years,
                        raw.loc[source_variable, years].to_numpy(float),
                    )
                )
                passed = raw_value >= -1e-12 and bool(
                    np.isclose(
                        max(0, raw_value), r["scenario_demand"], rtol=1e-12, atol=1e-12
                    )
                )
                passed &= r["unit"] == "megajoule" and r["conversion_factor"] == 1e9
                rows.append(
                    {
                        "scenario": scenario,
                        "year": r["year"],
                        "variable": r["variable"],
                        "raw_variable": source_variable,
                        "raw_PJ": raw_value,
                        "saved_PJ": r["scenario_demand"],
                        "MJ": max(0, raw_value) * 1e9,
                        "passed": bool(passed),
                        "workbook": member,
                        "sheet": sheet,
                    }
                )
    assert len(rows) == 4 * 7 * 101
    assert all(r["passed"] for r in rows)
    pd.DataFrame(rows).to_csv(out / "raw_STEM_demand_checks.csv", index=False)
    return rows


def children(inv, root):
    start, end = inv.columns.indptr[root : root + 2]
    return [
        (int(i), float(-a / inv.A[root, root]))
        for i, a in zip(inv.columns.indices[start:end], inv.columns.data[start:end])
        if i != root
    ]


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    workbook_evidence()
    raw = raw_demand_checks()
    methods = [
        m
        for m in json.loads((RESULTS / "method_registry.json").read_text())["methods"]
        if "definition" in m
    ]
    selected = [
        i
        for i, m in enumerate(methods)
        if m["label"]
        in [
            "Copper",
            "Lithium",
            "Nickel",
            "Cobalt",
            "Neodymium",
            "Aluminium",
            "Aluminium alloys",
            "Iron and steel alloys",
            "Silver",
            "Zinc",
        ]
    ]
    direct, battery, effects, heat, equipment, boundary = [], [], [], [], [], []
    for s in range(1, 5):
        scenario = f"SPS{s}_bas0"
        demands = pd.read_csv(
            RESULTS / "scenarios" / scenario / "pathways_demands.csv",
            float_precision="round_trip",
        )
        package = next((BASE / "build/packages").glob(f"*-{scenario}.zip"))
        for year in [2020, 2050]:
            inv = Inventory(package, year, methods)
            group = demands[demands.year.eq(year)]
            context = {"scenario": scenario, "year": year}
            # Same Swiss supply used by the electric-car foreground: actual package supplier.
            car = (
                group[group.variable.eq("EXT - 0 - FE_cars_electricity")]
                .iloc[0]
                .to_dict()
            )
            car_id, car_demand = inv.demand(car)
            electric = [
                (i, c)
                for i, c in children(inv, car_id)
                if inv.nodes.loc[i, "reference product"] == "electricity, low voltage"
                and inv.nodes.loc[i, "unit"] == "kilowatt hour"
            ]
            assert len(electric) == 1
            electricity, electricity_coefficient = electric[0]
            assert np.isclose(electricity_coefficient, 1 / 3.6)
            bat = [
                (i, c)
                for i, c in children(inv, car_id)
                if inv.nodes.loc[i, "reference product"]
                == "electricity storage capacity"
            ]
            assert len(bat) == 1
            mix, car_battery_coefficient = bat[0]
            mix_inputs = children(inv, mix)
            assert np.isclose(sum(c for i, c in mix_inputs), 1)
            mix_no_lis = sum(
                c * inv.Z[:, i]
                for i, c in mix_inputs
                if "Li-sulfur" not in inv.nodes.loc[i, "name"]
            )
            lis_share = sum(
                c for i, c in mix_inputs if "Li-sulfur" in inv.nodes.loc[i, "name"]
            )
            mix_no_lis /= 1 - lis_share
            for supplier, share in mix_inputs:
                for k in selected:
                    battery.append(
                        {
                            **context,
                            "technology": inv.nodes.loc[supplier, "name"],
                            "share_kWh": share,
                            "indicator": methods[k]["indicator"],
                            "kg_per_kWh": inv.Z[k, supplier],
                            "contribution_kg_per_mix_kWh": share * inv.Z[k, supplier],
                            "mix_kg_per_kWh": inv.Z[k, mix],
                        }
                    )
            # Product-specific contributions show which mass conversion factors matter.
            rhs = np.zeros(len(inv.nodes))
            for r in group.to_dict("records"):
                i, q = inv.demand(r)
                rhs[i] += q
            system_supply, _ = inv.solve(rhs)
            system_totals = inv.C @ system_supply
            for k, m in enumerate(methods):
                for route, edges in [
                    ("counted", inv.edges[k]),
                    ("internal_excluded", inv.internal[k]),
                ]:
                    e = edges.copy()
                    e["product"] = inv.nodes.loc[
                        e.supplier, "reference product"
                    ].to_numpy()
                    e["kg"] = (
                        e.amount_per_activity * e.factor * system_supply[e.consumer]
                    )
                    for product, total in e.groupby("product").kg.sum().items():
                        boundary.append(
                            {
                                **context,
                                "indicator": m["indicator"],
                                "product": product,
                                "route": route,
                                "attributed_kg": total,
                            }
                        )
            for r in group.to_dict("records"):
                root, amount = inv.demand(r)
                root_children = children(inv, root)
                for i, c in root_children:
                    if c == 0:
                        continue
                    d = {
                        **context,
                        "variable": r["variable"],
                        "root": r["name"],
                        "annual_MJ": amount,
                        "supplier": i,
                        **{"supplier_" + a: b for a, b in inv.node(i).items()},
                        "input_per_MJ": c,
                        "annual_input_quantity": amount * c,
                    }
                    direct.append(d)
                    for k in selected:
                        equipment.append(
                            {
                                **d,
                                "indicator": methods[k]["indicator"],
                                "input_intensity_kg_per_unit": inv.Z[k, i],
                                "branch_kg": amount * c * inv.Z[k, i],
                                "consumer_total_kg": amount * inv.Z[k, root],
                            }
                        )
                # Counterfactuals preserve operating electricity, other components and mix.
                deltas = []
                is_car = r["name"].startswith(
                    "electricity, used in passenger car vehicle"
                )
                is_truck = r["name"] == "electricity, used in heavy duty vehicle"
                if is_car:
                    factor = 0.75 / 0.703 - 1
                    for i, c in root_children:
                        if any(
                            x in inv.nodes.loc[i, "name"]
                            for x in [
                                "market for electric motor",
                                "market for inverter",
                                "market for charger",
                                "market for battery capacity",
                            ]
                        ):
                            deltas.append(
                                (
                                    "car_documented_0.703_MJ_per_km_formula_cells_only",
                                    amount * c * factor * inv.Z[:, i],
                                )
                            )
                if is_truck:
                    assert not any(
                        inv.nodes.loc[i, "reference product"]
                        == "electricity, low voltage"
                        and inv.nodes.loc[i, "unit"] == "kilowatt hour"
                        for i, c in root_children
                    )
                    deltas.append(
                        (
                            "truck_add_operating_electricity_1MJ_per_MJ",
                            amount / 3.6 * inv.Z[:, electricity],
                        )
                    )
                    for i, c in root_children:
                        if (
                            inv.nodes.loc[i, "reference product"]
                            == "electricity storage capacity"
                        ):
                            deltas.append(
                                (
                                    "truck_battery_documented_1million_km_5.447MJ_per_km",
                                    amount * (283 / 1e6 / 5.447 - c) * inv.Z[:, i],
                                )
                            )
                for i, c in root_children:
                    if i == mix:
                        deltas.append(
                            (
                                "battery_no_LiS_renormalize_remaining_capacity_shares",
                                amount * c * (mix_no_lis - inv.Z[:, mix]),
                            )
                        )
                for sensitivity in sorted({name for name, delta in deltas}):
                    delta = sum(v for name, v in deltas if name == sensitivity)
                    for k in selected:
                        effects.append(
                            {
                                **context,
                                "variable": r["variable"],
                                "sensitivity": sensitivity,
                                "indicator": methods[k]["indicator"],
                                "current_consumer_kg": amount * inv.Z[k, root],
                                "change_kg": float(delta[k]),
                                "current_system_kg": system_totals[k],
                            }
                        )
                if (
                    "heat_pump" in r["variable"]
                    and not r["variable"].endswith("_gas")
                    and r["unit"] == "megajoule"
                ):
                    for i, c in root_children:
                        if "heat" not in inv.nodes.loc[i, "reference product"]:
                            continue
                        nested = children(inv, i)
                        power = sum(
                            c * v
                            for j, v in nested
                            if inv.nodes.loc[j, "reference product"]
                            == "electricity, low voltage"
                            and inv.nodes.loc[j, "unit"] == "kilowatt hour"
                        )
                        if power:
                            heat.append(
                                {
                                    **context,
                                    "variable": r["variable"],
                                    "heat_MJ_per_final_MJ": c,
                                    "operating_kWh_per_final_MJ": power,
                                    "operating_MJ_per_final_MJ": power * 3.6,
                                    "passed": bool(
                                        np.isclose(power * 3.6, 1, rtol=1e-10)
                                    ),
                                }
                            )
            for name, rows in [
                ("first_tier_inputs", direct),
                ("battery_chemistry_coefficients", battery),
                ("coefficient_sensitivities", effects),
                ("heat_pump_energy_balance", heat),
                ("equipment_branch_backcalculations", equipment),
                ("product_boundary_contributions", boundary),
            ]:
                pd.DataFrame(rows).to_parquet(OUT / f"{name}.parquet", index=False)
            print(f"Coefficient review {scenario}/{year}", flush=True)
    write_json(
        OUT / "coefficient_summary.json",
        {
            "raw_STEM_checks": len(raw),
            "raw_STEM_failures": sum(not r["passed"] for r in raw),
            "heat_pump_checks": len(heat),
            "heat_pump_failures": sum(not r["passed"] for r in heat),
            "sensitivities": "Unapplied counterfactuals; sum changes by scenario/year/indicator within a sensitivity, never across sensitivities.",
            "sources_sha256": {
                str(p): digest(p)
                for p in [
                    XL,
                    RAW,
                    Path(__file__),
                    BASE / "build/inputs/configuration_file/config.yaml",
                ]
            },
        },
    )
    shutil.copy2(__file__, OUT / Path(__file__).name)


if __name__ == "__main__":
    run()
