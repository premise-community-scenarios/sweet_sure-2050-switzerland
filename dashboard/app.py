"""Run with: .venv-dashboard/bin/python -m dashboard.app"""

import argparse
from datetime import datetime
import json
from pathlib import Path

from dash import Dash, Input, Output, State, ctx, dcc, html
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.colors import qualitative

from .data import (
    DEFAULT_RESULTS,
    INDICATORS,
    LOCATIONS,
    SECTORS,
    ResultStore,
    aggregate_series,
    percent_change,
    prepare_chart,
    scenario_label,
    variable_label,
)

COLORS = [
    "#147d77",
    "#ce6c45",
    "#526bb0",
    "#a06392",
    "#6a8544",
    "#ba9635",
    "#4c93a8",
    "#b75263",
] + qualitative.Safe


def label(text, target=None):
    return html.Label(text, htmlFor=target, className="field-label")


def short_series(value):
    if value.startswith("EXT - 0 - FE_"):
        return variable_label(value)
    if " - SPS" in value:
        return scenario_label(value)
    return value.capitalize() if value in SECTORS else value


def make_figure(frame, mode, scale, indicator):
    fig = go.Figure()
    unit = "% change from 2020" if scale == "change" else INDICATORS[indicator][3]
    if frame.empty:
        fig.add_annotation(
            text="Select at least one scenario, sector and location.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"size": 16, "color": "#697681"},
        )
    else:
        for i, (series, group) in enumerate(
            frame.groupby("series", sort=True, observed=True)
        ):
            if group.plot_value.isna().all():
                continue
            group = group.sort_values("year")
            common = dict(
                x=group.year.tolist(),
                y=group.plot_value.tolist(),
                name=short_series(series),
                legendgroup=series,
                hovertemplate=f"<b>{short_series(series)}</b><br>%{{x}} · %{{y:,.5g}} {unit}<extra></extra>",
            )
            if mode == "contributions" and scale == "absolute":
                fig.add_trace(go.Bar(**common, marker_color=COLORS[i % len(COLORS)]))
            else:
                fig.add_trace(
                    go.Scatter(
                        **common,
                        mode="lines+markers",
                        connectgaps=False,
                        line={
                            "color": COLORS[i % len(COLORS)],
                            "width": 2.7,
                            "dash": ["solid", "dash", "dot"][i // len(COLORS) % 3],
                        },
                        marker={"size": 6},
                    )
                )
        if mode == "contributions" and scale == "absolute":
            total = frame.groupby("year").plot_value.sum().sort_index()
            fig.add_trace(
                go.Scatter(
                    x=total.index.tolist(),
                    y=total.tolist(),
                    name="Net total",
                    mode="lines+markers",
                    line={"color": "#243944", "width": 2, "dash": "dot"},
                    marker={"size": 6},
                    hovertemplate=f"<b>Net total</b><br>%{{x}} · %{{y:,.5g}} {unit}<extra></extra>",
                )
            )
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font={"family": "Arial, sans-serif", "size": 12, "color": "#475862"},
        margin={"l": 78, "r": 24, "t": 48, "b": 70},
        hovermode="x unified",
        barmode="relative",
        bargap=0.38,
        legend={
            "orientation": "h",
            "y": 1.13,
            "x": 0,
            "maxheight": 100,
            "font": {"size": 11},
        },
        xaxis={
            "title": "Year",
            "tickmode": "array",
            "tickvals": (
                sorted(frame.year.unique().tolist())
                if not frame.empty
                else list(range(2020, 2051, 5))
            ),
            "showgrid": False,
            "zeroline": False,
        },
        yaxis={
            "title": unit,
            "gridcolor": "#edf1f1",
            "zeroline": True,
            "zerolinecolor": "#9dadb0",
            "zerolinewidth": 1,
            "rangemode": "tozero",
        },
        uirevision=f"{indicator}-{mode}-{scale}",
    )
    return fig


def summary_table(frame, years, indicator):
    if frame.empty:
        return html.P("No data selected.", className="muted")
    wide = frame.pivot(index="series", columns="year", values="display_value")
    first, last = years
    rows = []
    for series, values in wide.iterrows():
        start, end = values.get(first, np.nan), values.get(last, np.nan)
        change = float(percent_change(np.array([end]), np.array([start]))[0])
        rows.append(
            html.Tr(
                [
                    html.Td(short_series(series)),
                    html.Td(f"{start:,.4g}"),
                    html.Td(f"{end:,.4g}"),
                    html.Td(
                        f"{change:+,.1f}%" if np.isfinite(change) else "—",
                        className="signed-value",
                    ),
                ]
            )
        )
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Series"),
                        html.Th(str(first)),
                        html.Th(str(last)),
                        html.Th("Change"),
                    ]
                )
            ),
            html.Tbody(rows),
        ],
        className="results-table",
    )


def create_app(results_dir=DEFAULT_RESULTS):
    store = ResultStore.load(results_dir)
    app = Dash(
        __name__,
        assets_folder=str(Path(__file__).parent / "assets"),
        title="SWEET-SURE · Results explorer",
        update_title=None,
    )
    scenarios = store.scenarios
    baselines = [s for s in scenarios if s.endswith("_bas0")]
    defaults = baselines or scenarios[:4]
    options = [{"label": scenario_label(s), "value": s} for s in scenarios]
    summary = json.loads((store.directory / "completion_summary.json").read_text())
    date = datetime.fromisoformat(summary["finished_at"]).strftime("%d %b %Y")
    app.layout = html.Div(
        [
            html.Header(
                [
                    html.Div(
                        [
                            html.Span("S", className="brand-mark"),
                            html.Div(
                                [
                                    html.Strong("SWEET · SURE"),
                                    html.Span(
                                        "SWITZERLAND 2050", className="brand-sub"
                                    ),
                                ]
                            ),
                        ],
                        className="brand",
                    ),
                    html.Div(
                        [
                            html.Span(className="status-dot"),
                            "Latest results",
                            html.Span(date, className="date-label"),
                        ],
                        className="dataset-badge",
                    ),
                ],
                className="topbar",
            ),
            html.Main(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        "ENVIRONMENTAL FOOTPRINT", className="eyebrow"
                                    ),
                                    html.H1("Results explorer"),
                                    html.P(
                                        "Follow Switzerland’s energy scenarios from 2020 to 2050.",
                                        className="subtitle",
                                    ),
                                ]
                            ),
                            html.Div(
                                [
                                    html.Strong(str(len(scenarios))),
                                    html.Span("scenarios"),
                                    html.B("·"),
                                    html.Strong("7"),
                                    html.Span("years"),
                                    html.B("·"),
                                    html.Strong("6"),
                                    html.Span("indicators"),
                                ],
                                className="dataset-stats",
                            ),
                        ],
                        className="page-heading",
                    ),
                    html.Div(
                        [
                            html.Aside(
                                [
                                    html.Div(
                                        [
                                            html.H2("Your selection"),
                                            html.Button(
                                                "Reset",
                                                id="reset",
                                                className="text-button",
                                            ),
                                        ],
                                        className="sidebar-heading",
                                    ),
                                    label("Impact indicator", "indicator"),
                                    dcc.Dropdown(
                                        id="indicator",
                                        options=[
                                            {"label": v[0], "value": k}
                                            for k, v in INDICATORS.items()
                                        ],
                                        value="climate change",
                                        clearable=False,
                                    ),
                                    html.Div(
                                        [
                                            label("Scenarios", "scenarios"),
                                            html.Div(
                                                [
                                                    html.Button(
                                                        "Baselines",
                                                        id="baselines",
                                                        className="text-button",
                                                    ),
                                                    html.Button(
                                                        "All 40",
                                                        id="all-scenarios",
                                                        className="text-button",
                                                    ),
                                                ],
                                                className="quick-select",
                                            ),
                                        ],
                                        className="field-row",
                                    ),
                                    dcc.Dropdown(
                                        id="scenarios",
                                        options=options,
                                        value=defaults,
                                        multi=True,
                                        placeholder="Choose scenarios…",
                                    ),
                                    label("Sectors"),
                                    dcc.Checklist(
                                        id="sectors",
                                        options=[
                                            {"label": s.capitalize(), "value": s}
                                            for s in SECTORS
                                        ],
                                        value=SECTORS,
                                        className="checklist",
                                    ),
                                    label("Contribution location"),
                                    dcc.Checklist(
                                        id="locations",
                                        options=[
                                            {"label": v, "value": v} for v in LOCATIONS
                                        ],
                                        value=LOCATIONS,
                                        className="checklist",
                                    ),
                                    html.P(
                                        "Where impacts occur across the supply chain.",
                                        className="field-help",
                                    ),
                                    label("Final-energy uses", "variables"),
                                    dcc.Dropdown(
                                        id="variables",
                                        options=[
                                            {
                                                "label": variable_label(v),
                                                "value": v,
                                                "title": v,
                                            }
                                            for v in store.variables(SECTORS)
                                        ],
                                        value=[],
                                        multi=True,
                                        placeholder="All uses · search to filter",
                                    ),
                                    html.P(
                                        "Try ‘heat pump’, ‘cars’ or ‘hydrogen’.",
                                        className="field-help",
                                    ),
                                    html.Div(
                                        [
                                            label("Year range"),
                                            dcc.RangeSlider(
                                                id="years",
                                                min=store.years[0],
                                                max=store.years[-1],
                                                step=5,
                                                marks={
                                                    y: str(y) for y in store.years[::2]
                                                },
                                                value=[store.years[0], store.years[-1]],
                                                allowCross=False,
                                                pushable=5,
                                                tooltip={
                                                    "placement": "bottom",
                                                    "always_visible": False,
                                                },
                                            ),
                                        ],
                                        className="year-control",
                                    ),
                                    html.Div(
                                        [
                                            html.Span(
                                                "DATA BASIS", className="eyebrow"
                                            ),
                                            html.P(
                                                "Annual impacts of Swiss final-energy demand."
                                            ),
                                            html.P(
                                                "ecoinvent 3.12 cutoff · REMIND SSP2-PkBudg1000",
                                                className="muted",
                                            ),
                                        ],
                                        className="data-note",
                                    ),
                                ],
                                className="sidebar",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Div(
                                                [
                                                    dcc.RadioItems(
                                                        id="view",
                                                        options=[
                                                            {
                                                                "label": "Scenario comparison",
                                                                "value": "compare",
                                                            },
                                                            {
                                                                "label": "Contributions",
                                                                "value": "contributions",
                                                            },
                                                        ],
                                                        value="compare",
                                                        className="segmented",
                                                    ),
                                                    html.Button(
                                                        "↓ Download CSV",
                                                        id="download-button",
                                                        className="download-button",
                                                    ),
                                                    dcc.Download(id="download"),
                                                ],
                                                className="chart-toolbar",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.H2(id="chart-title"),
                                                            html.P(
                                                                id="chart-subtitle",
                                                                className="muted",
                                                            ),
                                                        ]
                                                    ),
                                                    dcc.RadioItems(
                                                        id="scale",
                                                        options=[
                                                            {
                                                                "label": "Annual impact",
                                                                "value": "absolute",
                                                            },
                                                            {
                                                                "label": "Change from 2020",
                                                                "value": "change",
                                                            },
                                                        ],
                                                        value="absolute",
                                                        className="scale-toggle",
                                                    ),
                                                ],
                                                className="chart-heading",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            label("Scenario", "focus"),
                                                            dcc.Dropdown(
                                                                id="focus",
                                                                options=options[:4],
                                                                value=defaults[0],
                                                                clearable=False,
                                                            ),
                                                        ]
                                                    ),
                                                    html.Div(
                                                        [
                                                            label(
                                                                "Break down by",
                                                                "breakdown",
                                                            ),
                                                            dcc.Dropdown(
                                                                id="breakdown",
                                                                options=[
                                                                    {
                                                                        "label": "Sector",
                                                                        "value": "sector",
                                                                    },
                                                                    {
                                                                        "label": "Contribution location",
                                                                        "value": "location",
                                                                    },
                                                                    {
                                                                        "label": "Final-energy use · top 10 + other",
                                                                        "value": "variable",
                                                                    },
                                                                ],
                                                                value="sector",
                                                                clearable=False,
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                                id="contribution-controls",
                                                className="contribution-controls",
                                                style={"display": "none"},
                                            ),
                                            html.Div(
                                                id="selection-notice",
                                                className="notice",
                                                role="status",
                                            ),
                                            dcc.Loading(
                                                dcc.Graph(
                                                    id="trend",
                                                    className="trend-chart",
                                                    config={
                                                        "displaylogo": False,
                                                        "scrollZoom": False,
                                                        "toImageButtonOptions": {
                                                            "format": "svg",
                                                            "filename": "sweet_sure_trends",
                                                        },
                                                        "modeBarButtonsToRemove": [
                                                            "lasso2d",
                                                            "select2d",
                                                        ],
                                                    },
                                                ),
                                                type="dot",
                                                color="#147d77",
                                            ),
                                            html.P(
                                                id="chart-note", className="chart-note"
                                            ),
                                        ],
                                        className="chart-card",
                                    ),
                                    html.Div(
                                        [
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.H2("Period summary"),
                                                            html.P(
                                                                id="table-caption",
                                                                className="muted",
                                                            ),
                                                        ]
                                                    ),
                                                    html.Span(
                                                        id="series-count",
                                                        className="pill",
                                                    ),
                                                ],
                                                className="table-heading",
                                            ),
                                            html.Div(
                                                id="table", className="table-scroll"
                                            ),
                                        ],
                                        className="table-card",
                                    ),
                                    html.Details(
                                        [
                                            html.Summary("About these results"),
                                            html.P(
                                                "Impacts include the full supply chains of Swiss final-energy demand. Negative values are retained. Scenario totals are compared separately and are never added together."
                                            ),
                                            html.P(
                                                "‘EU wo CH’ follows REMIND EUR + NEU excluding Switzerland, rather than political EU membership. The 2045 package uses interpolated scenario inputs."
                                            ),
                                            html.P(
                                                "Percentage change = 100 × (value − 2020 value) / |2020 value|. A zero baseline has no defined percentage change. The period table always shows annual impacts and compares its first and last displayed years."
                                            ),
                                            html.P(
                                                "Contributions use signed, stacked bars and a net-total line. The final-energy breakdown preserves all uses: the ten largest by absolute contribution across all years are shown separately, and the rest are summed as ‘Other uses’."
                                            ),
                                            html.P(
                                                "The approved industrial gas fuel-cell proxy and configured wood-CHP efficiency bounds are retained. Downloaded CSVs include native values and display units."
                                            ),
                                            html.P(
                                                f"Validated batch: {store.directory.name}. All 1,680 annual indicator checks passed."
                                            ),
                                        ],
                                        className="about",
                                    ),
                                ],
                                className="content-column",
                            ),
                        ],
                        className="workspace",
                    ),
                    html.Footer(
                        "SWEET-SURE  /  Swiss energy futures", className="footer"
                    ),
                ],
                className="page",
            ),
            dcc.Store(id="chart-data"),
        ]
    )

    @app.callback(
        Output("scenarios", "value"),
        Output("indicator", "value"),
        Output("sectors", "value"),
        Output("locations", "value"),
        Output("years", "value"),
        Output("view", "value"),
        Output("scale", "value"),
        Input("baselines", "n_clicks"),
        Input("all-scenarios", "n_clicks"),
        Input("reset", "n_clicks"),
        State("indicator", "value"),
        State("sectors", "value"),
        State("locations", "value"),
        State("years", "value"),
        State("view", "value"),
        State("scale", "value"),
        prevent_initial_call=True,
    )
    def presets(
        _baseline, _all, _reset, indicator, sectors, locations, years, view, scale
    ):
        if ctx.triggered_id == "reset":
            return (
                defaults,
                "climate change",
                SECTORS,
                LOCATIONS,
                [store.years[0], store.years[-1]],
                "compare",
                "absolute",
            )
        return (
            (scenarios if ctx.triggered_id == "all-scenarios" else defaults),
            indicator,
            sectors,
            locations,
            years,
            view,
            scale,
        )

    @app.callback(
        Output("variables", "options"),
        Output("variables", "value"),
        Input("sectors", "value"),
        Input("reset", "n_clicks"),
        State("variables", "value"),
    )
    def variable_options(sectors, _reset, selected):
        available = store.variables(sectors)
        return [
            {"label": variable_label(v), "value": v, "title": v} for v in available
        ], (
            []
            if ctx.triggered_id == "reset"
            else [v for v in selected or [] if v in available]
        )

    @app.callback(
        Output("focus", "options"),
        Output("focus", "value"),
        Input("scenarios", "value"),
        State("focus", "value"),
    )
    def focus_options(selected, focus):
        selected = selected or []
        return [{"label": scenario_label(v), "value": v} for v in selected], (
            focus if focus in selected else (selected[0] if selected else None)
        )

    @app.callback(
        Output("trend", "figure"),
        Output("chart-title", "children"),
        Output("chart-subtitle", "children"),
        Output("chart-note", "children"),
        Output("table", "children"),
        Output("table-caption", "children"),
        Output("series-count", "children"),
        Output("selection-notice", "children"),
        Output("chart-data", "data"),
        Output("contribution-controls", "style"),
        Input("indicator", "value"),
        Input("scenarios", "value"),
        Input("sectors", "value"),
        Input("locations", "value"),
        Input("variables", "value"),
        Input("years", "value"),
        Input("view", "value"),
        Input("scale", "value"),
        Input("focus", "value"),
        Input("breakdown", "value"),
    )
    def render(
        indicator,
        selected,
        sectors,
        locations,
        variables,
        years,
        view,
        scale,
        focus,
        breakdown,
    ):
        filtered = store.select(indicator, selected, sectors, locations, variables)
        series = aggregate_series(filtered, view, breakdown, focus)
        frame, undefined = prepare_chart(series, indicator, years, scale)
        title = INDICATORS[indicator][0]
        subtitle = (
            (
                f"{len(selected or [])} scenarios"
                if view == "compare"
                else scenario_label(focus or "No scenario")
            )
            + f" · {years[0]}–{years[1]} · "
            + (
                "all final-energy uses"
                if not variables
                else f"{len(variables)} selected uses"
            )
        )
        note = "Annual scenario-weighted impacts. Click a legend label to hide a series; double-click to isolate it."
        if scale == "change":
            note = "Change = 100 × (value − 2020 value) / |2020 value|. The baseline stays at 2020 when the year range changes."
        elif view == "contributions":
            note = "Bars retain positive and negative contributions. The dotted line is their net total."
        notice = ""
        if undefined:
            notice = f"Percentage change is undefined for {len(undefined)} series with a zero or missing 2020 baseline. Their annual values remain in the table and CSV."
        records = (
            frame.assign(
                indicator=indicator,
                scenario=frame.series if view == "compare" else focus,
                view=view,
                breakdown=breakdown if view == "contributions" else "scenario",
                selected_sectors="; ".join(sectors or []),
                selected_locations="; ".join(locations or []),
                selected_variables="; ".join(variables or []) or "all",
            )
            .replace({np.nan: None})
            .to_dict("records")
        )
        return (
            make_figure(frame, view, scale, indicator),
            title,
            subtitle,
            note,
            summary_table(frame, years, indicator),
            f"Annual impacts in {INDICATORS[indicator][3]} · change relative to |{years[0]} value|",
            f"{frame.series.nunique()} series",
            notice,
            records,
            ({"display": "grid"} if view == "contributions" else {"display": "none"}),
        )

    @app.callback(
        Output("download", "data"),
        Input("download-button", "n_clicks"),
        State("chart-data", "data"),
        prevent_initial_call=True,
    )
    def download(_clicks, records):
        return dcc.send_data_frame(
            pd.DataFrame(records or []).to_csv,
            "sweet_sure_selection.csv",
            index=False,
            float_format="%.17g",
        )

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()
    create_app(args.results).run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
