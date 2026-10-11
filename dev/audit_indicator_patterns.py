"""Screen completed indicators and visualize land attribution, without changing results.

Run after trace_result_patterns.py for inventory-level evidence. Curvature and
intensity thresholds are screening heuristics, not statistical significance tests.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yaml

from investigate_result_spikes import screen


def audit(batch):
    output = batch / "pattern_audit"
    output.mkdir(exist_ok=True)
    data = pd.read_parquet(batch / "df_final.parquet")
    assert np.isfinite(data.value).all()
    totals = pd.read_csv(batch / "indicator_totals.csv")
    actual = data.groupby(["scenario", "year", "impact_category"]).value.sum()
    expected = totals.set_index(["scenario", "year", "impact_category"]).value
    np.testing.assert_allclose(actual.sort_index(), expected.sort_index(), rtol=1e-10)
    screen(batch, output)
    demands = []
    for folder in sorted((batch / "scenarios").iterdir()):
        if folder.is_dir():
            frame = pd.read_csv(next(folder.glob("*_demands.csv")))
            frame["scenario"] = folder.name
            demands.append(frame)
    demands = pd.concat(demands, ignore_index=True)
    data["scenario"] = data.scenario.str.split(" - ").str[-1]
    keys = ["scenario", "year", "impact_category", "variable"]
    annual = (
        data.groupby(keys, as_index=False)
        .value.sum()
        .merge(
            demands[
                [
                    "scenario",
                    "year",
                    "variable",
                    "scenario_demand",
                    "conversion_factor",
                    "name",
                    "reference_product",
                    "unit",
                ]
            ],
            on=["scenario", "year", "variable"],
            how="left",
            validate="many_to_one",
        )
    )
    assert annual.scenario_demand.notna().all()
    annual["intensity_per_PJ"] = annual.value.div(
        annual.scenario_demand.where(annual.scenario_demand.gt(1e-8))
    )
    annual.to_parquet(output / "demand_intensity.parquet", index=False)
    peaks = annual.groupby(["scenario", "impact_category", "year"]).value.sum()
    peaks = peaks.abs().groupby(["scenario", "impact_category"]).max()
    curvature, changes = [], []
    for (scenario, indicator, variable), frame in annual.groupby(
        ["scenario", "impact_category", "variable"]
    ):
        frame = frame.sort_values("year")
        values = frame.value.to_numpy()
        years = frame.year.to_numpy()
        intensity = frame.intensity_per_PJ.to_numpy()
        peak = peaks.loc[scenario, indicator]
        for i in range(1, len(frame) - 1):
            expected = np.interp(
                years[i], years[[i - 1, i + 1]], values[[i - 1, i + 1]]
            )
            deviation = values[i] - expected
            fraction = abs(deviation) / peak if peak else 0
            if fraction > 0.01:
                curvature.append(
                    [
                        scenario,
                        indicator,
                        variable,
                        years[i],
                        values[i - 1],
                        values[i],
                        values[i + 1],
                        deviation,
                        fraction,
                        bool(
                            (values[i] - values[i - 1]) * (values[i + 1] - values[i])
                            < 0
                        ),
                    ]
                )
        for i in range(1, len(frame)):
            if not np.isfinite(intensity[[i - 1, i]]).all() or intensity[i - 1] == 0:
                continue
            ratio = intensity[i] / intensity[i - 1]
            fraction = max(abs(values[i]), abs(values[i - 1])) / peak if peak else 0
            if fraction > 0.01 and (ratio > 1.5 or ratio < 0.67):
                changes.append(
                    [
                        scenario,
                        indicator,
                        variable,
                        years[i - 1],
                        years[i],
                        intensity[i - 1],
                        intensity[i],
                        ratio,
                        fraction,
                    ]
                )
    pd.DataFrame(
        curvature,
        columns=[
            "scenario",
            "indicator",
            "variable",
            "year",
            "previous",
            "value",
            "next",
            "deviation",
            "fraction_of_peak",
            "reversal",
        ],
    ).sort_values("fraction_of_peak", ascending=False).to_csv(
        output / "variable_curvature.csv", index=False
    )
    pd.DataFrame(
        changes,
        columns=[
            "scenario",
            "indicator",
            "variable",
            "from_year",
            "to_year",
            "old_intensity",
            "new_intensity",
            "ratio",
            "fraction_of_peak",
        ],
    ).sort_values("fraction_of_peak", ascending=False).to_csv(
        output / "large_intensity_changes.csv", index=False
    )

    land = data[data.impact_category.eq("land occupation")]
    sectors = (
        land.groupby(["scenario", "year", "sector"]).value.sum().unstack(fill_value=0)
    )
    shares = sectors.div(sectors.sum(axis=1), axis=0) * 100
    sectors.to_csv(output / "sector_land_totals_m2year.csv")
    shares.to_csv(output / "sector_land_shares_percent.csv")
    selected = annual.loc[
        annual.scenario.eq("SPS2_bas0")
        & annual.year.eq(2030)
        & annual.impact_category.eq("land occupation")
    ].copy()
    selected["short"] = selected.variable.str.removeprefix("EXT - 0 - ")
    selected["total_share_percent"] = selected.value / selected.value.sum() * 100
    selected.to_csv(output / "SPS2_bas0_2030_land_attribution.csv", index=False)

    # Record scenario-side energy market shares at the investigated turning points.
    manifest = json.loads(
        next((batch / "scenarios/SPS2_bas0").glob("*.manifest.json")).read_text()
    )
    build = Path(manifest["package"]).parent.parent
    source = pd.read_csv(build / "inputs/scenario_data/scenario_data.csv")
    config = yaml.safe_load(
        (build / "inputs/configuration_file/config.yaml").read_text()
    )
    columns = sorted([c for c in source.columns if c.isdigit()], key=int)
    indexed_source = source.set_index(["scenario", "variables"])
    assert indexed_source.index.is_unique
    for (scenario, variable), frame in demands.groupby(["scenario", "variable"]):
        alias = variable.removeprefix("EXT - 0 - ")
        source_variable = config["production pathways"][alias]["production volume"][
            "variable"
        ]
        row = indexed_source.loc[(scenario, source_variable), columns]
        interpolated = np.interp(
            frame.year, list(map(int, columns)), row.to_numpy(float)
        )
        np.testing.assert_allclose(
            frame.scenario_demand, interpolated, rtol=1e-10, atol=1e-10
        )
    (output / "screen_verification.json").write_text(
        json.dumps(
            {
                "finite_result_rows": len(data),
                "aggregate_reconciliations": len(actual),
                "STEM_to_demand_checks": len(demands),
                "status": "passed",
            },
            indent=2,
        )
        + "\n"
    )
    mixes = []
    cases = [
        ("SPS3_soc2", [2045, 2050], "diesel"),
        ("SPS3_soc3", [2045, 2050], "electricity, high voltage"),
        ("SPS2_fin3", [2035, 2040], "methane"),
        ("SPS1_nuc3", [2045, 2050], "electricity, high voltage"),
    ]
    for scenario, years, fuel in cases:
        market = next(
            m for m in config["markets"] if m["name"] == f"market for {fuel} (SPS)"
        )
        rows = source.loc[source.scenario.eq(scenario)].set_index("variables")
        for year in years:
            values = []
            for alias in market["includes"]:
                variable = config["production pathways"][alias]["production volume"][
                    "variable"
                ]
                value = np.interp(
                    year,
                    list(map(int, columns)),
                    rows.loc[variable, columns].to_numpy(float),
                )
                values.append((alias, variable, value))
            denominator = sum(v for _, _, v in values)
            for alias, variable, value in values:
                mixes.append(
                    dict(
                        scenario=scenario,
                        year=year,
                        market=fuel,
                        alias=alias,
                        variable=variable,
                        energy_PJ=value,
                        share_percent=100 * value / denominator,
                        interpolated=str(year) not in columns,
                    )
                )
    pd.DataFrame(mixes).to_csv(output / "selected_STEM_market_shares.csv", index=False)

    figures = []
    fig = go.Figure()
    for family in range(1, 5):
        scenario = f"SPS{family}_bas0"
        series = shares.loc[scenario, "residential"]
        fig.add_scatter(x=series.index, y=series, mode="lines+markers", name=scenario)
    fig.update_layout(
        title="Residential share of land occupation — all supply locations",
        xaxis_title="Year",
        yaxis_title="Share of total (%)",
        yaxis_range=[0, 100],
    )
    figures.append(fig)
    fig = go.Figure()
    groups = {
        "Residential wood space heating": selected.short.eq(
            "FE_residential_space_heating_wood"
        ),
        "Residential wood water heating": selected.short.eq(
            "FE_residential_water_heating_wood"
        ),
        "Other residential": selected.short.str.startswith("FE_residential")
        & ~selected.short.str.endswith("_wood"),
        "All other sectors": ~selected.short.str.startswith("FE_residential"),
    }
    for label, mask in groups.items():
        fig.add_bar(
            name=label,
            x=["SPS2 baseline, 2030"],
            y=[selected.loc[mask, "value"].sum() / 1e6],
        )
    fig.update_layout(
        title="What drives the large residential share?",
        barmode="stack",
        yaxis_title="Land occupation (km²·year)",
    )
    figures.append(fig)
    fig = go.Figure()
    for variable, label in [
        ("FE_residential_space_heating_wood", "Residential logs"),
        ("FE_industry_process_wood", "Industrial wood"),
        ("FE_services_wood", "Services wood"),
        ("FE_residential_space_heating_pellet", "Residential pellets"),
        ("FE_residential_space_heating_heat_pump", "Residential heat pump"),
    ]:
        row = selected.loc[selected.short.eq(variable)].iloc[0]
        fig.add_bar(name=label, x=[label], y=[row.intensity_per_PJ / 1e6])
    fig.update_layout(
        title="Land occupation per PJ of final energy — SPS2 baseline, 2030",
        yaxis_title="km²·year / PJ final energy",
        showlegend=False,
    )
    figures.append(fig)
    indicators = sorted(data.impact_category.unique())
    fig = make_subplots(rows=3, cols=2, subplot_titles=indicators)
    for i, indicator in enumerate(indicators):
        for family in range(1, 5):
            scenario = f"SPS{family}_bas0"
            values = (
                data.loc[
                    data.scenario.eq(scenario) & data.impact_category.eq(indicator)
                ]
                .groupby("year")
                .value.sum()
            )
            fig.add_scatter(
                x=values.index,
                y=values / values.loc[2020] * 100,
                mode="lines+markers",
                name=scenario,
                legendgroup=scenario,
                showlegend=i == 0,
                row=i // 2 + 1,
                col=i % 2 + 1,
            )
    fig.update_layout(title="Baseline trajectories (2020 = 100)", height=850)
    figures.append(fig)
    html = [
        '<!doctype html><meta charset="utf-8"><title>Indicator pattern audit</title>',
        '<body style="font:16px system-ui;max-width:1200px;margin:30px auto">',
        "<h1>Indicator pattern audit — latest results</h1>",
        "<p>40 scenarios, 2020–2050. All contribution locations are included. "
        "Land occupation is area × time throughout the supply chain; it is not new land clearing. "
        "Final-energy intensities compare fuel/electricity inputs, not equal amounts of useful heat.</p>",
    ]
    for i, figure in enumerate(figures):
        figure.update_layout(template="plotly_white")
        html.append(figure.to_html(full_html=False, include_plotlyjs=i == 0))
    (output / "patterns.html").write_text("\n".join(html) + "</body>\n")
    print(f"Screened {len(data):,} result rows; wrote {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    audit(parser.parse_args().batch.resolve())
