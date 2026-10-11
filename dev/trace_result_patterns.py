"""Trace selected final-energy results through the exported inventory matrices."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse.linalg import spsolve

from workflow_audit_core import MatrixAudit, digest


def trace(batch, scenario, year, variables):
    folder = batch / "scenarios" / scenario
    manifest = json.loads(next(folder.glob("*.manifest.json")).read_text())
    package = Path(manifest["package"])
    cf = Path(manifest["pathways_source"]) / "data/lcia_ei312.json"
    assert digest(package) == manifest["calculation"]["package_sha256"]
    assert digest(cf) == manifest["calculation"]["cf_sha256"]
    output = batch / "pattern_audit" / f"{scenario}_{year}"
    output.mkdir(parents=True, exist_ok=True)
    matrix = MatrixAudit(
        package, year, cf, manifest["calculation"]["methods"], solver="superlu"
    )
    demands = pd.read_csv(
        next(folder.glob("*_demands.csv")), float_precision="round_trip"
    )
    selected = demands.loc[
        demands.year.eq(year)
        & demands.variable.str.removeprefix("EXT - 0 - ").isin(variables)
    ].reset_index(drop=True)
    assert len(selected) == len(variables)
    selected["index"] = [
        matrix.find(r["name"], r.reference_product, r.unit, r.location)
        for _, r in selected.iterrows()
    ]
    selected["amount"] = selected.scenario_demand * selected.conversion_factor
    assert selected.amount.gt(0).all()
    f = np.zeros((len(matrix.ai), len(selected)))
    for column, row in selected.iterrows():
        f[int(row["index"]), column] = row.amount
    supply = spsolve(matrix.A, f, use_umfpack=False)
    if supply.ndim == 1:
        supply = supply[:, None]
    residual = np.max(np.abs(matrix.A @ supply - f), axis=0)
    denominator = float(abs(matrix.A).sum(axis=1).max()) * np.max(
        np.abs(supply), axis=0
    ) + np.max(np.abs(f), axis=0)
    backward = np.divide(
        residual, denominator, out=np.zeros_like(residual), where=denominator != 0
    )
    assert np.isfinite(supply).all() and np.max(backward) < 1e-12
    inventory = matrix.B @ supply
    scores = matrix.C @ inventory
    results = pd.read_parquet(batch / "df_final.parquet")
    results = results.loc[
        results.scenario.str.endswith(" - " + scenario) & results.year.eq(year)
    ]
    checks = []
    for column, row in selected.iterrows():
        variable = row.variable.removeprefix("EXT - 0 - ")
        matrix.exchanges(int(row["index"])).to_csv(
            output / f"{variable}_inputs.csv", index=False
        )
        for k, method in enumerate(manifest["calculation"]["methods"]):
            indicator = manifest["methods"][method][0]
            expected = results.loc[
                results.variable.eq(row.variable)
                & results.impact_category.eq(indicator),
                "value",
            ].sum()
            actual = float(scores[k, column])
            np.testing.assert_allclose(actual, expected, rtol=1e-8, atol=1e-5)
            checks.append(
                {
                    "variable": variable,
                    "indicator": indicator,
                    "demand_PJ": row.scenario_demand,
                    "score": actual,
                    "expected": expected,
                    "score_per_inventory_unit": actual / row.amount,
                    "score_per_PJ": actual / row.scenario_demand,
                    "backward_error": float(backward[column]),
                }
            )
            label = indicator.replace(" ", "_")
            direct = matrix.contributions(supply[:, column] / row.amount, k)
            direct.head(35).to_csv(
                output / f"{variable}_{label}_direct.csv", index=False
            )
            flows = matrix.bi.copy()
            flows["inventory_per_unit"] = inventory[:, column] / row.amount
            flows["cf"] = matrix.C[k]
            flows["score_per_unit"] = flows.inventory_per_unit * flows.cf
            flows.loc[flows.score_per_unit.ne(0)].sort_values(
                "score_per_unit", key=abs, ascending=False
            ).head(35).to_csv(output / f"{variable}_{label}_flows.csv")
            matrix.supplier_contributions(int(row["index"]), k).to_csv(
                output / f"{variable}_{label}_suppliers.csv", index=False
            )
    # Follow the largest absolute land contributions down the immediate supply chain.
    land = next(
        i
        for i, m in enumerate(manifest["calculation"]["methods"])
        if manifest["methods"][m][0] == "land occupation"
    )
    visited = set()
    pending = [(int(row["index"]), 0) for _, row in selected.iterrows()]
    chain = []
    while pending:
        index, depth = pending.pop(0)
        if index in visited or depth > 4:
            continue
        visited.add(index)
        ex = matrix.supplier_contributions(index, land)
        metadata = matrix.ai.loc[index].to_dict()
        for record in ex.head(6).to_dict("records"):
            record.update(
                consumer_index=index,
                consumer=metadata["name"],
                consumer_unit=metadata["unit"],
                consumer_location=metadata["location"],
                depth=depth,
            )
            chain.append(record)
        # Only expand the first three suppliers when they explain material land use.
        total = float(matrix.upstream_intensities()[land, index])
        for record in ex.head(3).to_dict("records"):
            if abs(record["contribution_per_output"]) > max(abs(total) * 0.05, 1e-5):
                pending.append((int(record["index of product"]), depth + 1))
    pd.DataFrame(chain).to_csv(output / "land_supply_chain.csv", index=False)
    # These market tables explain changes in shared electricity and fuel inputs.
    for market, location, label in [
        ("market for electricity, high voltage (SPS)", "CH", "electricity"),
        ("market for diesel (SPS)", "CH", "diesel"),
        ("market for methane (SPS)", "CH", "methane"),
        ("market for lignocellulosic biomass, used as fuel", "NEU", "biomass"),
    ]:
        index = matrix.find(market, location=location)
        for k, method in enumerate(manifest["calculation"]["methods"]):
            indicator = manifest["methods"][method][0].replace(" ", "_")
            matrix.supplier_contributions(index, k).to_csv(
                output / f"market_{label}_{indicator}.csv", index=False
            )
    pd.DataFrame(checks).to_csv(output / "independent_variable_checks.csv", index=False)
    summary = {
        "scenario": scenario,
        "year": year,
        "variables": variables,
        "checks": len(checks),
        "passed": True,
        "maximum_backward_error": float(np.max(backward)),
        "package_sha256": digest(package),
        "cf_sha256": digest(cf),
    }
    (output / "verification.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("scenario")
    parser.add_argument("year", type=int)
    parser.add_argument("variables", nargs="+")
    args = parser.parse_args()
    trace(args.batch.resolve(), args.scenario, args.year, args.variables)
