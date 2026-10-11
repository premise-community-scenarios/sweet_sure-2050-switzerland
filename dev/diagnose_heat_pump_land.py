"""Trace heat-pump land occupation and quantify isolated in-memory sensitivities.

No source package or published result is modified. Counterfactuals diagnose
mechanisms; they are not replacement results or approvals of modelling choices.
"""

import argparse
import json
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
from scipy.sparse import bmat, csr_matrix
from scikits.umfpack import splu
import yaml

from workflow_audit_core import MatrixAudit, digest

WOOD = "heat and power co-generation, wood chips, 6667 kW, state-of-the-art 2014"
HEAT = "heat production, air-water heat pump 10kW"


def restore_background_wood(m, factor):
    """Keep STEM CHP in the Swiss SPS mix; undo its scaling in background uses."""
    wood = m.find(WOOD, "electricity, high voltage", location="CH")
    market = m.find("market for electricity, high voltage (SPS)", location="CH")
    # A cloned column retains all scenario-specific scaled inputs and emissions.
    # The original supplier used by background markets is restored to its logged
    # pre-adjustment quantities. Input-market linking is otherwise unchanged.
    clone = m.A[:, wood].tolil()
    clone[wood, 0] = 0
    A = bmat(
        [[m.A, clone], [csr_matrix((1, len(m.ai))), csr_matrix([[m.A[wood, wood]]])]],
        format="lil",
    )
    B = bmat([[m.B, m.B[:, wood]]], format="lil")
    for row in m.A[:, wood].nonzero()[0]:
        if row != wood:
            A[row, wood] = m.A[row, wood] / factor
    B[:, wood] = m.B[:, wood] / factor
    A[len(m.ai), market] = A[wood, market]
    A[wood, market] = 0
    return A.tocsr(), B.tocsr()


def local_heat_pump(m, A, B):
    """Build the Swiss heat-pump column from its NEU proxy, with Swiss electricity."""
    ch = m.find(HEAT, location="CH")
    neu = m.find(HEAT, location="NEU")
    grid = m.find("market for electricity, low voltage (SPS)", location="CH")
    A = A.tolil(copy=True)
    B = B.tolil(copy=True)
    A[:, ch] = 0
    A[ch, ch] = 1
    for row in m.exchanges(neu).to_dict("records"):
        if row["flip"] != 1:
            continue
        supplier = (
            grid
            if row["reference product"] == "electricity, low voltage"
            else row["index of product"]
        )
        A[supplier, ch] -= row["value"]
    B[:, ch] = 0
    B[: len(m.bi), ch] = m.B[:, neu]
    return A.tocsr(), B.tocsr()


def corrected_waste_mix(m, A, config, scenario_data, discrepancies, year):
    from collections import Counter

    market = next(
        market
        for market in config["markets"]
        if market["name"] == "market for electricity, high voltage (SPS)"
    )
    aliases = {}
    used = Counter()
    for key, settings in config["production pathways"].items():
        al = settings["ecoinvent alias"]
        pair = al["name"], al["reference product"]
        aliases[key] = al["name"] + (
            f"_{key}" if used[pair] and not al.get("new dataset") else ""
        )
        if not al.get("new dataset"):
            used[pair] += 1
    rowdata = scenario_data.set_index("variables")
    amounts = {
        key: float(
            rowdata.loc[
                config["production pathways"][key]["production volume"]["variable"],
                str(year),
            ]
        )
        for key in market["includes"]
    }
    for key in amounts:
        name = config["production pathways"][key]["production volume"]["variable"]
        correction = discrepancies[
            discrepancies.variable.eq(name) & discrepancies.year.eq(year)
        ]
        if len(correction):
            amounts[key] = float(correction.iloc[0].expected)
    market_idx = m.find(market["name"], location="CH")
    ex = m.exchanges(market_idx)
    A = A.tolil(copy=True)
    for key, amount in amounts.items():
        alias = config["production pathways"][key]["ecoinvent alias"]
        providers = ex[
            ex.name.str.lower().eq(aliases[key].lower())
            & ex["reference product"].eq(alias["reference product"])
            & ex.flip.eq(1)
        ]
        if len(providers) != 1:
            assert abs(amount) < 1e-10
            continue
        idx = int(providers.iloc[0]["index of product"])
        # In the combined sensitivity the dedicated clone occupies the last row.
        if A.shape[0] > len(m.ai) and idx == m.find(
            WOOD, "electricity, high voltage", location="CH"
        ):
            idx = len(m.ai)
        A[idx, market_idx] = -amount / sum(amounts.values())
    return A.tocsr()


def diagnose(batch):
    output = batch / "workflow_audit"
    output.mkdir(exist_ok=True)
    build = Path(json.loads((batch / "inputs.json").read_text())["build"])
    config = yaml.safe_load(
        (build / "inputs/configuration_file/config.yaml").read_text()
    )
    source = pd.read_csv(
        build / "inputs/scenario_data/scenario_data.csv", float_precision="round_trip"
    )
    discrepancies = pd.read_csv(output / "stem_format_discrepancies.csv")
    observations, trial_records = [], []
    scenarios = ["SPS3_bas0", "SPS3_soc1", "SPS3_soc2", "SPS3_soc3"]
    for scenario in scenarios:
        for year in [2030, 2040, 2050]:
            manifest = json.loads(
                next(
                    (batch / "scenarios" / scenario).glob("*.manifest.json")
                ).read_text()
            )
            with warnings.catch_warnings(record=True) as warning_log:
                warnings.simplefilter("always")
                m = MatrixAudit(
                    manifest["package"],
                    year,
                    Path(manifest["pathways_source"]) / "data/lcia_ei312.json",
                    manifest["calculation"]["methods"],
                )
                demands = pd.read_csv(
                    next((batch / "scenarios" / scenario).glob("*_demands.csv")),
                    float_precision="round_trip",
                )
                demands = demands[demands.year.eq(year)]
                hp = demands[
                    demands.variable.eq(
                        "EXT - 0 - FE_residential_space_heating_heat_pump"
                    )
                ].iloc[0]
                hp_idx = m.find(hp["name"], hp.reference_product, hp.unit, hp.location)
                f = np.zeros((len(m.ai), 2))
                f[hp_idx, 0] = hp.scenario_demand * 1e9
                for r in demands.to_dict("records"):
                    idx = m.find(
                        r["name"], r["reference_product"], r["unit"], r["location"]
                    )
                    f[idx, 1] += r["scenario_demand"] * r["conversion_factor"]
                supply, scores, err = m.solve(f)
                efficiency = json.loads(
                    next(
                        (build / "scenarios" / scenario).glob("*.efficiencies.json")
                    ).read_text()
                )
                eff = next(
                    r
                    for r in efficiency
                    if r["year"] == year
                    and r["activity"] == WOOD
                    and r["product"] == "electricity, high voltage"
                    and r["location"] == "CH"
                )
                contributions = m.contributions(supply[:, 0], 2)
                forestry = contributions[contributions.name.str.contains("forestry")]
                wood = m.find(WOOD, "electricity, high voltage", location="CH")
                heat_neu = m.find(HEAT, location="NEU")
                neu_grid = m.find(
                    "market group for electricity, high voltage", location="NEU"
                )
                hp_trace = m.supplier_contributions(heat_neu, 2)
                neu_trace = m.supplier_contributions(neu_grid, 2)
                wood_flow = m.exchanges(wood)
                biomass = float(
                    wood_flow.loc[
                        wood_flow.name.eq(
                            "market for lignocellulosic biomass, used as fuel"
                        ),
                        "value",
                    ].sum()
                )
                observations.append(
                    {
                        "scenario": scenario,
                        "year": year,
                        "heat_pump_electricity_PJ": float(hp.scenario_demand),
                        "heat_pump_land_m2_year": float(scores[2, 0]),
                        "heat_pump_land_m2_year_per_MJ": float(
                            scores[2, 0] / (hp.scenario_demand * 1e9)
                        ),
                        "forestry_share_percent": float(
                            forestry.score.sum() / scores[2, 0] * 100
                        ),
                        "stem_wood_chp_efficiency_fraction": eff[
                            "stem_target_efficiency"
                        ],
                        "stem_wood_chp_efficiency_percent": eff[
                            "stem_target_efficiency"
                        ]
                        * 100,
                        "wood_chp_scaling_factor": eff["scaling_factor"],
                        "wood_chp_biomass_kg_per_kWh": biomass,
                        "wood_chp_land_m2_year_per_kWh": float(
                            m.upstream_intensities()[2, wood]
                        ),
                        "NEU_high_voltage_wood_chp_share": float(
                            neu_trace.loc[
                                neu_trace["index of product"].eq(wood), "value"
                            ].sum()
                        ),
                        "NEU_high_voltage_land_m2_year_per_kWh": float(
                            m.upstream_intensities()[2, neu_grid]
                        ),
                        "backward_error": float(np.max(err)),
                    }
                )
                if scenario in ["SPS3_bas0", "SPS3_soc2"] and year == 2040:
                    contributions.head(100).to_csv(
                        output / f"{scenario}_{year}_heat_pump_land_contributions.csv",
                        index=False,
                    )
                    hp_trace.to_csv(
                        output
                        / f"{scenario}_{year}_heat_pump_NEU_input_contributions.csv",
                        index=False,
                    )
                    neu_trace.to_csv(
                        output
                        / f"{scenario}_{year}_NEU_electricity_land_contributions.csv",
                        index=False,
                    )
                    wood_flow.to_csv(
                        output / f"{scenario}_{year}_wood_CHP_exchanges.csv",
                        index=False,
                    )
                if scenario == "SPS3_soc2" and year == 2040:
                    variants = {"original": (m.A, m.B)}
                    variants["Swiss electricity for Swiss heat pump"] = local_heat_pump(
                        m, m.A, m.B
                    )
                    variants["Isolate STEM-scaled wood CHP from background markets"] = (
                        restore_background_wood(m, eff["scaling_factor"])
                    )
                    variants["Both inventory linkage changes"] = local_heat_pump(
                        m,
                        *variants[
                            "Isolate STEM-scaled wood CHP from background markets"
                        ],
                    )
                    av, bv = variants["Both inventory linkage changes"]
                    variants["Both linkage changes plus restored waste electricity"] = (
                        corrected_waste_mix(
                            m,
                            av,
                            config,
                            source[source.scenario.eq(scenario)],
                            discrepancies[discrepancies.scenario.eq(scenario)],
                            year,
                        ),
                        bv,
                    )
                    for label, (A, B) in variants.items():
                        demand = np.zeros((A.shape[0], 2))
                        demand[: len(m.ai), :] = f
                        x = splu(A).solve(demand)
                        trial = m.C @ (B @ x)
                        residual = np.max(np.abs(A @ x - demand), axis=0) / (
                            float(abs(A).sum(axis=1).max()) * np.max(np.abs(x), axis=0)
                            + np.max(np.abs(demand), axis=0)
                        )
                        assert np.max(residual) < 1e-12
                        for j, method in enumerate(manifest["calculation"]["methods"]):
                            trial_records.append(
                                {
                                    "scenario": scenario,
                                    "year": year,
                                    "diagnostic_variant": label,
                                    "impact": manifest["methods"][method][0],
                                    "unit": manifest["methods"][method][1],
                                    "residential_heat_pump_score": float(trial[j, 0]),
                                    "whole_scenario_score": float(trial[j, 1]),
                                    "backward_error": float(np.max(residual)),
                                }
                            )
            print(
                f"Heat-pump trace: {scenario}/{year}; land={scores[2,0]:.6g}; wood CHP efficiency={eff['stem_target_efficiency']:.6g}",
                flush=True,
            )
            pd.DataFrame(observations).to_csv(
                output / "heat_pump_root_cause.csv", index=False
            )
            if trial_records:
                pd.DataFrame(trial_records).to_csv(
                    output / "heat_pump_diagnostic_sensitivities.csv", index=False
                )
            del m
    (output / "heat_pump_diagnostic_manifest.json").write_text(
        json.dumps(
            {
                "scenario_years": len(observations),
                "counterfactuals": "In-memory diagnostics only. Original packages and final results are unchanged. Isolating STEM CHP scaling restores pre-adjustment amounts for background uses while retaining the approved STEM target in a dedicated supplier for the Swiss SPS market. Corrected heat-pump geography copies the NEU process into CH with Swiss SPS electricity. These scenarios require a proper rebuild and review before publication.",
                "script_sha256": digest(Path(__file__)),
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("batch", type=Path)
    diagnose(p.parse_args().batch)
