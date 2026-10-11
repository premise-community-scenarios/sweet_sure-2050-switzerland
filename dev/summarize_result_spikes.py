"""Reconcile anomaly drivers with saved STEM exports and summarize diagnostics."""

import argparse
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from openpyxl import load_workbook
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yaml

from stem_source_cache import external_caches, raw_series
from workflow_audit_core import digest

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "dev/STEM raw data/new data export/OneDrive_2_28-09-2026.zip"
WATER = "net use of fresh water"
PUMP = "electricity production, hydro, pumped storage_hydro (pumped storage)"


def workbook_name(scenario):
    family, variant = scenario.split("_")
    suffix = (
        ""
        if variant == "bas0"
        else f"_{variant[:3].upper()}_{['Low', 'Mid', 'High'][int(variant[-1])-1]}"
    )
    return f"STEM_to_Premise_{family}{suffix}_2035.xlsx"


def source_audit(batch, output):
    manifest = json.loads(
        next((batch / "scenarios/SPS2_bas0").glob("*.manifest.json")).read_text()
    )
    build = Path(manifest["package"]).parent.parent
    source = pd.read_csv(
        build / "inputs/scenario_data/scenario_data.csv", float_precision="round_trip"
    )
    config = yaml.safe_load(
        (build / "inputs/configuration_file/config.yaml").read_text()
    )
    checks, cache_checks = [], []
    volumes = {
        "output_PJ": ("Electricity generation|Hydro|Pump storage", "VAR_fout", "ELC"),
        "input_PJ": (
            "Fuel input for electricity generation|Hydro|Pump storage",
            "VAR_fin",
            "ELC",
        ),
        "capacity_GW": (
            "Electricity generation capacity|Hydro|Pump storage",
            "VAR_cap",
            "-",
        ),
    }
    with ZipFile(ARCHIVE) as archive:
        # Four baselines: verify the labelled upstream raw model rows too.
        for family in range(1, 5):
            scenario = f"SPS{family}_bas0"
            name = workbook_name(scenario)
            payload = archive.read(name)
            cache = external_caches(payload)[1]
            for field, (variable, kind, commodity) in volumes.items():
                components = [
                    raw_series(cache, tech, kind, commodity)
                    for tech in ["EHYD-PUM00", "EHYD-PUM15"]
                ]
                row = source.loc[
                    source.scenario.eq(scenario) & source.variables.eq(variable)
                ].iloc[0]
                for year in components[0]:
                    value = sum(component[year][0] for component in components)
                    expected = float(row[str(year)])
                    np.testing.assert_allclose(value, expected, rtol=1e-10, atol=1e-9)
                    cache_checks.append(
                        {
                            "scenario": scenario,
                            "field": field,
                            "year": year,
                            "raw_value": value,
                            "csv_value": expected,
                            "source_cells": ";".join(
                                component[year][1] for component in components
                            ),
                            "workbook": name,
                            "source_target": cache["target"],
                        }
                    )
        # Every scenario: ensure pump output/input and selected demand anomalies
        # survive export -> frozen CSV unchanged, without refreshing Excel links.
        extra = [
            "FE_residential_space_heating_wood",
            "FE_residential_water_heating_wood",
            "FE_heavy_duty_truck_diesel",
            "FE_cars_electricity",
            "FE_industry_process_solar",
        ]
        variables = [x[0] for x in volumes.values()] + [
            config["production pathways"][v]["production volume"]["variable"]
            for v in extra
        ]
        for scenario in sorted(source.scenario.unique()):
            name = workbook_name(scenario)
            wb = load_workbook(
                BytesIO(archive.read(name)),
                read_only=True,
                data_only=True,
                keep_links=False,
            )
            sheet = wb[name.removeprefix("STEM_to_Premise_").removesuffix("_2035.xlsx")]
            rows = list(sheet.values)
            header = rows[0]
            years = {
                int(value): i
                for i, value in enumerate(header)
                if isinstance(value, (int, float)) and 2020 <= value <= 2050
            }
            matches = {
                r[3]: (i + 1, r)
                for i, r in enumerate(rows)
                if len(r) > 3 and r[3] in variables
            }
            for variable in variables:
                row_number, exported = matches[variable]
                row = source.loc[
                    source.scenario.eq(scenario) & source.variables.eq(variable)
                ].iloc[0]
                for year, col in years.items():
                    value = float(exported[col])
                    expected = float(row[str(year)])
                    np.testing.assert_allclose(value, expected, rtol=1e-10, atol=1e-9)
                    checks.append(
                        {
                            "scenario": scenario,
                            "variable": variable,
                            "year": year,
                            "export_value": value,
                            "csv_value": expected,
                            "row": row_number,
                            "workbook": name,
                        }
                    )
            wb.close()
    pd.DataFrame(cache_checks).to_csv(
        output / "labelled_STEM_hydro_reconciliation.csv", index=False
    )
    pd.DataFrame(checks).to_csv(
        output / "export_to_csv_reconciliation.csv", index=False
    )
    market = next(
        m
        for m in config["markets"]
        if m["name"] == "market for electricity, high voltage (SPS)"
    )
    market_variables = [
        config["production pathways"][v]["production volume"]["variable"]
        for v in market["includes"]
    ]
    shares = []
    for file in output.glob("*_high_voltage_suppliers.csv"):
        scenario, year = file.name.split("_high_voltage")[0].rsplit("_", 1)
        rows = source.loc[source.scenario.eq(scenario)].set_index("variables")
        total = rows.loc[market_variables, year].sum()
        pump = rows.loc[volumes["output_PJ"][0], year]
        suppliers = pd.read_csv(file, float_precision="round_trip")
        actual = suppliers.loc[suppliers.name.eq(PUMP), "value"].item()
        np.testing.assert_allclose(actual, pump / total, rtol=1e-10, atol=1e-12)
        shares.append(
            {
                "scenario": scenario,
                "year": int(year),
                "generation_and_imports_PJ": total,
                "pump_output_PJ": pump,
                "expected_share": pump / total,
                "matrix_share": actual,
            }
        )
    pd.DataFrame(shares).to_csv(
        output / "pump_market_share_reconciliation.csv", index=False
    )
    (output / "source_verification.json").write_text(
        json.dumps(
            {
                "archive_sha256": digest(ARCHIVE),
                "source_csv_sha256": digest(
                    build / "inputs/scenario_data/scenario_data.csv"
                ),
                "labelled_raw_checks": len(cache_checks),
                "export_checks": len(checks),
                "market_share_checks": len(shares),
                "status": "passed",
            },
            indent=2,
        )
        + "\n"
    )
    return source


def other_anomalies(batch, output):
    data = pd.read_parquet(batch / "df_final.parquet")
    totals = pd.read_csv(batch / "indicator_totals.csv")
    series = (
        data.groupby(["scenario", "impact_category", "variable", "year"])
        .value.sum()
        .unstack(fill_value=0)
    )
    peaks = totals.groupby(["scenario", "impact_category"]).value.max()
    records = []
    for key, values in series.iterrows():
        v, years = values.values, values.index
        for i in range(1, len(v) - 1):
            weight = (years[i] - years[i - 1]) / (years[i + 1] - years[i - 1])
            deviation = v[i] - ((1 - weight) * v[i - 1] + weight * v[i + 1])
            fraction = abs(deviation) / abs(peaks.loc[key[:2]])
            if fraction > 0.02 and (v[i] - v[i - 1]) * (v[i + 1] - v[i]) < 0:
                records.append(
                    (*key, int(years[i]), v[i - 1], v[i], v[i + 1], fraction)
                )
    pd.DataFrame(
        records,
        columns=[
            "scenario",
            "indicator",
            "variable",
            "year",
            "previous",
            "value",
            "next",
            "fraction_of_scenario_peak",
        ],
    ).to_csv(output / "large_variable_reversals.csv", index=False)
    cases = [
        (
            "SPS2_bas0",
            "land occupation",
            "FE_residential_space_heating_wood",
            [2020, 2025, 2030],
        ),
        (
            "SPS2_bas0",
            "land occupation",
            "FE_residential_water_heating_wood",
            [2020, 2025, 2030],
        ),
        ("SPS2_bas0", WATER, "FE_industry_process_solar", [2025, 2030, 2035]),
        (
            "SPS3_soc3",
            "climate change",
            "FE_heavy_duty_truck_diesel",
            [2030, 2035, 2040],
        ),
        ("SPS4_soc3", "minerals depletion", "FE_cars_electricity", [2030, 2035, 2040]),
        ("SPS2_soc3", "minerals depletion", "FE_cars_electricity", [2035, 2040, 2045]),
    ]
    records = []
    for scenario, indicator, variable, years in cases:
        demands = pd.read_csv(
            next((batch / "scenarios" / scenario).glob("*_demands.csv"))
        )
        selected = demands.loc[
            demands.variable.eq("EXT - 0 - " + variable) & demands.year.isin(years)
        ].copy()
        impacts = (
            data.loc[
                data.scenario.str.endswith(" - " + scenario)
                & data.impact_category.eq(indicator)
                & data.variable.eq("EXT - 0 - " + variable)
            ]
            .groupby("year")
            .value.sum()
        )
        selected["score"] = selected.year.map(impacts)
        selected["score_per_PJ"] = selected.score / selected.scenario_demand
        records.append(selected.assign(scenario=scenario, indicator=indicator))
    pd.concat(records).to_csv(
        output / "other_anomalies_demand_intensity.csv", index=False
    )
    for scenario in [f"SPS{i}_bas0" for i in range(1, 5)]:
        selected = data.loc[
            data.scenario.str.endswith(" - " + scenario)
            & data.impact_category.eq("land occupation")
        ]
        land = selected.groupby(["variable", "year"]).value.sum().unstack(fill_value=0)
        land["change_2020_2025"] = land[2025] - land[2020]
        land.sort_values("change_2020_2025", ascending=False).to_csv(
            output / f"{scenario}_land_drivers.csv"
        )


def figures(batch, output, source):
    totals = pd.read_csv(batch / "indicator_totals.csv")
    sensitivity = pd.DataFrame(
        json.loads(p.read_text()) for p in sorted(output.glob("*_sensitivity.json"))
    )
    sensitivity.to_csv(output / "freshwater_sensitivity_summary.csv", index=False)
    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=[
            "SPS2 baseline · freshwater",
            "SPS4 baseline · freshwater",
            "SPS3 baseline · freshwater",
            "STEM pumped-storage generation",
        ],
    )
    panels = [("SPS2_bas0", 1, 1), ("SPS4_bas0", 1, 2), ("SPS3_bas0", 2, 1)]
    for scenario, row, col in panels:
        saved = totals.loc[
            totals.scenario.str.endswith(" - " + scenario)
            & totals.impact_category.eq(WATER)
        ].sort_values("year")
        fig.add_trace(
            go.Scatter(
                x=saved.year,
                y=saved.value / 1e6,
                name="Current results",
                legendgroup="current",
                showlegend=row == col == 1,
                mode="lines+markers",
                line=dict(color="#ba4a37"),
            ),
            row=row,
            col=col,
        )
        test = sensitivity.loc[sensitivity.scenario.eq(scenario)].sort_values("year")
        for field, label, color, dash in [
            (
                "water_only_correction_m3",
                "Water correction only (diagnostic)",
                "#718190",
                "dot",
            ),
            (
                "both_corrections_m3",
                "Water + STEM efficiency (diagnostic)",
                "#14796d",
                "dash",
            ),
        ]:
            fig.add_trace(
                go.Scatter(
                    x=test.year,
                    y=test[field] / 1e6,
                    name=label,
                    legendgroup=field,
                    showlegend=row == col == 1,
                    mode="lines+markers",
                    line=dict(color=color, dash=dash),
                ),
                row=row,
                col=col,
            )
        fig.update_yaxes(title_text="Million m³/year", row=row, col=col)
    for family, color in [
        (1, "#64748b"),
        (2, "#ba4a37"),
        (3, "#7c55aa"),
        (4, "#14796d"),
    ]:
        record = source.loc[
            source.scenario.eq(f"SPS{family}_bas0")
            & source.variables.eq("Electricity generation|Hydro|Pump storage")
        ].iloc[0]
        years = [2020, 2022, 2025, 2030, 2035, 2040, 2050]
        fig.add_trace(
            go.Scatter(
                x=years,
                y=[record[str(y)] / 3.6 for y in years],
                name=f"SPS{family} pumped storage",
                mode="lines+markers",
                line=dict(color=color),
            ),
            row=2,
            col=2,
        )
    fig.update_yaxes(title_text="TWh/year", row=2, col=2)
    fig.update_xaxes(title_text="Year", dtick=5)
    fig.update_layout(
        title="Freshwater anomaly investigation · all sectors and contribution locations",
        template="plotly_white",
        height=850,
        legend=dict(orientation="h", y=-0.12),
        margin=dict(b=150),
    )
    fig.write_html(output / "freshwater_diagnosis.html", include_plotlyjs=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()
    output = args.batch / "anomaly_investigation"
    source = source_audit(args.batch, output)
    other_anomalies(args.batch, output)
    figures(args.batch, output, source)
    print((output / "source_verification.json").read_text())


if __name__ == "__main__":
    main()
