"""Independent, seeded audit of final-energy material quantities and supply chains.

Reads original package CSVs and uses adjoint UMFPACK solves; does not import
Edges, Pathways, or their coefficient-selection/calculation helpers.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import heapq
import json
from pathlib import Path
import shutil
from zipfile import ZipFile

import numpy as np
import pandas as pd
from scipy import sparse
from scikits.umfpack import splu, UMFPACK_At
import yaml

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261012
SECTORS = ["industry", "residential", "services", "transport"]
COMMON = [
    "Copper",
    "Lithium",
    "Nickel",
    "Cobalt",
    "Neodymium",
    "Silicon",
    "Zinc",
    "Lead",
    "Silver",
    "Platinum",
    "Aluminium",
    "Aluminium alloys",
    "Iron and steel alloys",
]
TARGETS = [
    "cars_electricity",
    "light_duty_truck_electricity",
    "heavy_duty_truck_electricity",
    "residential_space_heating_heat_pump",
    "services_other_electric",
    "industry_process_FE_other_electric",
]


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=lambda x: x.item()) + "\n")


class Inventory:
    def __init__(self, package, year, methods):
        with ZipFile(package) as archive:
            (name,) = [
                n for n in archive.namelist() if n.endswith(f"/{year}/A_matrix.csv")
            ]
            self.raw = pd.read_csv(
                archive.open(name), sep=";", float_precision="round_trip"
            )
            self.nodes = (
                pd.read_csv(
                    archive.open(name.replace("A_matrix.csv", "A_matrix_index.csv")),
                    sep=";",
                )
                .set_index("index")
                .sort_index()
            )
            self.scenario_data = pd.read_csv(
                archive.open("scenario_data/scenario_data.csv"),
                float_precision="round_trip",
            )
            self.mapping = yaml.safe_load(archive.read("mapping/mapping.yaml"))
        assert list(self.nodes.index) == list(range(len(self.nodes)))
        a = self.raw
        self.A = sparse.coo_matrix(
            (
                a.value.to_numpy() * np.where(a.flip.eq(1), -1.0, 1.0),
                (a["index of product"], a["index of activity"]),
            ),
            shape=(len(self.nodes), len(self.nodes)),
        ).tocsr()
        self.columns = self.A.tocsc()
        self.factor = splu(self.columns)
        self.methods = methods
        self.C = np.zeros((len(methods), len(self.nodes)))
        self.edges = []
        self.internal = []
        products = self.nodes["reference product"].to_numpy()
        for k, method in enumerate(methods):
            definition = method["definition"]
            factors = {p["product"]: p["factor"] for p in definition["products"]}
            ids = self.nodes.index[
                self.nodes["reference product"].isin(factors)
            ].to_numpy()
            assert self.nodes.loc[ids, "unit"].eq("kilogram").all()
            selected = self.A[ids].tocoo()
            suppliers, consumers = ids[selected.row], selected.col
            factor = np.array([factors[products[i]] for i in suppliers])
            input_edge = (selected.data < 0) & (suppliers != consumers)
            internal = np.isin(products[consumers], definition["internal_products"])
            for container, mask in [
                (self.edges, input_edge & ~internal),
                (self.internal, input_edge & internal),
            ]:
                container.append(
                    pd.DataFrame(
                        {
                            "supplier": suppliers[mask],
                            "consumer": consumers[mask],
                            "amount_per_activity": -selected.data[mask],
                            "factor": factor[mask],
                        }
                    )
                )
            edge = self.edges[-1]
            self.C[k] = np.bincount(
                edge.consumer,
                weights=edge.amount_per_activity * edge.factor,
                minlength=len(self.nodes),
            )
        # kg material per native unit of every product, computed by A^T z = c.
        self.Z = np.array(
            [
                self.factor.umf.solve(
                    UMFPACK_At, self.factor._A, row, autoTranspose=True
                )
                for row in self.C
            ]
        )
        self.initial_Z = self.Z.copy()
        # Extended-precision residuals prevent cancellation in the differently
        # scaled physical units of the inventory. Corrections use the same LU.
        extended = self.A.T.astype(np.longdouble)
        self.refinement_residuals = []
        for _ in range(3):
            residual = (
                self.C.astype(np.longdouble)
                - (extended @ self.Z.T.astype(np.longdouble)).T
            )
            self.refinement_residuals.append(float(np.max(np.abs(residual))))
            self.Z += np.array(
                [
                    self.factor.umf.solve(
                        UMFPACK_At,
                        self.factor._A,
                        np.asarray(row, dtype=float),
                        autoTranspose=True,
                    )
                    for row in residual
                ]
            )
        assert np.isfinite(self.Z).all()
        self.identity = {
            tuple(r): i
            for i, r in self.nodes[
                ["name", "reference product", "unit", "location"]
            ].iterrows()
        }

    def node(self, position):
        return self.nodes.loc[int(position)].to_dict()

    def demand(self, row):
        matches = self.scenario_data.loc[
            self.scenario_data.region.eq("CH")
            & self.scenario_data.variables.eq(row["variable"])
            & self.scenario_data.year.eq(row["year"])
        ]
        assert len(matches) == 1
        source = matches.iloc[0]
        assert source.unit == row["scenario_unit"] == "PJ/yr."
        assert row["unit"] == "megajoule" and row["conversion_factor"] == 1e9
        amount = max(0.0, float(source.value)) * 1_000_000_000
        np.testing.assert_allclose(
            amount,
            row["scenario_demand"] * row["conversion_factor"],
            rtol=1e-13,
            atol=1e-6,
        )
        idx = self.identity[
            (row["name"], row["reference_product"], row["unit"], row["location"])
        ]
        allowed = self.mapping[row["variable"]]["dataset"]
        assert any(
            (d["name"], d["reference product"], d["unit"])
            == (row["name"], row["reference_product"], row["unit"])
            for d in allowed
        )
        return idx, amount

    def solve(self, rhs):
        supply = self.factor.solve(rhs)
        denominator = float(abs(self.A).sum(axis=1).max()) * np.max(
            np.abs(supply), axis=0
        ) + np.max(np.abs(rhs), axis=0)
        error = np.max(np.abs(self.A @ supply - rhs), axis=0)
        backward = np.divide(
            error, denominator, out=np.zeros_like(error), where=denominator != 0
        )
        assert np.max(backward) < 1e-12
        return supply, backward

    def decompose(self, root, amount, method, max_nodes=100, max_depth=12):
        """Expand large supply-chain branches; keep an exact signed residual.

        Every row records a quantity times coefficient calculation. Unexpanded
        branches and cycles are retained as residual kg, never silently dropped.
        """
        z = self.Z[method]
        queue = [(-abs(amount * z[root]), 0, root, amount, (), -1)]
        rows, sequence, accounted, residual = [], 0, 0.0, 0.0
        while queue:
            _, _, node, quantity, ancestors, parent = heapq.heappop(queue)
            score = quantity * z[node]
            expand = (
                len(rows) < max_nodes
                and len(ancestors) < max_depth
                and node not in ancestors
            )
            scale = quantity / self.A[node, node]
            own = scale * self.C[method, node]
            row_id = len(rows)
            row = {
                "row": row_id,
                "parent": parent,
                "depth": len(ancestors),
                "activity": node,
                **self.node(node),
                "required_product_quantity": quantity,
                "input_amount_per_parent_activity": (
                    quantity / rows[parent]["activity_scale"]
                    if parent >= 0 and rows[parent]["activity_scale"]
                    else None
                ),
                "net_diagonal": float(self.A[node, node]),
                "activity_scale": scale,
                "upstream_kg_per_product_unit": z[node],
                "attributed_kg": score,
                "direct_counted_kg": own if expand else 0.0,
                "residual_kg": 0.0 if expand else score,
                "expanded": expand,
                "reason": (
                    "expanded"
                    if expand
                    else (
                        "cycle"
                        if node in ancestors
                        else (
                            "depth limit"
                            if len(ancestors) >= max_depth
                            else "node limit"
                        )
                    )
                ),
            }
            rows.append(row)
            if not expand:
                residual += score
                continue
            accounted += own
            start, end = self.columns.indptr[node : node + 2]
            children = []
            for supplier, value in zip(
                self.columns.indices[start:end], self.columns.data[start:end]
            ):
                if supplier == node:
                    continue
                q = -value * scale
                impact = q * z[supplier]
                if impact == 0:
                    continue
                sequence += 1
                children.append(
                    (
                        -abs(impact),
                        sequence,
                        int(supplier),
                        float(q),
                        (*ancestors, node),
                        row_id,
                    )
                )
            # Keep only dominant branches expanded; all children remain explicit.
            for item in children:
                heapq.heappush(queue, item)
            reconstructed = own + sum(item[3] * z[item[2]] for item in children)
            np.testing.assert_allclose(reconstructed, score, rtol=1e-9, atol=1e-6)
        np.testing.assert_allclose(
            accounted + residual, amount * z[root], rtol=1e-9, atol=1e-6
        )
        return rows, {
            "direct_accounted_kg": accounted,
            "unexpanded_residual_kg": residual,
            "total_kg": amount * z[root],
        }


def sampling_plan(results):
    rng = np.random.default_rng(SEED)
    cases = []
    for n in range(1, 5):
        scenario = f"SPS{n}_bas0"
        demands = pd.read_csv(
            results / "scenarios" / scenario / "pathways_demands.csv",
            float_precision="round_trip",
        )
        for year, group in demands.groupby("year", sort=True):
            detailed_sector = SECTORS[int(rng.integers(4))]
            for sector in SECTORS:
                eligible = group[
                    group.sector.eq(sector) & group.scenario_demand.gt(1e-8)
                ].sort_values("variable")
                selected = eligible.iloc[int(rng.integers(len(eligible)))]
                cases.append(
                    {
                        "scenario": scenario,
                        "year": int(year),
                        "variable": selected.variable,
                        "sector": sector,
                        "selection": "seeded random active",
                        "detailed": sector == detailed_sector,
                        "trace_metals": rng.choice(COMMON, 2, replace=False).tolist(),
                    }
                )
            zero = group[group.scenario_demand.eq(0)].sort_values("variable")
            selected = zero.iloc[int(rng.integers(len(zero)))]
            cases.append(
                {
                    "scenario": scenario,
                    "year": int(year),
                    "variable": selected.variable,
                    "sector": selected.sector,
                    "selection": "seeded random inactive",
                    "detailed": False,
                    "trace_metals": [],
                }
            )
            for suffix in TARGETS:
                variable = "EXT - 0 - FE_" + suffix
                selected = group[group.variable.eq(variable)].iloc[0]
                if selected.scenario_demand <= 1e-8 or any(
                    r["scenario"] == scenario
                    and r["year"] == year
                    and r["variable"] == variable
                    for r in cases
                ):
                    continue
                cases.append(
                    {
                        "scenario": scenario,
                        "year": int(year),
                        "variable": variable,
                        "sector": selected.sector,
                        "selection": "targeted high-contribution archetype",
                        "detailed": n == 1 and year in [2020, 2050],
                        "trace_metals": [
                            "Copper",
                            "Lithium",
                            "Neodymium",
                            "Iron and steel alloys",
                        ],
                    }
                )
    return cases


def run(args):
    results, output = args.results.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    original_summary = json.loads((results / "completion_summary.json").read_text())
    for name, expected in original_summary["outputs_sha256"].items():
        assert digest(results / name) == expected
    registry = json.loads((results / "method_registry.json").read_text())
    methods = [m for m in registry["methods"] if "definition" in m]
    plan = sampling_plan(results)
    write_json(
        output / "sampling_plan.json",
        {
            "seed": SEED,
            "selection": "One positive demand per sector and one zero per scenario-year, sampled without selecting on scores; targeted archetypes are additional and labelled separately.",
            "cases": plan,
        },
    )
    pd.DataFrame(plan).to_csv(output / "sample.csv", index=False)
    source_results = pd.read_parquet(results / "refined_metal_results.parquet")
    saved_locations = source_results.set_index(
        ["scenario", "year", "variable", "impact_category", "location"]
    ).value
    totals = source_results.groupby(
        ["scenario", "year", "variable", "impact_category"]
    ).value.sum()
    (
        checks,
        region_checks,
        top_edges,
        traces,
        trace_summaries,
    ) = ([], [], [], [], [])
    system_checks, solver_diagnostics = [], []
    provenance = json.loads((results / "provenance.json").read_text())
    write_json(
        output / "audit_inputs.json",
        {
            "original_summary_sha256": digest(results / "completion_summary.json"),
            "original_provenance": provenance,
            "method_registry_sha256": digest(results / "method_registry.json"),
            "sampling_plan_sha256": digest(output / "sampling_plan.json"),
            "script_sha256": digest(__file__),
        },
    )
    shutil.copy2(__file__, output / Path(__file__).name)
    for n in range(1, 5):
        scenario = f"SPS{n}_bas0"
        if args.scenarios and scenario not in args.scenarios:
            continue
        package = next(args.packages.glob(f"*-stem-{scenario}.zip"))
        assert digest(package) == provenance["packages"][package.name]
        demands = pd.read_csv(
            results / "scenarios" / scenario / "pathways_demands.csv",
            float_precision="round_trip",
        )
        manifest = json.loads(
            next(
                (args.base_results / "scenarios" / scenario).glob("*.manifest.json")
            ).read_text()
        )
        for year, group in demands.groupby("year", sort=True):
            if args.years and year not in args.years:
                continue
            inv = Inventory(package, int(year), methods)
            ids, amounts = zip(*(inv.demand(r) for r in group.to_dict("records")))
            ids, amounts = np.array(ids), np.array(amounts)
            predictions = inv.Z[:, ids] * amounts
            rhs_all = np.zeros((len(inv.nodes), len(group)))
            rhs_all[ids, np.arange(len(group))] = amounts
            forward_all, forward_error = inv.solve(rhs_all)
            forward_predictions = inv.C @ forward_all
            solver_diagnostics.append(
                {
                    "scenario": scenario,
                    "year": int(year),
                    "adjoint_refinement_residuals": inv.refinement_residuals,
                    "forward_backward_error": float(max(forward_error)),
                }
            )
            full_scenario = f"SSP2-PkBudg1000 - {scenario}"
            for k, method in enumerate(methods):
                for j, row in enumerate(group.to_dict("records")):
                    expected = float(
                        totals.loc[
                            (full_scenario, year, row["variable"], method["indicator"])
                        ]
                    )
                    actual = predictions[k, j]
                    checks.append(
                        {
                            "scenario": scenario,
                            "year": int(year),
                            "variable": row["variable"],
                            "indicator": method["indicator"],
                            "unit": method["unit"],
                            "saved_edges_kg": expected,
                            "adjoint_kg": actual,
                            "difference_kg": actual - expected,
                            "initial_adjoint_kg": inv.initial_Z[k, ids[j]] * amounts[j],
                            "independent_forward_kg": forward_predictions[k, j],
                            "forward_passed": bool(
                                np.isclose(
                                    forward_predictions[k, j],
                                    expected,
                                    rtol=1e-8,
                                    atol=1e-6,
                                )
                            ),
                            "within_one_ppm": bool(
                                np.isclose(actual, expected, rtol=1e-6, atol=1e-6)
                            ),
                            "passed": bool(
                                np.isclose(actual, expected, rtol=1e-8, atol=1e-6)
                            ),
                            "kg_per_GJ": inv.Z[k, ids[j]] * 1000,
                        }
                    )
            selected = [
                r for r in plan if r["scenario"] == scenario and r["year"] == year
            ]
            by_variable = group.set_index("variable")
            rhs = np.zeros((len(inv.nodes), len(selected) + 1))
            for i, case in enumerate(selected):
                root, amount = inv.demand(
                    {
                        "variable": case["variable"],
                        **by_variable.loc[case["variable"]].to_dict(),
                    }
                )
                rhs[root, i] = amount
            np.add.at(rhs[:, -1], ids, amounts)
            supply, error = inv.solve(rhs)
            annual = inv.C @ supply[:, -1]
            for k, method in enumerate(methods):
                saved_total = float(
                    totals.loc[(full_scenario, year)]
                    .xs(method["indicator"], level="impact_category")
                    .sum()
                )
                system_checks.append(
                    {
                        "scenario": scenario,
                        "year": int(year),
                        "indicator": method["indicator"],
                        "saved_kg": saved_total,
                        "adjoint_kg": float(predictions[k].sum()),
                        "independent_forward_kg": float(annual[k]),
                        "difference_kg": float(annual[k] - saved_total),
                        "passed": bool(
                            np.isclose(annual[k], saved_total, rtol=1e-8, atol=1e-6)
                        ),
                    }
                )
            locations = np.where(
                inv.nodes.location.eq("CH"),
                "CH",
                np.where(
                    inv.nodes.location.isin(manifest["europe_locations"]),
                    "EU wo CH",
                    "RoW",
                ),
            )
            for k, method in enumerate(methods):
                for i, case in enumerate(selected):
                    for location in ["CH", "EU wo CH", "RoW"]:
                        value = float(
                            (inv.C[k] * (locations == location)) @ supply[:, i]
                        )
                        saved = saved_locations.loc[
                            (
                                full_scenario,
                                year,
                                case["variable"],
                                method["indicator"],
                                location,
                            )
                        ]
                        region_checks.append(
                            {
                                "scenario": scenario,
                                "year": int(year),
                                "variable": case["variable"],
                                "indicator": method["indicator"],
                                "location": location,
                                "saved_kg": saved,
                                "independent_kg": value,
                                "passed": bool(
                                    np.isclose(saved, value, rtol=1e-8, atol=1e-6)
                                ),
                                "backward_error": float(error[i]),
                            }
                        )
                edge = inv.edges[k].copy()
                edge["attributed_kg"] = (
                    edge.amount_per_activity * edge.factor * supply[edge.consumer, -1]
                )
                for _, r in edge.iloc[
                    np.argsort(-np.abs(edge.attributed_kg.to_numpy()))[:15]
                ].iterrows():
                    top_edges.append(
                        {
                            "scenario": scenario,
                            "year": int(year),
                            "indicator": method["indicator"],
                            **r.to_dict(),
                            **{
                                "supplier_" + k: v
                                for k, v in inv.node(r.supplier).items()
                            },
                            **{
                                "consumer_" + k: v
                                for k, v in inv.node(r.consumer).items()
                            },
                            "consumer_activity_scale": float(
                                supply[int(r.consumer), -1]
                            ),
                        }
                    )
            for case in selected:
                if not case["detailed"]:
                    continue
                root, amount = inv.demand(
                    {
                        "variable": case["variable"],
                        **by_variable.loc[case["variable"]].to_dict(),
                    }
                )
                for metal in case["trace_metals"]:
                    k = next(i for i, m in enumerate(methods) if m["label"] == metal)
                    rows, summary = inv.decompose(root, amount, k)
                    context = {
                        "scenario": scenario,
                        "year": int(year),
                        "variable": case["variable"],
                        "indicator": methods[k]["indicator"],
                        "selection": case["selection"],
                    }
                    traces.extend({**context, **r} for r in rows)
                    trace_summaries.append({**context, **summary})
            print(
                f"Audited {scenario}/{year}: {len(group)*len(methods)} adjoint checks, {len(selected)} sampled consumers",
                flush=True,
            )
            # Persist after each matrix so a later diagnostic failure is reviewable.
            for name, rows in [
                ("adjoint_checks", checks),
                ("location_checks", region_checks),
                ("dominant_edges", top_edges),
                ("supply_chain_steps", traces),
                ("trace_reconciliation", trace_summaries),
                ("system_checks", system_checks),
            ]:
                pd.DataFrame(rows).to_parquet(output / f"{name}.parquet", index=False)
            write_json(output / "solver_diagnostics.json", solver_diagnostics)
    write_json(
        output / "numerical_summary.json",
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "seed": SEED,
            "consumer_cases": len(plan),
            "processed_consumer_cases": len(
                {(r["scenario"], r["year"], r["variable"]) for r in region_checks}
            ),
            "adjoint_checks": len(checks),
            "adjoint_failures": sum(not r["passed"] for r in checks),
            "forward_failures": sum(not r["forward_passed"] for r in checks),
            "outside_one_ppm": sum(not r["within_one_ppm"] for r in checks),
            "system_checks": len(system_checks),
            "system_failures": sum(not r["passed"] for r in system_checks),
            "location_checks": len(region_checks),
            "location_failures": sum(not r["passed"] for r in region_checks),
            "detailed_traces": len(trace_summaries),
            "max_absolute_difference_kg": max(abs(r["difference_kg"]) for r in checks),
            "original_results_sha256": digest(results / "completion_summary.json"),
            "script_sha256": digest(__file__),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results", type=Path, default=ROOT / "results/sps_base/refined_metals"
    )
    parser.add_argument(
        "--base-results", type=Path, default=ROOT / "results/sps_base/indicators"
    )
    parser.add_argument(
        "--packages", type=Path, default=ROOT / "results/sps_base/build/packages"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/sps_base/metal_audit_20261012"
    )
    parser.add_argument("--scenarios", nargs="+")
    parser.add_argument("--years", nargs="+", type=int)
    run(parser.parse_args())
