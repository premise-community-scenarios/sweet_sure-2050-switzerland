from __future__ import annotations

from pathlib import Path
from textwrap import shorten

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.colors import to_hex


RESULTS_FILE = Path("results_edges_geopolrisk_paired_2024_all_scenarios_aggregated.gzip")
OUTPUT_FILE = Path("geopolrisk_scenarios_variants_stacked.png")
OUTPUT_WORKBOOK = Path("geopolrisk_scenarios_variants_stacked.xlsx")
CORRECTED_RESULTS_FILE = Path("results_edges_geopolrisk_paired_2024_all_scenarios_aggregated_corrected.gzip")

EDGE_METHOD_LABEL = "GeoPolRisk paired 2024"
SCORE_UNIT = "kg Copper-eq."
YEARS = [2020, 2025, 2030, 2035, 2040, 2050]
STACK_TOP_N = 6
OTHER_LABEL = "Other"
RAW_OTHER_TRANSPORT_ELECTRIC = "EXT - 0 - FE_other_transport_electric"
RAW_RAIL_TRANSPORT_ELECTRICITY = "EXT - 0 - FE_rail_transport_electricity"
OTHER_TRANSPORT_SCALE = 0.2
SCENARIO_ORDER = ["SPS1", "SPS2", "SPS3", "SPS4"]
VARIANT_ORDER = [
    "bas0",
    "fin3",
    "nuc3",
    "soc3",
]

VARIABLE_SUFFIXES = [
    "waste_non_renew_ccs",
    "waste_non_renew",
    "light_fuel_oil",
    "natural_gas",
    "other_electric",
    "electric_boiler",
    "heat_pump_gas",
    "heat_pump",
    "fuel_cell",
    "gas_ccs",
    "wood_ccs",
    "gasoline",
    "electricity",
    "electric",
    "hydrogen",
    "diesel",
    "biogas",
    "pellet",
    "solar",
    "wood",
    "coal",
    "gas",
    "oil",
    "dh",
]

PART_REPLACEMENTS = {
    "cars": "Cars",
    "bus": "Bus",
    "coach": "Coach",
    "heavy_duty_truck": "Heavy-duty truck",
    "light_duty_truck": "Light-duty truck",
    "motorcycle": "Motorcycle",
    "other_transport": "Other transport",
    "rail_transport": "Rail transport",
    "tram": "Tram",
    "trolleybus": "Trolleybus",
    "industry_process": "Industry process",
    "industry_heat": "Industry heat",
    "industry_heat_chp": "Industry heat CHP",
    "residential_space_heating": "Residential space heating",
    "residential_water_heating": "Residential water heating",
    "residential": "Residential",
    "services": "Services",
    "services_chp": "Services CHP",
    "natural_gas": "Natural gas",
    "light_fuel_oil": "Light fuel oil",
    "waste_non_renew": "Waste non-renewable",
    "waste_non_renew_ccs": "Waste non-renewable CCS",
    "other_electric": "Other electric",
    "electric_boiler": "Electric boiler",
    "heat_pump": "Heat pump",
    "heat_pump_gas": "Heat pump gas",
    "fuel_cell": "Fuel cell",
    "gas_ccs": "Gas CCS",
    "wood_ccs": "Wood CCS",
    "electricity": "Electricity",
    "electric": "Electricity",
    "diesel": "Diesel",
    "gasoline": "Gasoline",
    "hydrogen": "Hydrogen",
    "gas": "Gas",
    "oil": "Oil",
    "wood": "Wood",
    "pellet": "Pellet",
    "solar": "Solar",
    "biogas": "Biogas",
    "coal": "Coal",
    "dh": "DH",
    "chp": "CHP",
    "ccs": "CCS",
}

VARIANT_STYLES = {
    "bas0": {"color": "#111111", "marker": "o", "xlsx_marker": "circle", "label": "bas0"},
    "fin3": {"color": "#d95f02", "marker": "^", "xlsx_marker": "triangle", "label": "fin3"},
    "nuc3": {"color": "#1f78b4", "marker": "^", "xlsx_marker": "triangle", "label": "nuc3"},
    "soc3": {"color": "#33a02c", "marker": "^", "xlsx_marker": "triangle", "label": "soc3"},
}


def format_phrase(text: str, *, lowercase: bool = False) -> str:
    words: list[str] = []
    for raw_word in text.split():
        mapped = PART_REPLACEMENTS.get(raw_word.lower(), raw_word.replace("_", " "))
        for word in mapped.split():
            if word in {"CHP", "CCS", "DH", "H2"}:
                words.append(word)
            elif lowercase:
                words.append(word.lower())
            else:
                words.append(word.capitalize())
    return " ".join(words)


def clean_variable_label(label: str, max_len: int = 32) -> str:
    cleaned = str(label)
    for prefix in ("EXT - 0 - FE_", "EXT - 0 - FE", "EXT - 0 - "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    base = cleaned.lower().replace("_fe_", "_").strip("_")

    consumer = base
    vector = ""
    for suffix in VARIABLE_SUFFIXES:
        if base.endswith("_" + suffix):
            consumer = base[: -(len(suffix) + 1)]
            vector = suffix
            break

    consumer_label = format_phrase(consumer.replace("_", " "))
    if vector:
        vector_label = format_phrase(vector.replace("_", " "), lowercase=True)
        return shorten(f"{consumer_label} / {vector_label}", width=max_len, placeholder="...")
    return shorten(consumer_label, width=max_len, placeholder="...")


def split_scenario_label(label: str) -> tuple[str, str]:
    parts = str(label).split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return str(label), ""


def prepare_reference_stacked_data(df: pd.DataFrame, top_labels: list[str]) -> pd.DataFrame:
    plot_df = df[["year", "variable", "value"]].copy()
    plot_df["variable"] = plot_df["variable"].fillna("Unknown").astype(str)
    plot_df["variable"] = plot_df["variable"].where(
        plot_df["variable"].isin(set(top_labels)),
        OTHER_LABEL,
    )
    stacked = (
        plot_df.groupby(["year", "variable"], dropna=False)["value"]
        .sum()
        .unstack(fill_value=0)
        .reindex(index=YEARS, fill_value=0)
    )
    ordered_columns = list(top_labels) + [OTHER_LABEL]
    return stacked.reindex(columns=ordered_columns, fill_value=0)


def make_unique_labels(labels: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    unique_labels: list[str] = []
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
        if counts[label] == 1:
            unique_labels.append(label)
        else:
            unique_labels.append(f"{label} ({counts[label]})")
    return unique_labels


def build_display_label_map(top_labels: list[str]) -> dict[str, str]:
    cleaned = [clean_variable_label(label) for label in top_labels]
    unique = make_unique_labels(cleaned)
    mapping = {original: display for original, display in zip(top_labels, unique)}
    mapping[OTHER_LABEL] = OTHER_LABEL
    return mapping


def load_results_frame() -> pd.DataFrame:
    df = pd.read_parquet(RESULTS_FILE, engine="pyarrow")
    if df.empty:
        raise ValueError(f"No data in {RESULTS_FILE}")

    df = df[df["value"].notna() & (df["value"] != 0)].copy()
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df = df[df["year"].isin(YEARS)].copy()
    correction_mask = df["variable"].astype(str) == RAW_OTHER_TRANSPORT_ELECTRIC
    if correction_mask.any():
        df.loc[correction_mask, "value"] = df.loc[correction_mask, "value"] * OTHER_TRANSPORT_SCALE
        df.loc[correction_mask, "variable"] = RAW_RAIL_TRANSPORT_ELECTRICITY
    scenario_variant = df["scenario_label"].map(split_scenario_label)
    df["scenario_group"] = scenario_variant.map(lambda item: item[0])
    df["variant"] = scenario_variant.map(lambda item: item[1])
    return df


def get_stack_metadata(df: pd.DataFrame) -> tuple[list[str], dict[str, str], dict[str, str]]:
    reference_df = df[df["variant"] == "bas0"].copy()
    top_labels = (
        reference_df.groupby("variable")["value"]
        .sum()
        .sort_values(ascending=False)
        .head(STACK_TOP_N)
        .index.tolist()
    )
    display_labels = build_display_label_map(top_labels)
    ordered_stack_labels = list(top_labels) + [OTHER_LABEL]
    bar_palette = list(plt.cm.Set2.colors) + list(plt.cm.Paired.colors)
    bar_colors = {
        label: ("#9e9e9e" if label == OTHER_LABEL else to_hex(bar_palette[idx % len(bar_palette)]))
        for idx, label in enumerate(ordered_stack_labels)
    }
    return top_labels, display_labels, bar_colors


def build_scenario_plot_data(
    scenario_df: pd.DataFrame,
    top_labels: list[str],
    display_labels: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    reference_group_df = scenario_df[scenario_df["variant"] == "bas0"].copy()
    stacked = prepare_reference_stacked_data(reference_group_df, top_labels=top_labels)
    display_stacked = stacked.copy()
    display_stacked.columns = [display_labels[label] for label in stacked.columns]

    variant_totals = (
        scenario_df.groupby(["variant", "year"], dropna=False)["value"]
        .sum()
        .unstack(fill_value=0)
        .reindex(index=VARIANT_ORDER, columns=YEARS, fill_value=0)
    )
    return stacked, display_stacked, variant_totals


def export_excel_workbook(
    df: pd.DataFrame,
    top_labels: list[str],
    display_labels: dict[str, str],
    bar_colors: dict[str, str],
) -> Path:
    ordered_stack_labels = list(top_labels) + [OTHER_LABEL]

    with pd.ExcelWriter(OUTPUT_WORKBOOK, engine="xlsxwriter") as writer:
        workbook = writer.book
        summary_df = pd.DataFrame(
            [
                {"field": "method", "value": EDGE_METHOD_LABEL},
                {"field": "unit", "value": SCORE_UNIT},
                {"field": "source_file", "value": RESULTS_FILE.name},
                {
                    "field": "data_correction",
                    "value": (
                        "Scaled Other transport / electricity to 20% "
                        "and relabeled it as Rail transport / electricity"
                    ),
                },
                {"field": "scenarios", "value": ", ".join(SCENARIO_ORDER)},
                {"field": "variants", "value": ", ".join(VARIANT_ORDER)},
                {"field": "stack_reference", "value": "bas0 end-user decomposition"},
            ]
        )
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        summary_ws = writer.sheets["Summary"]
        summary_ws.set_column(0, 0, 18)
        summary_ws.set_column(1, 1, 42)

        for scenario_group in SCENARIO_ORDER:
            scenario_df = df[df["scenario_group"] == scenario_group].copy()
            stacked, display_stacked, variant_totals = build_scenario_plot_data(
                scenario_df,
                top_labels=top_labels,
                display_labels=display_labels,
            )

            export_df = pd.DataFrame({"Year": YEARS})
            for raw_label, display_label in zip(stacked.columns, display_stacked.columns):
                export_df[display_label] = stacked[raw_label].tolist()
            for variant in VARIANT_ORDER:
                export_df[variant] = variant_totals.loc[variant].tolist()

            sheet_name = scenario_group
            export_df.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            worksheet.freeze_panes(1, 1)
            worksheet.set_column(0, 0, 10)
            worksheet.set_column(1, len(display_stacked.columns), 20)
            worksheet.set_column(len(display_stacked.columns) + 1, len(export_df.columns) - 1, 12)

            column_chart = workbook.add_chart({"type": "column", "subtype": "stacked"})
            nrows, _ = export_df.shape

            for col_idx, raw_label in enumerate(ordered_stack_labels, start=1):
                column_chart.add_series(
                    {
                        "name": [sheet_name, 0, col_idx],
                        "categories": [sheet_name, 1, 0, nrows, 0],
                        "values": [sheet_name, 1, col_idx, nrows, col_idx],
                        "fill": {"color": bar_colors[raw_label]},
                        "border": {"none": True},
                    }
                )

            line_chart = workbook.add_chart({"type": "line"})
            variant_start_col = 1 + len(ordered_stack_labels)
            for offset, variant in enumerate(VARIANT_ORDER):
                style = VARIANT_STYLES[variant]
                col_idx = variant_start_col + offset
                line_chart.add_series(
                    {
                        "name": [sheet_name, 0, col_idx],
                        "categories": [sheet_name, 1, 0, nrows, 0],
                        "values": [sheet_name, 1, col_idx, nrows, col_idx],
                        "line": {"color": style["color"], "width": 1.25},
                        "marker": {
                            "type": style["xlsx_marker"],
                            "size": 7,
                            "border": {"color": style["color"]},
                            "fill": {"color": "#ffffff"},
                        },
                    }
                )

            column_chart.combine(line_chart)
            column_chart.set_title(
                {
                    "name": (
                        f"{scenario_group}: bas0 end-user stacks with "
                        f"{'/'.join(VARIANT_ORDER)} markers"
                    )
                }
            )
            column_chart.set_x_axis({"name": "Year"})
            column_chart.set_y_axis({"name": f"GeoPolRisk [{SCORE_UNIT}]", "num_format": "0.00E+00"})
            column_chart.set_legend({"position": "bottom", "font": {"size": 8}})
            column_chart.set_size({"width": 860, "height": 460})
            worksheet.insert_chart(nrows + 3, 0, column_chart)

    return OUTPUT_WORKBOOK


def main() -> None:
    df = load_results_frame()
    df.to_parquet(CORRECTED_RESULTS_FILE, compression="gzip", index=False)
    top_labels, display_labels, bar_colors = get_stack_metadata(df)
    ordered_stack_labels = list(top_labels) + [OTHER_LABEL]

    plt.style.use("ggplot")
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.titlesize": 13,
            "axes.labelsize": 10,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 8,
            "figure.titlesize": 16,
        }
    )

    fig, axes = plt.subplots(2, 2, figsize=(18, 12), sharex=True)
    axes = axes.flatten()
    x_positions = list(range(len(YEARS)))

    for ax, scenario_group in zip(axes, SCENARIO_ORDER):
        scenario_df = df[df["scenario_group"] == scenario_group].copy()
        stacked, display_stacked, variant_totals = build_scenario_plot_data(
            scenario_df,
            top_labels=top_labels,
            display_labels=display_labels,
        )
        display_stacked.plot(
            kind="bar",
            stacked=True,
            ax=ax,
            color=[bar_colors[label] for label in stacked.columns],
            width=0.72,
            legend=False,
            zorder=1,
        )

        for variant in VARIANT_ORDER:
            style = VARIANT_STYLES[variant]
            ax.plot(
                x_positions,
                variant_totals.loc[variant].tolist(),
                color=style["color"],
                marker=style["marker"],
                markersize=6,
                linewidth=1.1,
                markerfacecolor="white",
                markeredgewidth=1.4,
                zorder=3,
            )

        ax.set_title(scenario_group, fontweight="bold", pad=10)
        ax.set_xlabel("Year")
        ax.set_ylabel(f"GeoPolRisk [{SCORE_UNIT}]")
        ax.set_xticks(x_positions)
        ax.set_xticklabels(YEARS, rotation=0)
        ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))
        ax.grid(axis="y", alpha=0.3)
        ax.set_ylim(bottom=0)

    stack_handles = [
        Patch(
            facecolor=bar_colors[label],
            edgecolor="none",
            label=display_labels[label],
        )
        for label in ordered_stack_labels
    ]
    variant_handles = [
        Line2D(
            [0],
            [0],
            color=VARIANT_STYLES[variant]["color"],
            marker=VARIANT_STYLES[variant]["marker"],
            markerfacecolor="white",
            markeredgewidth=1.4,
            linewidth=1.1,
            label=VARIANT_STYLES[variant]["label"],
        )
        for variant in VARIANT_ORDER
    ]

    fig.legend(
        handles=stack_handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.09),
        ncol=4,
        frameon=False,
        title="End users in stacked bars",
    )
    fig.legend(
        handles=variant_handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.01),
        ncol=5,
        frameon=False,
        title="Variant totals as markers",
    )
    fig.suptitle(
        f"{EDGE_METHOD_LABEL}\nScenario panels with bas0 end-user stacks and bas0/fin3/nuc3/soc3 markers",
        y=0.97,
    )
    fig.subplots_adjust(left=0.06, right=0.98, top=0.88, bottom=0.24, wspace=0.16, hspace=0.26)
    fig.savefig(OUTPUT_FILE, dpi=220, bbox_inches="tight")
    workbook_path = export_excel_workbook(df, top_labels, display_labels, bar_colors)
    print(CORRECTED_RESULTS_FILE)
    print(OUTPUT_FILE)
    print(workbook_path)


if __name__ == "__main__":
    main()
