"""Read validated results and aggregate them without changing their units or signs."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "results/indicators_ei312_20261001"
INDICATORS = {
    "climate change": ("Climate change", "kg CO2-eq.", 1e9, "Mt CO₂-eq."),
    "land occupation": ("Land occupation", "square meter-year", 1e6, "km²·year"),
    "net use of fresh water": ("Freshwater use", "m3", 1e6, "million m³"),
    "human health": ("Human health", "DALY", 1e3, "thousand DALY"),
    "ecosystem quality": (
        "Ecosystem quality",
        "species-year lost",
        1,
        "species·year lost",
    ),
    "minerals depletion": ("Minerals depletion", "kg Sb-eq.", 1e3, "t Sb-eq."),
}
SECTORS = ["industry", "residential", "services", "transport"]
LOCATIONS = ["CH", "EU wo CH", "RoW"]


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def scenario_label(value):
    return value.rsplit(" - ", 1)[-1]


def variable_label(value):
    text = value.removeprefix("EXT - 0 - FE_")
    words = text.replace("_FE_", "_").replace("_", " ").split()
    names = {"CHP": "CHP", "DH": "district heat", "CCS": "CCS", "non": "non"}
    return (
        " ".join(names.get(word, word) for word in words)
        .capitalize()
        .replace("chp", "CHP")
        .replace("ccs", "CCS")
    )


def percent_change(values, baseline):
    """Signed change divided by the absolute baseline; zero has no defined ratio."""
    return np.divide(
        100 * (values - baseline),
        np.abs(baseline),
        out=np.full_like(np.asarray(values, dtype=float), np.nan),
        where=np.asarray(baseline) != 0,
    )


@dataclass
class ResultStore:
    frame: pd.DataFrame
    directory: Path

    @classmethod
    def load(cls, directory=DEFAULT_RESULTS):
        directory = Path(directory).resolve()
        summary = json.loads((directory / "completion_summary.json").read_text())
        export = json.loads((directory / "toby_export/export_summary.json").read_text())
        source = directory / "toby_export/premise_prem1_all_scenarios.csv"
        expected = export["files_sha256"][source.name]
        if digest(source) != expected:
            raise ValueError("Result checksum differs from the validated export")
        if not export["csv_roundtrip_exact"] or export["preview"]:
            raise ValueError("A complete, validated export is required")
        frame = pd.read_csv(source, float_precision="round_trip").rename(
            columns={"impact": "impact_category"}
        )
        keys = [c for c in frame if c != "value"]
        if frame.duplicated(keys).any() or not np.isfinite(frame.value).all():
            raise ValueError("Duplicated or nonfinite result coordinates")
        if (
            frame.scenario.nunique() != summary["scenarios"]
            or sorted(frame.year.unique()) != summary["years"]
        ):
            raise ValueError("Incomplete scenario/year coverage")
        if set(frame.impact_category) != set(INDICATORS):
            raise ValueError("Unexpected impact categories")
        for indicator, (_, unit, _, _) in INDICATORS.items():
            if set(frame.loc[frame.impact_category.eq(indicator), "unit"]) != {unit}:
                raise ValueError(f"Unexpected unit for {indicator}")
        if set(frame.region) != {"CH"} or frame.model.nunique() != 1:
            raise ValueError("This explorer expects one model and Swiss annual demands")
        # Dense, round-trip-verified exports include inactive uses as explicit zeros.
        expected_rows = (
            frame.scenario.nunique()
            * frame.year.nunique()
            * frame.variable.nunique()
            * 6
            * 3
        )
        if len(frame) != expected_rows or set(frame.location) != set(LOCATIONS):
            raise ValueError("Incomplete dense result grid")
        for column in frame.select_dtypes("object"):
            frame[column] = frame[column].astype("category")
        return cls(frame, directory)

    @property
    def scenarios(self):
        return sorted(self.frame.scenario.unique(), key=scenario_label)

    @property
    def years(self):
        return sorted(int(y) for y in self.frame.year.unique())

    def variables(self, sectors):
        return sorted(
            self.frame.loc[self.frame.sector.isin(sectors or []), "variable"].unique()
        )

    def select(self, indicator, scenarios, sectors, locations, variables=None):
        frame = self.frame
        mask = (
            frame.impact_category.eq(indicator)
            & frame.scenario.isin(scenarios or [])
            & frame.sector.isin(sectors or [])
            & frame.location.isin(locations or [])
        )
        if variables:
            mask &= frame.variable.isin(variables)
        return frame.loc[mask]


def aggregate_series(frame, mode="compare", breakdown="sector", focus=None, top_n=10):
    if frame.empty:
        return pd.DataFrame(columns=["series", "year", "value"])
    if mode == "compare":
        result = (
            frame.groupby(["scenario", "year"], observed=True).value.sum().reset_index()
        )
        return result.rename(columns={"scenario": "series"}).assign(
            series=lambda d: d.series.astype(str)
        )
    frame = frame.loc[frame.scenario.eq(focus)]
    if frame.empty:
        return pd.DataFrame(columns=["series", "year", "value"])
    result = frame.groupby([breakdown, "year"], observed=True).value.sum().reset_index()
    result = result.rename(columns={breakdown: "series"})
    result["series"] = result.series.astype(str)
    if breakdown == "variable" and result.series.nunique() > top_n:
        largest = (
            result.assign(magnitude=result.value.abs())
            .groupby("series")
            .magnitude.sum()
            .nlargest(top_n)
            .index
        )
        result.loc[~result.series.isin(largest), "series"] = "Other uses"
        result = result.groupby(["series", "year"], as_index=False).value.sum()
    return result


def prepare_chart(series, indicator, years, scale):
    """Keep native values for CSV and display-scaled values for plotting."""
    result = series.copy()
    result["native_unit"] = INDICATORS[indicator][1]
    result["display_value"] = result.value / INDICATORS[indicator][2]
    result["display_unit"] = INDICATORS[indicator][3]
    result["plot_value"] = result.display_value
    result["plot_unit"] = INDICATORS[indicator][3]
    undefined = []
    if scale == "change" and not result.empty:
        baseline = result.loc[result.year.eq(2020)].set_index("series").value
        result["baseline"] = result.series.map(baseline)
        result["plot_value"] = percent_change(
            result.value.to_numpy(), result.baseline.to_numpy()
        )
        result["plot_unit"] = "% change from 2020"
        undefined = (
            result.loc[result.baseline.eq(0) | result.baseline.isna(), "series"]
            .unique()
            .tolist()
        )
    result = result.loc[result.year.between(years[0], years[1])].sort_values(
        ["series", "year"]
    )
    return result, undefined
