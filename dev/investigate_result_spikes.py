"""Read-only screening and matrix contribution diagnosis of indicator anomalies."""

import argparse
import json
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
from scipy.sparse.linalg import spsolve

from workflow_audit_core import MatrixAudit, digest

WATER = "net use of fresh water"


def screen(batch, output):
    totals = pd.read_csv(batch / "indicator_totals.csv", float_precision="round_trip")
    findings = []
    for (scenario, indicator), group in totals.groupby(["scenario", "impact_category"]):
        group = group.sort_values("year")
        years, values = group.year.to_numpy(), group.value.to_numpy()
        scale = np.max(np.abs(values))
        for i in range(1, len(values) - 1):
            weight = (years[i] - years[i - 1]) / (years[i + 1] - years[i - 1])
            expected = values[i - 1] + weight * (values[i + 1] - values[i - 1])
            difference = values[i] - expected
            findings.append(
                {
                    "scenario": scenario,
                    "indicator": indicator,
                    "year": int(years[i]),
                    "value": values[i],
                    "previous": values[i - 1],
                    "next": values[i + 1],
                    "neighbor_interpolation": expected,
                    "deviation": difference,
                    "deviation_fraction_of_peak": (
                        abs(difference) / scale if scale else 0.0
                    ),
                    "direction_reversal": bool(
                        (values[i] - values[i - 1]) * (values[i + 1] - values[i]) < 0
                    ),
                }
            )
    scan = pd.DataFrame(findings).sort_values(
        "deviation_fraction_of_peak", ascending=False
    )
    scan.to_csv(output / "all_temporal_checks.csv", index=False)
    scan.loc[scan.deviation_fraction_of_peak.gt(0.08)].to_csv(
        output / "flagged_temporal_checks.csv", index=False
    )


def trace(batch, output, scenario, year):
    folder = batch / "scenarios" / scenario
    manifest = json.loads(next(folder.glob("*.manifest.json")).read_text())
    package = Path(manifest["package"])
    assert digest(package) == manifest["calculation"]["package_sha256"]
    methods = [
        method
        for method in manifest["calculation"]["methods"]
        if manifest["methods"][method][0] == WATER
    ]
    cf = Path(manifest["pathways_source"]) / "data/lcia_ei312.json"
    assert digest(cf) == manifest["calculation"]["cf_sha256"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        print(scenario, year, "loading matrices", flush=True)
        matrix = MatrixAudit(package, year, cf, methods, solver="umfpack")
    demands = pd.read_csv(
        next(folder.glob("*_demands.csv")), float_precision="round_trip"
    )
    demands = demands.loc[demands.year.eq(year)].reset_index(drop=True)
    f = np.zeros((len(matrix.ai), len(demands)))
    demand_indices = []
    for col, row in demands.iterrows():
        idx = matrix.find(row["name"], row.reference_product, row.unit, row.location)
        f[idx, col] += row.scenario_demand * row.conversion_factor
        demand_indices.append(idx)
    # Match the independently validated, separate-demand CSR SuperLU solve.
    # Summed-demand UMFPACK has small roundoff differences in these matrices.
    supply = spsolve(matrix.A, f, use_umfpack=False)
    residual = np.max(np.abs(matrix.A @ supply - f), axis=0)
    denominator = float(abs(matrix.A).sum(axis=1).max()) * np.max(
        np.abs(supply), axis=0
    ) + np.max(np.abs(f), axis=0)
    backward = np.divide(
        residual, denominator, out=np.zeros_like(residual), where=denominator != 0
    )
    assert np.isfinite(supply).all() and np.max(backward) < 1e-12
    scores = matrix.C @ (matrix.B @ supply)
    totals = pd.read_csv(batch / "indicator_totals.csv", float_precision="round_trip")
    prefix = f"{scenario}_{year}"
    records = []
    for method_idx, method in enumerate(methods):
        short = manifest["methods"][method][0]
        expected = totals.loc[
            totals.scenario.str.endswith(" - " + scenario)
            & totals.year.eq(year)
            & totals.impact_category.eq(short),
            "value",
        ].item()
        actual = float(scores[method_idx].sum())
        np.testing.assert_allclose(actual, expected, rtol=1e-8, atol=1e-5)
        records.append(
            {
                "indicator": short,
                "score": actual,
                "expected": expected,
                "relative_difference": abs(actual - expected)
                / max(abs(expected), 1e-30),
            }
        )
    x = supply.sum(axis=1)
    m = next(
        i for i, name in enumerate(methods) if manifest["methods"][name][0] == WATER
    )
    direct = matrix.contributions(x, m)
    direct.head(80).to_csv(
        output / f"{prefix}_water_direct_activities.csv", index=False
    )
    flows = matrix.bi.copy()
    flows["inventory"] = matrix.B @ x
    flows["cf"] = matrix.C[m]
    flows["score"] = flows.inventory * flows.cf
    flows.loc[flows.score.ne(0)].sort_values("score", key=abs, ascending=False).to_csv(
        output / f"{prefix}_water_flows.csv"
    )
    annual = demands.copy()
    annual["water_score"] = scores[m]
    annual["water_score_adjoint"] = (
        matrix.upstream_intensities()[m, demand_indices]
        * demands.scenario_demand
        * demands.conversion_factor
    )
    annual["water_per_PJ"] = (
        matrix.upstream_intensities()[m, demand_indices] * demands.conversion_factor
    )
    annual.to_csv(output / f"{prefix}_demand_water.csv", index=False)
    for voltage in ["high", "medium", "low"]:
        idx = matrix.find(
            f"market for electricity, {voltage} voltage (SPS)", location="CH"
        )
        matrix.supplier_contributions(idx, m).to_csv(
            output / f"{prefix}_{voltage}_voltage_suppliers.csv", index=False
        )
    for idx in direct.head(8)["index"]:
        exchanges = matrix.exchanges(idx)
        exchanges.to_csv(output / f"{prefix}_activity_{idx}_inputs.csv", index=False)
        bio = matrix.b.loc[matrix.b["index of activity"].eq(idx)].merge(
            matrix.bi, left_on="index of biosphere flow", right_index=True
        )
        bio["water_cf"] = matrix.C[m][bio["index of biosphere flow"].to_numpy()]
        bio.loc[bio.water_cf.ne(0)].to_csv(
            output / f"{prefix}_activity_{idx}_water_exchanges.csv", index=False
        )
    pump = matrix.find(
        "electricity production, hydro, pumped storage_hydro (pumped storage)",
        "electricity, high voltage",
        "kilowatt hour",
        "CH",
    )
    grid = matrix.find(
        "market for electricity, high voltage (SPS)",
        "electricity, high voltage",
        "kilowatt hour",
        "CH",
    )
    inputs = matrix.exchanges(pump)
    current_input = inputs.loc[
        inputs["index of product"].eq(grid) & inputs.flip.eq(1), "value"
    ].item()
    source = pd.read_csv(
        package.parent.parent / "inputs/scenario_data/scenario_data.csv",
        float_precision="round_trip",
    )
    eta = source.loc[
        source.scenario.eq(scenario)
        & source.variables.eq(
            "Efficiency of power plants (electric)|Hydro|Pump storage"
        ),
        str(year),
    ].item()
    target_input = 1 / eta
    changed_A = matrix.A.copy()
    changed_A[grid, pump] += current_input - target_input
    changed_supply = spsolve(changed_A, f, use_umfpack=False)
    changed_x = changed_supply.sum(axis=1)
    changed_residual = np.max(np.abs(changed_A @ changed_supply - f), axis=0)
    changed_denominator = float(abs(changed_A).sum(axis=1).max()) * np.max(
        np.abs(changed_supply), axis=0
    ) + np.max(np.abs(f), axis=0)
    changed_backward = np.divide(
        changed_residual,
        changed_denominator,
        out=np.zeros_like(changed_residual),
        where=changed_denominator != 0,
    )
    assert np.isfinite(changed_supply).all() and np.max(changed_backward) < 1e-12
    changed_score = float((matrix.C @ (matrix.B @ changed_supply))[m].sum())
    # Read-only sensitivity: changing water discharge and evaporation together
    # gives a net 0.00175 m3/kWh, preserving the original turbine withdrawal.
    water_delta = 0.00175 - matrix.direct[m, pump]
    original_score = float(scores[m].sum())
    sensitivity = {
        "scenario": scenario,
        "year": year,
        "current_pumping_kWh_per_kWh": current_input,
        "STEM_efficiency": eta,
        "target_pumping_kWh_per_kWh": target_input,
        "current_direct_water_m3_per_kWh": float(matrix.direct[m, pump]),
        "proposed_direct_water_m3_per_kWh": 0.00175,
        "original_water_m3": original_score,
        "water_only_correction_m3": original_score + water_delta * x[pump],
        "efficiency_only_correction_m3": changed_score,
        "both_corrections_m3": changed_score + water_delta * changed_x[pump],
        "changed_max_backward_error": float(np.max(changed_backward)),
        "status": "In-memory sensitivity only; original matrices, inputs and results unchanged",
    }
    (output / f"{prefix}_sensitivity.json").write_text(
        json.dumps(sensitivity, indent=2) + "\n"
    )
    result = {
        "scenario": scenario,
        "year": year,
        "package_sha256": digest(package),
        "supply_solver": "SuperLU, CSR, separate demands",
        "intensity_solver": "UMFPACK adjoint (diagnostic)",
        "adjoint_sum_relative_difference": float(
            abs(annual.water_score_adjoint.sum() - annual.water_score.sum())
            / abs(annual.water_score.sum())
        ),
        "max_backward_error": float(np.max(backward)),
        "checks": records,
    }
    (output / f"{prefix}_verification.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(scenario, year, "verified; leading freshwater activities:", flush=True)
    print(
        direct[
            [
                "name",
                "reference product",
                "location",
                "supply",
                "direct_score_per_unit",
                "score",
            ]
        ]
        .head(5)
        .to_string(index=False),
        flush=True,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("batch", type=Path)
    p.add_argument(
        "--scenarios", nargs="+", default=["SPS2_bas0", "SPS3_bas0", "SPS4_bas0"]
    )
    p.add_argument("--years", type=int, nargs="+", default=[2020, 2025, 2030, 2035])
    args = p.parse_args()
    output = args.batch / "anomaly_investigation"
    output.mkdir(exist_ok=True)
    screen(args.batch, output)
    for scenario in args.scenarios:
        for year in args.years:
            trace(args.batch, output, scenario, year)


if __name__ == "__main__":
    main()
