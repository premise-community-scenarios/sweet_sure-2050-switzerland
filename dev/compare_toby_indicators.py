#!/usr/bin/env python3
"""Compare archived Toby results with a validated Pathways batch, without upload.

Blank old cells stay missing. The difference from the sum of stored old values
is decomposed into changes on populated matches, new values on old blank cells,
and contributions from variables absent from the old parameter registry.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd

from download_toby_results import sha256

KEYS = ["scenario", "year", "impact", "unit"]
PARTS = ["matched_change", "latest_on_old_blank", "latest_unregistered"]


def percent_change(delta, baseline):
    """Positive means an increase, including when the old score is negative."""
    return delta.div(baseline.abs().where(baseline.ne(0))).mul(100)


def read_old(report_path, raw_path, mapping, scenario_id):
    old = pd.read_csv(report_path, float_precision="round_trip")
    years = [c for c in old if c.isdigit()]
    if not years or old.prm_id.duplicated().any() or set(old.scn_id) != {scenario_id}:
        raise ValueError("Invalid old report coordinates")
    registered = mapping.loc[mapping.registered].copy()
    registered["prm_id"] = registered.prm_id.astype("int64")
    identities = old.merge(
        registered,
        on=["prm_id", "path", "unit"],
        how="outer",
        validate="one_to_one",
        indicator=True,
    )
    if not identities._merge.eq("both").all():
        raise ValueError("Old parameter ID, path or unit differs from the mapping")
    old = identities.drop(columns="_merge").melt(
        id_vars=["prm_id", "scn_id", "path", "unit", "variable", "impact", "location"],
        value_vars=years,
        var_name="year",
        value_name="old_value",
    )
    old["year"] = old.year.astype(int)
    if not np.isfinite(old.old_value.dropna()).all():
        raise ValueError("Nonfinite old stored value")
    raw = pd.read_csv(
        raw_path,
        names=["prm_id", "scn_id", "year", "raw_value"],
        float_precision="round_trip",
    )
    coordinates = ["prm_id", "scn_id", "year"]
    if raw.duplicated(coordinates).any() or not np.isfinite(raw.raw_value).all():
        raise ValueError("Duplicate or invalid raw database records")
    check = old.loc[old.old_value.notna(), coordinates + ["old_value"]].merge(
        raw, on=coordinates, how="outer", validate="one_to_one", indicator=True
    )
    if (
        not check._merge.eq("both").all()
        or not check.old_value.eq(check.raw_value).all()
    ):
        raise ValueError("Report cells disagree with raw stored database records")
    return old, sorted(map(int, years)), len(raw)


def compare_cells(latest, old, mapping, scenario):
    coordinates = ["variable", "impact", "location", "unit"]
    if latest.duplicated(coordinates + ["year"]).any():
        raise ValueError("Duplicate latest coordinates")
    if not np.isfinite(latest.value).all():
        raise ValueError("Nonfinite latest score")
    cells = latest.rename(
        columns={"value": "latest_value", "scenario": "latest_scenario"}
    ).merge(mapping, on=coordinates, how="left", validate="many_to_one", indicator=True)
    if not cells._merge.eq("both").all():
        raise ValueError("Latest variable, impact, location or unit has no mapping")
    cells = cells.drop(columns="_merge").merge(
        old[coordinates + ["year", "old_value"]],
        on=coordinates + ["year"],
        how="left",
        validate="one_to_one",
    )
    cells["scenario"] = scenario
    observed = cells.old_value.notna()
    blank = cells.registered & ~observed
    cells["old_status"] = np.select(
        [observed, blank], ["stored", "blank_registered"], default="unregistered"
    )
    cells["matched_change"] = (cells.latest_value - cells.old_value).where(observed, 0)
    cells["latest_on_matches"] = cells.latest_value.where(observed, 0)
    cells["latest_on_old_blank"] = cells.latest_value.where(blank, 0)
    cells["latest_unregistered"] = cells.latest_value.where(~cells.registered, 0)
    cells["matched_change_percent"] = percent_change(
        cells.latest_value - cells.old_value, cells.old_value
    )
    return cells


def aggregate(cells, extra=()):
    keys = KEYS + list(extra)
    grouped = cells.groupby(keys, observed=True, dropna=False)
    result = grouped[["old_value", "latest_value", "latest_on_matches", *PARTS]].sum(
        min_count=1
    )
    # NaN means no old observation in this group, not a stored zero.
    result = result.rename(
        columns={"old_value": "old_stored_total", "latest_value": "latest_total"}
    )
    result["old_stored_cells"] = grouped.old_value.count()
    for status in ["blank_registered", "unregistered"]:
        result[f"{status}_cells"] = (
            cells.old_status.eq(status)
            .groupby([cells[k] for k in keys], observed=True, dropna=False)
            .sum()
        )
    result["total_change"] = result.latest_total - result.old_stored_total
    result["total_change_percent"] = percent_change(
        result.total_change, result.old_stored_total
    )
    result["matched_change_percent"] = percent_change(
        result.matched_change, result.old_stored_total
    )
    result["latest_added_coverage"] = (
        result.latest_on_old_blank + result.latest_unregistered
    )
    result["coverage_share_of_latest_percent"] = percent_change(
        result.latest_added_coverage, result.latest_total
    )
    for part in PARTS:
        result[f"{part}_percentage_points"] = percent_change(
            result[part], result.old_stored_total
        )
    # Test conservation even for groups without an old observation; using zero
    # here only defines a sum of stored records, not the unknown old score.
    difference_from_stored_sum = result.latest_total - result.old_stored_total.fillna(0)
    np.testing.assert_allclose(
        difference_from_stored_sum, result[PARTS].sum(axis=1), rtol=1e-11, atol=1e-5
    )
    return result.reset_index()


def make_dashboard(totals, target):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    impacts = list(totals.impact.unique())
    scenarios = sorted(totals.scenario.unique())
    fig = make_subplots(rows=2, cols=3, subplot_titles=impacts, vertical_spacing=0.18)
    series = [
        ("old_stored_total", "Old stored results", "#63758b"),
        ("latest_on_matches", "Latest: same populated cells", "#42a5a0"),
        ("latest_total", "Latest: all variables", "#e88b44"),
    ]
    for scenario_index, scenario in enumerate(scenarios):
        for j, impact in enumerate(impacts):
            frame = totals[
                (totals.scenario == scenario) & (totals.impact == impact)
            ].sort_values("year")
            for column, label, color in series:
                fig.add_trace(
                    go.Bar(
                        x=frame.year.astype(str),
                        y=frame[column],
                        name=label,
                        marker_color=color,
                        legendgroup=column,
                        showlegend=j == 0,
                        visible=scenario_index == 0,
                        hovertemplate=f"{label}<br>%{{x}}: %{{y:,.5g}} {frame.unit.iloc[0]}<extra></extra>",
                    ),
                    row=j // 3 + 1,
                    col=j % 3 + 1,
                )
            fig.update_yaxes(
                title_text=frame.unit.iloc[0], row=j // 3 + 1, col=j % 3 + 1
            )
    per_scenario = len(impacts) * len(series)
    buttons = [
        dict(
            label=scenario,
            method="update",
            args=[
                {"visible": [i // per_scenario == j for i in range(len(fig.data))]},
                {"title.text": f"Old Toby results vs latest — {scenario}"},
            ],
        )
        for j, scenario in enumerate(scenarios)
    ]
    fig.update_layout(
        title=f"Old Toby results vs latest — {scenarios[0]}",
        template="plotly_white",
        barmode="group",
        height=820,
        margin=dict(t=150, b=140, l=90, r=35),
        legend=dict(orientation="h", x=0, y=-0.15),
        updatemenus=[dict(buttons=buttons, x=0, y=1.2, xanchor="left")],
    )
    fig.add_annotation(
        text="Annual final-energy impacts. Shared years only; old blanks remain missing.<br>"
        "Latest: same populated cells excludes old blank cells and unregistered variables.<br>"
        "Totals include signed contributions across CH, EU wo CH and RoW. Old software/inventory versions are not identified by the download.",
        x=0,
        y=-0.25,
        xref="paper",
        yref="paper",
        showarrow=False,
        xanchor="left",
        align="left",
    )
    fig.write_html(target, include_plotlyjs=True, full_html=True)


def compare(batch, archive, output):
    output.mkdir(parents=True, exist_ok=True)
    provenance = json.loads((archive / "sources.json").read_text())
    for name, entry in provenance["files"].items():
        if sha256(archive / name) != entry["sha256"]:
            raise ValueError(f"Changed downloaded file: {name}")
    mapping_path = batch / "toby_export" / "parameter_mapping.csv"
    mapping = pd.read_csv(mapping_path)
    if (
        mapping.duplicated(["variable", "impact", "location", "unit"]).any()
        or mapping.path.duplicated().any()
    ):
        raise ValueError("Ambiguous parameter mapping")
    registry_path = batch / "toby_reference" / "scenarios.csv"
    if sha256(registry_path) != provenance["scenario_registry_sha256"]:
        raise ValueError("Changed scenario registry")
    registry = pd.read_csv(registry_path).set_index("scn_code").scn_id.to_dict()
    inputs = json.loads((batch / "inputs.json").read_text())
    export_summary = json.loads(
        (batch / "toby_export" / "export_summary.json").read_text()
    )
    frames, audits = [], []
    for entry in inputs["packages"]:
        scenario = entry["scenario"]
        report_path = archive / f"old_{scenario}.csv"
        raw_path = archive / f"old_{scenario}_raw.csv"
        for path in (report_path, raw_path):
            if path.name not in provenance["files"]:
                raise ValueError(f"Missing download provenance: {path}")
        old, old_years, raw_records = read_old(
            report_path, raw_path, mapping, int(registry[scenario])
        )
        latest_path = batch / "toby_export" / "prem1_by_scenario" / f"{scenario}.csv"
        relative = str(latest_path.relative_to(batch / "toby_export"))
        if sha256(latest_path) != export_summary["files_sha256"][relative]:
            raise ValueError(f"Changed validated export: {latest_path}")
        latest = pd.read_csv(
            latest_path, encoding="latin-1", float_precision="round_trip"
        )
        if (
            set(latest.region) != {"CH"}
            or set(latest.model) != {"remind"}
            or not latest.scenario.str.endswith(" - " + scenario).all()
        ):
            raise ValueError("Unexpected latest model/scenario/region")
        latest_years = sorted(latest.year.unique().tolist())
        years = sorted(set(old_years) & set(latest_years))
        if not years:
            raise ValueError(f"No shared years: {scenario}")
        subset = latest[latest.year.isin(years)]
        if len(subset) != len(mapping) * len(years):
            raise ValueError("Latest export is not dense across all mapped series")
        cells = compare_cells(subset, old[old.year.isin(years)], mapping, scenario)
        frames.append(cells)
        audits.append(
            {
                "scenario": scenario,
                "scn_id": int(registry[scenario]),
                "shared_years": years,
                "old_only_years": sorted(set(old_years) - set(latest_years)),
                "latest_only_years": sorted(set(latest_years) - set(old_years)),
                "old_raw_records": raw_records,
                "stored_cells": int(cells.old_value.notna().sum()),
                "old_explicit_zero_cells": int(cells.old_value.eq(0).sum()),
                "old_blank_cells": int(cells.old_status.eq("blank_registered").sum()),
                "unregistered_cells": int(cells.old_status.eq("unregistered").sum()),
                "report_raw_exact_match": True,
                "latest_sha256": sha256(latest_path),
            }
        )
    cells = pd.concat(frames, ignore_index=True)
    totals = aggregate(cells)
    expected = pd.read_csv(batch / "indicator_totals.csv", float_precision="round_trip")
    expected["scenario"] = expected.scenario.str.split(" - ").str[-1]
    expected = expected.rename(columns={"impact_category": "impact"})
    joined = totals.merge(expected[KEYS + ["value"]], on=KEYS, validate="one_to_one")
    if len(joined) != len(totals):
        raise ValueError("Missing independent batch total")
    np.testing.assert_allclose(joined.latest_total, joined.value, rtol=1e-12, atol=1e-6)
    sectors = aggregate(cells, ["sector"])
    locations = aggregate(cells, ["location"])
    variables = aggregate(cells, ["sector", "variable"])
    variables["difference_from_old_stored_sum"] = (
        variables.latest_total - variables.old_stored_total.fillna(0)
    )
    variables["absolute_difference"] = variables.difference_from_old_stored_sum.abs()
    drivers = (
        variables.sort_values("absolute_difference", ascending=False)
        .groupby(KEYS)
        .head(10)
    )
    summaries = (
        totals.groupby(["year", "impact", "unit"])
        .agg(
            scenarios=("scenario", "nunique"),
            full_median_percent=("total_change_percent", "median"),
            full_min_percent=("total_change_percent", "min"),
            full_max_percent=("total_change_percent", "max"),
            matched_median_percent=("matched_change_percent", "median"),
            matched_min_percent=("matched_change_percent", "min"),
            matched_max_percent=("matched_change_percent", "max"),
            median_coverage_share_of_latest_percent=(
                "coverage_share_of_latest_percent",
                "median",
            ),
        )
        .reset_index()
    )
    baselines = totals[totals.scenario.str.endswith("_bas0")]
    cells.to_parquet(output / "cell_comparison.parquet", index=False)
    cells.to_csv(output / "cell_comparison.csv.gz", index=False, float_format="%.16e")
    tables = {
        "indicator_summary": summaries,
        "baseline_scenarios": baselines,
        "scenario_totals": totals,
        "sector_totals": sectors,
        "location_totals": locations,
        "top_variable_differences": drivers,
        "variable_totals": variables.drop(columns="absolute_difference"),
    }
    for name, table in tables.items():
        table.to_csv(output / f"{name}.csv", index=False, float_format="%.16e")
    notes = [
        "Annual scenario-weighted final-energy impacts, not per-unit impacts.",
        "Scenarios are joined by Toby scenario code; IDs, parameter paths and units are checked exactly.",
        "Only shared years are compared. No interpolation or extrapolation of old values.",
        "old_stored_total sums observed Toby records; blanks are unknown, not stored zeros.",
        "latest_on_matches sums latest values on exactly the populated old cells.",
        "matched_change = latest_on_matches minus old_stored_total.",
        "latest_on_old_blank sums latest values for registered series with no stored old value.",
        "latest_unregistered sums latest values for variables absent from the old registry.",
        "total_change = matched_change + latest_on_old_blank + latest_unregistered.",
        "Where a group has no old observations, its old total and percentage changes stay blank.",
        "All change percentages divide by the absolute old stored total; zero denominators stay blank.",
        "Signed scores are preserved; percentage changes can exceed 100% when a total crosses zero.",
        "Coverage contributions are arithmetic components, not estimates of previously missing old impacts.",
        "EU wo CH retains REMIND EUR + NEU excluding Switzerland, not political EU membership.",
        "Old report does not identify exact inventory/software/CF versions; matches establish labels and units, not identical calculation provenance.",
        "Underlying causes cannot be isolated from totals alone: STEM demand and inventories both changed.",
        "Latest data: ecoinvent 3.12 cutoff, September 2026 STEM inputs, verified Pathways batch.",
        "Old reports are checked cell-for-cell against independently downloaded raw database records.",
        "No data uploaded to Toby.",
    ]
    with pd.ExcelWriter(output / "old_vs_latest.xlsx", engine="xlsxwriter") as writer:
        pd.DataFrame({"Reading the comparison": notes}).to_excel(
            writer, sheet_name="Read me", index=False
        )
        writer.sheets["Read me"].set_column(0, 0, 125)
        for name, table in tables.items():
            table.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.freeze_panes(1, min(4, len(table.columns)))
            sheet.autofilter(0, 0, len(table), len(table.columns) - 1)
            sheet.set_column(0, len(table.columns) - 1, 22)
    make_dashboard(totals, output / "old_vs_latest.html")
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": len(audits),
        "indicator_totals_compared": len(totals),
        "cells_compared": len(cells),
        "audits": audits,
        "registered_variables": int(
            mapping.loc[mapping.registered, "variable"].nunique()
        ),
        "unregistered_variables": int(
            mapping.loc[~mapping.registered, "variable"].nunique()
        ),
        "source_manifest_sha256": sha256(archive / "sources.json"),
        "parameter_mapping_sha256": sha256(mapping_path),
        "batch_inputs_sha256": sha256(batch / "inputs.json"),
        "batch_totals_sha256": sha256(batch / "indicator_totals.csv"),
        "comparison_script_sha256": sha256(Path(__file__)),
        "download_script_sha256": sha256(
            Path(__file__).with_name("download_toby_results.py")
        ),
        "all_reports_equal_raw_records": True,
        "latest_totals_equal_validated_batch": True,
        "difference_decomposition_valid": True,
        "uploaded": False,
        "notes": notes,
    }
    (output / "comparison_validation.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    (output / "README.txt").write_text(
        "OLD TOBY RESULTS VS LATEST PATHWAYS RESULTS\n\n"
        + "\n".join(notes)
        + "\n\nAll-scenario summary (percent changes):\n"
        + summaries.to_string(index=False)
        + "\n"
    )
    print(summaries.to_string(index=False))
    print(
        f"Validated {len(totals)} totals and {len(cells)} cells across {len(audits)} scenarios; output: {output}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or args.batch / "toby_comparison"
    compare(args.batch, args.archive or output / "old_downloads", output)


if __name__ == "__main__":
    main()
