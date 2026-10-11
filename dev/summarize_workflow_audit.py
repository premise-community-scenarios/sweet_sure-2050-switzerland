"""Compile the numerical and modelling audit, including STEM CHP energy balances."""

import argparse
import json
from pathlib import Path
import zipfile

import numpy as np
from openpyxl import load_workbook
import pandas as pd

from workflow_audit_core import digest


def pv_trace(batch, build, out):
    inventory = build / "software/premise/data/additional_inventories/lci-PV-2026.xlsx"
    workbook = load_workbook(inventory, read_only=True, data_only=True)
    names = {
        str(row[1]).strip().lower()
        for sheet in workbook
        for row in sheet.values
        if row[0] == "Activity"
    }
    workbook.close()
    checks = pd.read_csv(out / "random_electricity_market_checks.csv")
    records = []
    for (scenario, year), rows in checks[checks.is_IEA_PV].groupby(
        ["scenario", "year"]
    ):
        with zipfile.ZipFile(
            build / "packages" / f"remind-SSP2-PkBudg1000-stem-{scenario}.zip"
        ) as archive:
            prefix = f"inventories/remind/SSP2-PkBudg1000-{scenario}/{year}/"
            ai = pd.read_csv(archive.open(prefix + "A_matrix_index.csv"), sep=";")
            a = pd.read_csv(
                archive.open(prefix + "A_matrix.csv"),
                sep=";",
                float_precision="round_trip",
            )
            for row in rows.to_dict("records"):
                ids = ai[
                    ai.name.str.lower().eq(row["supplier"].lower())
                    & ai.location.eq("CH")
                ]["index"]
                assert len(ids) == 1
                ex = a[a["index of activity"].isin(ids) & a.flip.eq(1)].merge(
                    ai, left_on="index of product", right_on="index"
                )
                install = ex[
                    ex.name.str.contains("photovoltaic installation construction")
                ]
                assert (
                    len(install) > 0
                    and install.name.str.strip().str.lower().isin(names).all()
                )
                assert install["unit"].eq("unit").all() and install.value.gt(0).all()
                records.append(
                    {
                        "scenario": scenario,
                        "year": int(year),
                        "pathway": row["pathway"],
                        "pv_supplier": row["supplier"],
                        "installation_exchanges": len(install),
                        "IEA_2026_installation_names_verified": True,
                        "source_workbook_sha256": digest(inventory),
                    }
                )
    frame = pd.DataFrame(records)
    frame.to_csv(out / "pv_2026_inventory_trace.csv", index=False)
    return frame


def energy_balances(build, out):
    source = pd.read_csv(
        build / "inputs/scenario_data/scenario_data.csv", float_precision="round_trip"
    )
    years = [int(c) for c in source if c.isdigit()]
    records = []
    for scenario, group in source.groupby("scenario"):
        data = group.set_index("variables")
        for fuel in ["Wood", "Gas", "Oil"]:
            plant = f"CHP {fuel} (for District Heating)"
            keys = [
                f"Electricity generation|{fuel}|{plant}",
                f"Heat generation from CHPs and heat plants|District heating|CHP|{fuel}|{plant}",
                f"Fuel input for electricity generation|{fuel}|{plant}",
                f"Fuel input for heat generation from CHPs and heat plants|District heating|CHP|{fuel}|{plant}",
            ]
            assert set(data.loc[keys, "unit"]) == {"PJ/yr."}
            for year in years:
                electricity, heat, fuel_e, fuel_h = data.loc[keys, str(year)].to_numpy(
                    float
                )
                total_fuel = fuel_e + fuel_h
                combined = (
                    (electricity + heat) / total_fuel if total_fuel > 0 else np.nan
                )
                electric = electricity / total_fuel if total_fuel > 0 else np.nan
                target = data.loc[
                    f"Efficiency of power plants (electric)|{fuel}|{plant}", str(year)
                ]
                records.append(
                    {
                        "scenario": scenario,
                        "year": year,
                        "fuel": fuel,
                        "electricity_PJ": electricity,
                        "heat_PJ": heat,
                        "fuel_allocated_to_electricity_PJ": fuel_e,
                        "fuel_allocated_to_heat_PJ": fuel_h,
                        "total_fuel_PJ": total_fuel,
                        "combined_output_over_input": combined,
                        "derived_electric_efficiency": electric,
                        "reported_electric_efficiency": target,
                        "output_exceeds_input_10pct": bool(combined > 1.1),
                        "combined_efficiency_below_1pct": bool(0 < combined < 0.01),
                    }
                )
    frame = pd.DataFrame(records)
    frame.to_csv(out / "STEM_CHP_energy_balances.csv", index=False)
    return frame


def summarize(batch):
    out = batch / "workflow_audit"
    build = Path(json.loads((batch / "inputs.json").read_text())["build"])
    balances = energy_balances(build, out)
    pv = pv_trace(batch, build, out)
    inputs = json.loads((out / "input_chain_validation.json").read_text())
    random = json.loads((out / "random_audit_validation.json").read_text())
    recheck = json.loads((out / "random_solver_recheck_validation.json").read_text())
    failures = pd.read_csv(out / "stem_format_discrepancies.csv")
    failures["omitted_electricity_PJ"] = failures.expected - failures.actual
    omitted = (
        failures.groupby(["scenario", "year"])
        .omitted_electricity_PJ.sum()
        .reset_index()
    )
    omitted["omitted_electricity_TWh"] = omitted.omitted_electricity_PJ / 3.6
    omitted.to_csv(out / "omitted_waste_electricity.csv", index=False)
    stage_results = pd.DataFrame(
        [
            {
                "stage": "Raw STEM archive to formatted scenario inputs",
                "status": "FAIL",
                "evidence": f"{inputs['raw_numeric_cells']} numeric cells checked; {inputs['format_discrepancies']} waste-electricity aggregation discrepancies",
            },
            {
                "stage": "Final-energy source values, mappings and units",
                "status": "PASS",
                "evidence": f"{inputs['final_energy_chains_checked']} source-to-package-to-calculation demand chains; 40 scenarios x 101 variables x 7 years",
            },
            {
                "stage": "Sampled independent solves and geographic grouping",
                "status": "PASS after numerical recheck",
                "evidence": f"70 demands, 420 indicator totals, 1260 geographic contributions; max alternative-solver difference {random['max_relative_score_difference']:.3g}; {recheck['unresolved']} unresolved strict flags",
            },
            {
                "stage": "Sampled electricity market shares",
                "status": "PASS against frozen inputs",
                "evidence": "742 shares agree with frozen inputs; this does not repair the omitted raw waste-electricity input",
            },
            {
                "stage": "PV inventory generation",
                "status": "PASS",
                "evidence": f"{len(pv)} PV providers; {pv.installation_exchanges.sum()} installation exchanges match IEA 2026 source workbook",
            },
            {
                "stage": "Swiss heat-pump electricity geography",
                "status": "FAIL modelling intent",
                "evidence": "Swiss wrapper -> CH heat proxy -> NEU heat process -> NEU electricity, bypassing direct Swiss SPS electricity",
            },
            {
                "stage": "STEM CHP modifications in background electricity",
                "status": "REQUIRES CORRECTION",
                "evidence": "STEM-scaled CH wood CHP is reused at fixed IAM background market share; it dominates the heat-pump land score",
            },
            {
                "stage": "Original STEM CHP energy balances",
                "status": "SOURCE CLARIFICATION REQUIRED",
                "evidence": f"{balances.output_exceeds_input_10pct.sum()} of {len(balances)} gas/oil/wood CHP year-cases have output above 110% of reported fuel input",
            },
        ]
    )
    stage_results.to_csv(out / "audit_summary.csv", index=False)
    notes = [
        "Current batch is not recommended for publication. Numerical agreement is distinct from model/input correctness.",
        "No source inventory, scenario input, package or published result has been modified by this audit.",
        "Seed 20260930; sample selected by demand before observing scores. Two scenario draws per year; all four SPS and shock families; each draw has one positive demand per sector and one zero.",
        "Demand checks cover all 40 scenarios and all seven years; random matrix checks cover 14 distinct scenario-year pairs.",
        "Solver warnings are retained in random_audit_validation.json. Ill-conditioned matrices caused sub-ppm UMFPACK/SuperLU differences. Independent direct CSR SuperLU rechecks resolve all 13 strict flags.",
        "Geographic contributions are assigned by emitting activity location. EU wo CH means REMIND EUR+NEU excluding Switzerland.",
        "All diagnostic sensitivities are in-memory counterfactuals, not approved replacement results.",
        "The waste-restoration sensitivity changes market shares only and retains current CHP efficiency assumptions. A production fix must also make the representation of electricity-only waste plants and their fuel/efficiency basis consistent.",
        "The source CHP energy-balance screen sums the rows explicitly labelled fuel input for electricity and fuel input for heat. Both inputs and both outputs use PJ/yr. The source author must clarify anomalous rows; efficiencies were not clipped.",
        "The scan is broad but is not an exhaustive validation of every source inventory exchange, technology proxy or uncertainty assumption.",
    ]
    tables = {
        "Audit summary": stage_results,
        "Random sample": pd.read_csv(out / "random_sample.csv"),
        "Source workbook checks": pd.read_csv(out / "stem_workbook_checks.csv"),
        "Waste discrepancies": failures,
        "Waste electricity omitted": omitted,
        "All demand chains": pd.read_csv(out / "all_final_energy_input_chains.csv"),
        "Score checks": pd.read_csv(out / "random_score_checks.csv"),
        "Solver rechecks": pd.read_csv(out / "random_solver_flag_rechecks.csv"),
        "Market shares": pd.read_csv(out / "random_electricity_market_checks.csv"),
        "PV IEA 2026 trace": pv,
        "Heat pump root cause": pd.read_csv(out / "heat_pump_root_cause.csv"),
        "Diagnostic sensitivities": pd.read_csv(
            out / "heat_pump_diagnostic_sensitivities.csv"
        ),
        "STEM CHP balances": balances,
    }
    with pd.ExcelWriter(out / "workflow_audit.xlsx", engine="xlsxwriter") as writer:
        pd.DataFrame({"Read me": notes}).to_excel(
            writer, sheet_name="Read me", index=False
        )
        writer.sheets["Read me"].set_column(0, 0, 125)
        for name, table in tables.items():
            table.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.freeze_panes(1, 2)
            sheet.autofilter(0, 0, len(table), len(table.columns) - 1)
            sheet.set_column(0, len(table.columns) - 1, 23)
        writer.sheets["Audit summary"].set_column(0, 0, 55)
        writer.sheets["Audit summary"].set_column(1, 1, 32)
        writer.sheets["Audit summary"].set_column(2, 2, 100)
    summary = {
        "publication_ready": False,
        "reason": "Waste-electricity omission, Swiss heat-pump geography and STEM-scaled background CHP coupling require correction; original STEM CHP energy balances require clarification.",
        "original_results_modified": False,
        "stage_results": stage_results.to_dict("records"),
        "solver_flags_unresolved": recheck["unresolved"],
        "source_energy_balance_flags": int(balances.output_exceeds_input_10pct.sum()),
        "source_workbooks": 40,
        "full_final_energy_chains": inputs["final_energy_chains_checked"],
        "sampled_demands": 70,
        "sampled_indicator_totals": 420,
        "sampled_geographic_contributions": 1260,
        "code_sha256": {
            p.name: digest(p) for p in Path(__file__).parent.glob("*workflow*.py")
        },
        "input_audit_script_sha256": digest(
            Path(__file__).with_name("audit_stem_input_chain.py")
        ),
        "heat_pump_script_sha256": digest(
            Path(__file__).with_name("diagnose_heat_pump_land.py")
        ),
        "execution_software_sha256": {
            str(p.relative_to(out)): digest(p) for p in (out / "software").glob("*.py")
        },
        "files_sha256": {
            p.name: digest(p)
            for p in out.iterdir()
            if p.is_file() and p.name != "AUDIT_STATUS.json"
        },
        "notes": notes,
    }
    (out / "AUDIT_STATUS.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(stage_results.to_string(index=False))
    print(
        balances.groupby("fuel")[
            ["output_exceeds_input_10pct", "combined_efficiency_below_1pct"]
        ]
        .sum()
        .to_string()
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("batch", type=Path)
    summarize(p.parse_args().batch)
