"""Protect numerical aggregation, signs and baseline semantics in the explorer."""

from pathlib import Path
import json

import numpy as np
import pandas as pd
import pytest

from dashboard.data import (
    DEFAULT_RESULTS,
    INDICATORS,
    LOCATIONS,
    SECTORS,
    ResultStore,
    aggregate_series,
    percent_change,
    prepare_chart,
    digest,
)
from dashboard.app import create_app, make_figure, indicator_context


def test_negative_baseline_and_zero_are_not_misleading():
    changes = percent_change(
        np.array([-5.0, -20.0, 5.0, 12.0]), np.array([-10.0, -10.0, 0.0, 10.0])
    )
    np.testing.assert_allclose(changes[[0, 1, 3]], [50.0, -100.0, 20.0])
    assert np.isnan(changes[2])


def test_percent_baseline_stays_2020_after_year_filter():
    series = pd.DataFrame(
        {"series": ["A"] * 3, "year": [2020, 2030, 2050], "value": [10.0, 15.0, 20.0]}
    )
    chart, undefined = prepare_chart(series, "climate change", [2030, 2050], "change")
    np.testing.assert_allclose(chart.plot_value, [50.0, 100.0])
    assert undefined == []
    assert chart.native_unit.eq("kg CO2-eq.").all()


def test_other_uses_and_negative_contributions_preserve_totals():
    frame = pd.DataFrame(
        [
            {"scenario": "A", "variable": f"use{i}", "year": year, "value": amount}
            for year in (2020, 2050)
            for i, amount in enumerate([100, -80, 5, -3, 0])
        ]
        + [{"scenario": "B", "variable": "use0", "year": 2020, "value": 1000}]
    )
    grouped = aggregate_series(frame, "contributions", "variable", "A", top_n=2)
    assert set(grouped.series) == {"use0", "use1", "Other uses"}
    np.testing.assert_allclose(grouped.groupby("year").value.sum(), [22, 22])
    assert grouped.loc[grouped.series.eq("use1"), "value"].eq(-80).all()
    chart, _ = prepare_chart(grouped, "climate change", [2020, 2050], "absolute")
    fig = make_figure(chart, "contributions", "absolute", "climate change")
    assert fig.layout.barmode == "relative"
    np.testing.assert_allclose(fig.data[-1].y, [22e-9, 22e-9])


@pytest.fixture(scope="module")
def store():
    if not DEFAULT_RESULTS.exists():
        pytest.skip("Local validated results are not available")
    return ResultStore.load()


def test_all_chart_totals_match_validated_results(store):
    actual = (
        store.frame.groupby(["scenario", "year", "impact_category"], observed=True)
        .value.sum()
        .sort_index()
    )
    reference = (
        pd.read_csv(
            DEFAULT_RESULTS / "indicator_totals.csv", float_precision="round_trip"
        )
        .set_index(["scenario", "year", "impact_category"])
        .value.sort_index()
    )
    assert list(actual.index) == list(reference.index)
    assert len(actual) == len(store.scenarios) * len(store.years) * len(
        store.indicators
    )
    np.testing.assert_allclose(
        actual.to_numpy(), reference.to_numpy(), rtol=1e-12, atol=1e-6
    )


def test_empty_selection_and_inactive_variables(store):
    indicator = next(k for k in store.indicators if k.endswith("climate change"))
    assert store.select(indicator, [], SECTORS, LOCATIONS).empty
    assert store.select(indicator, store.scenarios, [], LOCATIONS).empty
    assert len(store.variables(SECTORS)) == 101
    zero_variables = store.frame.groupby("variable", observed=True).value.apply(
        lambda x: x.eq(0).all()
    )
    name = zero_variables[zero_variables].index[0]
    selected = store.select(indicator, store.scenarios[:1], SECTORS, LOCATIONS, [name])
    chart, undefined = prepare_chart(
        aggregate_series(selected), indicator, [2020, 2050], "change", store.indicators
    )
    assert len(chart) == 7 and len(undefined) == 1
    assert chart.value.eq(0).all() and chart.plot_value.isna().all()


def test_units_are_converted_only_for_display(store):
    for indicator, (_, unit, factor, display_unit) in store.indicators.items():
        filtered = store.select(indicator, store.scenarios[:1], SECTORS, LOCATIONS)
        series = aggregate_series(filtered)
        chart, _ = prepare_chart(
            series, indicator, [2020, 2050], "absolute", store.indicators
        )
        np.testing.assert_allclose(chart.plot_value * factor, chart.value)
        assert (
            chart.native_unit.eq(unit).all() and chart.plot_unit.eq(display_unit).all()
        )


def test_dash_routes_serve_local_app(store):
    client = create_app().server.test_client()
    for path in ("/", "/_dash-layout", "/_dash-dependencies", "/assets/style.css"):
        assert client.get(path).status_code == 200


@pytest.fixture
def registry_batch(tmp_path):
    methods = [
        {
            "indicator": "EF v3.1 - climate change",
            "family": "EF v3.1",
            "label": "Climate change",
            "unit": "kg CO2-Eq",
            "display_divisor": 1e9,
            "display_unit": "Mt CO₂-eq.",
        },
        {
            "indicator": "RELICS - Copper",
            "family": "RELICS",
            "label": "Copper",
            "unit": "kilogram",
            "display_divisor": 1000,
            "display_unit": "tonnes",
        },
    ]
    for family, name, kind, unit, product in [
        ("Refined-metal inputs", "Copper", "contained_metal", "Cu", "copper, cathode"),
        (
            "Alloy inputs",
            "Iron and steel alloys",
            "alloy_mass",
            "alloy",
            "steel, low-alloyed",
        ),
    ]:
        methods.append(
            {
                "indicator": f"{family} - {name}",
                "family": family,
                "label": name,
                "unit": f"kg {unit}",
                "display_divisor": 1000,
                "display_unit": f"tonnes {unit}",
                "definition": {
                    "kind": kind,
                    "name": name,
                    "products": [
                        {"product": product, "factor": 1, "basis": "test mass basis"}
                    ],
                },
                "limitation": "Selected products only.",
                "interpretation": "Do not add metal and alloy indicators.",
            }
        )
    frame = pd.DataFrame(
        [
            dict(
                model="remind",
                scenario=scenario,
                region="CH",
                year=year,
                variable="EXT - 0 - FE_industry_heat",
                sector="industry",
                impact_category=m["indicator"],
                unit=m["unit"],
                location=location,
                value=value,
            )
            for scenario in ("SPS1_bas0", "SPS2_bas0")
            for year in (2020, 2050)
            for m in methods
            for location, value in zip(LOCATIONS, [1000.0, -200.0, 0.0])
        ]
    )
    frame.to_parquet(tmp_path / "df_final.parquet", index=False)
    (tmp_path / "method_registry.json").write_text(
        json.dumps(
            {
                "methods": methods,
                "unavailable_elements": {"Uranium": "No verified mass basis"},
            }
        )
    )
    summary = dict(
        method_registry="method_registry.json",
        dense=True,
        nonfinite_results=0,
        unlinked_variables=0,
        scenarios=2,
        scenario_names=["SPS1_bas0", "SPS2_bas0"],
        years=[2020, 2050],
        variables=["EXT - 0 - FE_industry_heat"],
        indicators=len(methods),
        independent_checks=4 * len(methods),
        finished_at="2026-10-11T12:00:00+00:00",
        outputs_sha256={
            name: digest(tmp_path / name)
            for name in ("df_final.parquet", "method_registry.json")
        },
    )
    (tmp_path / "completion_summary.json").write_text(json.dumps(summary))
    return tmp_path


def test_registry_dashboard_retains_units_signs_and_all_methods(registry_batch):
    store = ResultStore.load(registry_batch)
    series = aggregate_series(
        store.select("RELICS - Copper", store.scenarios, SECTORS, LOCATIONS)
    )
    chart, _ = prepare_chart(
        series, "RELICS - Copper", [2020, 2050], "absolute", store.indicators
    )
    assert chart.value.eq(800).all() and chart.plot_value.eq(0.8).all()
    assert chart.native_unit.eq("kilogram").all() and chart.plot_unit.eq("tonnes").all()
    app = create_app(registry_batch)
    layout = app.server.test_client().get("/_dash-layout").get_data(as_text=True)
    assert '"id":"method-family"' in layout and "RELICS" in layout

    family_key = next(k for k in app.callback_map if "indicator.options" in k)
    client = app.server.test_client()
    response = client.post(
        "/_dash-update-component",
        json={
            "output": family_key,
            "outputs": [
                {"id": "indicator", "property": "options"},
                {"id": "indicator", "property": "value"},
            ],
            "inputs": [
                {"id": "method-family", "property": "value", "value": "RELICS"},
                {"id": "reset", "property": "n_clicks", "value": None},
            ],
            "state": [
                {
                    "id": "indicator",
                    "property": "value",
                    "value": "EF v3.1 - climate change",
                }
            ],
            "changedPropIds": ["method-family.value"],
        },
    )
    assert response.status_code == 200
    selection = response.get_json()["response"]["indicator"]
    assert selection["value"] == "RELICS - Copper"
    assert selection["options"] == [{"label": "Copper", "value": "RELICS - Copper"}]


def test_registry_dashboard_rejects_modified_data(registry_batch):
    with (registry_batch / "df_final.parquet").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        ResultStore.load(registry_batch)


@pytest.mark.parametrize(
    "indicator,unit,basis",
    [
        ("Refined-metal inputs - Copper", "tonnes Cu", "contained element"),
        ("Alloy inputs - Iron and steel alloys", "tonnes alloy", "whole alloy"),
    ],
)
def test_material_mass_basis_and_coverage_reach_plot_and_export(
    registry_batch, indicator, unit, basis
):
    store = ResultStore.load(registry_batch)
    context = indicator_context(store.method_metadata[indicator])
    assert basis in context["quantity_basis"]
    assert "consuming activity" in context["location_basis"]
    assert context["selected_products"]
    assert (
        store.coverage_metadata["unavailable_elements"]["Uranium"]
        == "No verified mass basis"
    )
    chart, _ = prepare_chart(
        aggregate_series(store.select(indicator, store.scenarios, SECTORS, LOCATIONS)),
        indicator,
        [2020, 2050],
        "absolute",
        store.indicators,
    )
    assert chart.display_value.eq(0.8).all()
    assert chart.display_unit.eq(unit).all()


def test_registry_dashboard_rejects_incomplete_grid_even_with_updated_hash(
    registry_batch,
):
    path = registry_batch / "df_final.parquet"
    pd.read_parquet(path).iloc[:-1].to_parquet(path, index=False)
    summary_path = registry_batch / "completion_summary.json"
    summary = json.loads(summary_path.read_text())
    summary["outputs_sha256"][path.name] = digest(path)
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="Incomplete dense"):
        ResultStore.load(registry_batch)
