"""Protect numerical aggregation, signs and baseline semantics in the explorer."""

from pathlib import Path

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
)
from dashboard.app import create_app, make_figure


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


def test_all_1680_chart_totals_match_validated_results(store):
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
    assert len(actual) == 1680
    np.testing.assert_allclose(
        actual.to_numpy(), reference.to_numpy(), rtol=1e-12, atol=1e-6
    )


def test_empty_selection_and_inactive_variables(store):
    assert store.select("climate change", [], SECTORS, LOCATIONS).empty
    assert store.select("climate change", store.scenarios, [], LOCATIONS).empty
    assert len(store.variables(SECTORS)) == 101
    zero_variables = store.frame.groupby("variable", observed=True).value.apply(
        lambda x: x.eq(0).all()
    )
    name = zero_variables[zero_variables].index[0]
    selected = store.select(
        "climate change", store.scenarios[:1], SECTORS, LOCATIONS, [name]
    )
    chart, undefined = prepare_chart(
        aggregate_series(selected), "climate change", [2020, 2050], "change"
    )
    assert len(chart) == 7 and len(undefined) == 1
    assert chart.value.eq(0).all() and chart.plot_value.isna().all()


def test_units_are_converted_only_for_display(store):
    for indicator, (_, unit, factor, display_unit) in INDICATORS.items():
        filtered = store.select(indicator, store.scenarios[:1], SECTORS, LOCATIONS)
        series = aggregate_series(filtered)
        chart, _ = prepare_chart(series, indicator, [2020, 2050], "absolute")
        np.testing.assert_allclose(chart.plot_value * factor, chart.value)
        assert (
            chart.native_unit.eq(unit).all() and chart.plot_unit.eq(display_unit).all()
        )


def test_dash_routes_serve_local_app(store):
    client = create_app().server.test_client()
    for path in ("/", "/_dash-layout", "/_dash-dependencies", "/assets/style.css"):
        assert client.get(path).status_code == 200
