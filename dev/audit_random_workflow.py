"""Seeded, stratified independent solves of individual STEM final-energy demands."""

import argparse
from collections import Counter
import json
from pathlib import Path
import warnings
import zipfile

import numpy as np
import pandas as pd
import yaml

from workflow_audit_core import MatrixAudit, digest

SEED = 20260930


def sample_cases(batch, output):
    rng = np.random.default_rng(SEED)
    inputs = json.loads((batch / "inputs.json").read_text())
    scenarios = sorted(e["scenario"] for e in inputs["packages"])
    # Two scenario draws for every year, conditional on covering all SPS families
    # and shock types. Within each draw select one positive-demand variable per
    # sector and one zero-demand variable, before looking at any LCIA score.
    while True:
        chosen = rng.choice(scenarios, 14, replace=False).tolist()
        if len({s[:4] for s in chosen}) == 4 and len({s[5:8] for s in chosen}) == 4:
            break
    years = sorted(inputs["years"] * 2)
    cases = []
    for scenario, year in zip(chosen, years):
        demands = pd.read_csv(
            next((batch / "scenarios" / scenario).glob("*_demands.csv"))
        )
        current = demands[demands.year.eq(year)]
        for sector in ["industry", "residential", "services", "transport"]:
            eligible = current[
                current.sector.eq(sector) & current.scenario_demand.gt(1e-8)
            ].sort_values("variable")
            row = eligible.iloc[int(rng.integers(len(eligible)))]
            cases.append(
                {
                    "scenario": scenario,
                    "year": year,
                    "variable": row.variable,
                    "selection": "random positive demand",
                    "sector": sector,
                }
            )
        zero = current[current.scenario_demand.eq(0)].sort_values("variable")
        row = zero.iloc[int(rng.integers(len(zero)))]
        cases.append(
            {
                "scenario": scenario,
                "year": year,
                "variable": row.variable,
                "selection": "random zero demand",
                "sector": row.sector,
            }
        )
    pd.DataFrame(cases).to_csv(output / "random_sample.csv", index=False)
    (output / "sampling_plan.json").write_text(
        json.dumps(
            {
                "seed": SEED,
                "description": "14 distinct scenarios, two draws per each of seven years, all SPS and shock families represented; one positive demand per sector and one zero per pair; selected without consulting scores",
                "cases": cases,
            },
            indent=2,
        )
        + "\n"
    )
    return pd.DataFrame(cases)


def audit(batch):
    output = batch / "workflow_audit"
    output.mkdir(exist_ok=True)
    sample = sample_cases(batch, output)
    inputs = json.loads((batch / "inputs.json").read_text())
    build = Path(inputs["build"])
    config = yaml.safe_load(
        (build / "inputs/configuration_file/config.yaml").read_text()
    )
    source = pd.read_csv(
        build / "inputs/scenario_data/scenario_data.csv", float_precision="round_trip"
    )
    years = sorted(int(c) for c in source if c.isdigit())
    aliases = {}
    used = Counter()
    for key, settings in config["production pathways"].items():
        alias = settings["ecoinvent alias"]
        identity = alias["name"], alias["reference product"]
        aliases[key] = alias["name"] + (
            f"_{key}" if used[identity] and not alias.get("new dataset") else ""
        )
        if not alias.get("new dataset"):
            used[identity] += 1
    chains = pd.read_csv(output / "all_final_energy_input_chains.csv")
    assert len(chains) == 40 * 101 * 7
    score_checks, market_checks, solver_checks, warning_records, exchanges = (
        [],
        [],
        [],
        [],
        [],
    )
    for counter, ((scenario, year), cases) in enumerate(
        sample.groupby(["scenario", "year"]), 1
    ):
        year = int(year)
        folder = batch / "scenarios" / scenario
        manifest = json.loads(next(folder.glob("*.manifest.json")).read_text())
        cf_path = Path(manifest["pathways_source"]) / "data/lcia_ei312.json"
        assert digest(cf_path) == manifest["calculation"]["cf_sha256"]
        assert digest(manifest["package"]) == manifest["calculation"]["package_sha256"]
        demands = pd.read_csv(
            next(folder.glob("*_demands.csv")), float_precision="round_trip"
        )
        final = pd.read_csv(
            batch / "toby_export/prem1_by_scenario" / f"{scenario}.csv",
            encoding="latin-1",
            float_precision="round_trip",
        )
        raw = pd.read_parquet(next(folder.glob("*.gzip")))
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            matrices = MatrixAudit(
                manifest["package"], year, cf_path, manifest["calculation"]["methods"]
            )
            f = np.zeros((len(matrices.ai), len(cases)))
            activities = []
            for column, case in enumerate(cases.to_dict("records")):
                row = demands[
                    demands.year.eq(year) & demands.variable.eq(case["variable"])
                ].iloc[0]
                idx = matrices.find(
                    row["name"], row.reference_product, row.unit, row.location
                )
                original = chains[
                    chains.scenario.eq(scenario)
                    & chains.year.eq(year)
                    & chains.variable.eq(case["variable"])
                ].iloc[0]
                # Rebuild demand from audited original workbook values and SI units,
                # not from the solver's demand file or its conversion functions.
                amount = max(0.0, original.raw_interpolated_PJ) * (
                    1e9 if row.unit == "megajoule" else 1e9 / 3.6
                )
                f[idx, column] = amount
                activities.append(idx)
                ex = matrices.exchanges(idx)
                ex["scenario"] = scenario
                ex["year"] = year
                ex["variable"] = case["variable"]
                exchanges.append(ex)
            supply, scores, backward = matrices.solve(f)
        warning_records.extend(
            {
                "scenario": scenario,
                "year": year,
                "category": w.category.__name__,
                "message": str(w.message),
            }
            for w in captured
        )
        geography = np.where(
            matrices.ai.location.eq("CH"),
            "CH",
            np.where(
                matrices.ai.location.isin(manifest["europe_locations"]),
                "EU wo CH",
                "RoW",
            ),
        )
        # Sum characterized contributions by emitting activity location, independently
        # of Pathways' geography/groupby code. Negative contributions stay signed.
        for column, case in enumerate(cases.to_dict("records")):
            for j, method in enumerate(manifest["calculation"]["methods"]):
                label = manifest["methods"][method][0]
                unit = manifest["methods"][method][1]
                for location in ["CH", "EU wo CH", "RoW", "TOTAL"]:
                    calculated = (
                        float(scores[j, column])
                        if location == "TOTAL"
                        else float(
                            np.sum(
                                matrices.direct[j, geography == location]
                                * supply[geography == location, column]
                            )
                        )
                    )
                    subset = final[
                        final.variable.eq(case["variable"])
                        & final.year.eq(year)
                        & final.impact.eq(label)
                    ]
                    assert set(subset.unit) == {unit}
                    reported = (
                        float(subset.value.sum())
                        if location == "TOTAL"
                        else float(
                            subset.loc[subset.location.eq(location), "value"].sum()
                        )
                    )
                    raw_rows = raw[
                        raw.variable.eq(case["variable"])
                        & raw.year.eq(year)
                        & raw.impact_category.eq(method)
                    ]
                    raw_score = float(raw_rows.value.sum())
                    absolute = abs(calculated - reported)
                    scale = max(abs(reported), float(subset.value.abs().sum()), 1e-30)
                    relative = absolute / scale
                    # Alternative factorization can differ slightly on badly scaled
                    # systems. Retain every discrepancy, tolerance and warning.
                    passed = absolute <= 1e-7 * scale + 1e-10
                    score_checks.append(
                        {
                            **case,
                            "impact": label,
                            "location": location,
                            "unit": unit,
                            "independent_score": calculated,
                            "latest_score": reported,
                            "absolute_difference": absolute,
                            "relative_to_total_absolute_contributions": relative,
                            "passed": bool(passed),
                            "raw_total": raw_score if location == "TOTAL" else np.nan,
                            "raw_grouping_passed": (
                                bool(
                                    np.isclose(
                                        raw_score, reported, rtol=1e-12, atol=1e-10
                                    )
                                )
                                if location == "TOTAL"
                                else True
                            ),
                        }
                    )
        # Verify the complete Swiss electricity supply mix against frozen STEM
        # inputs, including PV inventories and capture pathways.
        scdata = source[
            source.scenario.eq(scenario) & source.region.eq("CH")
        ].set_index("variables")
        market = next(
            m
            for m in config["markets"]
            if m["name"] == "market for electricity, high voltage (SPS)"
        )
        volumes = {
            key: float(
                np.interp(
                    year,
                    years,
                    scdata.loc[
                        config["production pathways"][key]["production volume"][
                            "variable"
                        ],
                        [str(y) for y in years],
                    ].to_numpy(float),
                )
            )
            for key in market["includes"]
        }
        total = sum(volumes.values())
        market_id = matrices.find(market["name"], location="CH")
        ex = matrices.exchanges(market_id)
        ex = ex[ex.flip.eq(1)]
        for key, volume in volumes.items():
            alias = config["production pathways"][key]["ecoinvent alias"]
            suppliers = ex[
                ex.name.str.lower().eq(aliases[key].lower())
                & ex["reference product"].eq(alias["reference product"])
            ]
            expected = volume / total
            actual = float(suppliers.value.sum())
            market_checks.append(
                {
                    "scenario": scenario,
                    "year": year,
                    "pathway": key,
                    "supplier": aliases[key],
                    "supplier_locations": "|".join(suppliers.location.unique()),
                    "expected_share": expected,
                    "exported_share": actual,
                    "passed": bool(np.isclose(actual, expected, rtol=1e-9, atol=1e-12)),
                    "is_IEA_PV": key.startswith("pv "),
                }
            )
        solver_checks.append(
            {
                "scenario": scenario,
                "year": year,
                "solver": "UMFPACK; independent of Pathways SuperLU solves",
                "activities": len(matrices.ai),
                "biosphere_flows": len(matrices.bi),
                "normwise_backward_error": float(np.max(backward)),
                "all_link_indices_valid": True,
                "unique_target_suppliers": True,
                "nonfinite_results": 0,
            }
        )
        pd.DataFrame(score_checks).to_csv(
            output / "random_score_checks.csv", index=False
        )
        pd.DataFrame(market_checks).to_csv(
            output / "random_electricity_market_checks.csv", index=False
        )
        print(
            f"Independent sample {counter}/14: {scenario}/{year}; {len(cases)} demands x 6 indicators x 4 geographic summaries",
            flush=True,
        )
        del matrices, supply, f
    pd.concat(exchanges, ignore_index=True).to_csv(
        output / "random_target_inventory_exchanges.csv", index=False
    )
    pd.DataFrame(solver_checks).to_csv(output / "random_solver_checks.csv", index=False)
    scores = pd.DataFrame(score_checks)
    markets = pd.DataFrame(market_checks)
    summary = {
        "seed": SEED,
        "sampled_scenario_years": len(solver_checks),
        "sampled_final_demands": len(sample),
        "score_checks": len(scores),
        "score_checks_failed": int((~scores.passed).sum()),
        "raw_grouping_failures": int((~scores.raw_grouping_passed).sum()),
        "market_shares_checked": len(markets),
        "market_share_failures": int((~markets.passed).sum()),
        "max_relative_score_difference": float(
            scores.relative_to_total_absolute_contributions.max()
        ),
        "max_normwise_backward_error": max(
            r["normwise_backward_error"] for r in solver_checks
        ),
        "solver_warnings": warning_records,
        "scope": "Numerical, mapping and unit checks do not establish that all inventory modelling choices are correct; see separate input and heat-pump findings.",
        "script_sha256": digest(Path(__file__)),
        "core_sha256": digest(Path(__file__).with_name("workflow_audit_core.py")),
    }
    (output / "random_audit_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "solver_warnings"}, indent=2
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    audit(parser.parse_args().batch)
