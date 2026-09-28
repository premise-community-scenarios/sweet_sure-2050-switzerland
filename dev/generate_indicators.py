#!/usr/bin/env python3
"""Calculate SWEET-SURE indicators from premise ZIP packages using Pathways.

Run with the pathways conda environment; see dev/INDICATORS.md. No notebook
execution or Brightway database import is needed: the package contains the LCI matrices.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import sys
import time
import warnings

import numpy as np
import pandas as pd

DEFAULT_YEARS = [2020, 2025, 2030, 2035, 2040, 2050]
METHODS = {
    "EN15804+A2 - Core impact categories and indicators - climate change: total (EF v3.0 - IPCC 2013) - global warming potential (GWP100)": (
        "climate change",
        "kg CO2-eq.",
    ),
    "EN15804+A2 - Core impact categories and indicators - material resources: metals/minerals - abiotic depletion potential (ADP): elements (ultimate reserves)": (
        "minerals depletion",
        "kg Sb-eq.",
    ),
    "Inventory results and indicators - resources - land occupation": (
        "land occupation",
        "square meter-year",
    ),
    "EN15804+A2 - Indicators describing resource use - net use of fresh water - FW": (
        "net use of fresh water",
        "m3",
    ),
    "ReCiPe 2016 v1.03, endpoint (H) - total: human health - human health": (
        "human health",
        "DALY",
    ),
    "ReCiPe 2016 v1.03, endpoint (H) - total: ecosystem quality - ecosystem quality": (
        "ecosystem quality",
        "species-year lost",
    ),
}
GROUP_COLUMNS = [
    "sector",
    "variable",
    "year",
    "region",
    "model",
    "scenario",
    "impact_category",
    "location",
]
TOTAL_COLUMNS = ["model", "scenario", "region", "year", "impact_category"]
TRANSPORT_MODES = (
    "bus_",
    "cars_",
    "coach_",
    "freight_rail_",
    "heavy_duty_truck_",
    "light_duty_truck_",
    "motorcycle_",
    "other_transport_",
    "passenger_rail_",
)


def sector_for_variable(variable: str) -> str:
    """Retain all final-energy variables, including those added since the notebook."""
    prefix = "EXT - 0 - FE_"
    if variable.startswith(prefix):
        suffix = variable[len(prefix) :]
        for sector in ("industry", "residential", "services"):
            if suffix.startswith(sector + "_"):
                return sector
        if suffix.startswith(TRANSPORT_MODES) or suffix in ("tram", "trolleybus"):
            return "transport"
    raise ValueError(f"No sector mapping for {variable!r}")


def aggregate_results(raw: pd.DataFrame, europe: set[str]) -> pd.DataFrame:
    """Apply the notebook's sector/geography grouping without dropping impacts."""
    required = set(GROUP_COLUMNS) - {"sector"}
    required.add("value")
    if missing := required - set(raw.columns):
        raise ValueError(f"Missing result columns: {sorted(missing)}")
    if raw[list(required)].isna().any().any():
        raise ValueError("Missing result coordinates or values")
    if not np.isfinite(raw["value"]).all():
        raise ValueError("Non-finite LCIA results")
    if unknown := set(raw["impact_category"]) - METHODS.keys():
        raise ValueError(f"Unknown LCIA methods: {sorted(unknown)}")
    df = raw.loc[raw["value"].ne(0)].copy()
    df["sector"] = df["variable"].map(sector_for_variable)
    # Keep the notebook's EUR + NEU grouping (this is broader than EU membership).
    df["location"] = np.where(
        df["location"].eq("CH"),
        "CH",
        np.where(df["location"].isin(europe), "EU wo CH", "RoW"),
    )
    grouped = df.groupby(GROUP_COLUMNS, as_index=False, sort=True)["value"].sum()
    before = df.groupby(TOTAL_COLUMNS)["value"].sum().sort_index()
    after = grouped.groupby(TOTAL_COLUMNS)["value"].sum().sort_index()
    if not before.index.equals(after.index) or not np.allclose(
        before.values, after.values, rtol=1e-10, atol=1e-8
    ):
        raise ValueError("Indicator totals changed during aggregation")
    grouped["unit"] = grouped["impact_category"].map(lambda m: METHODS[m][1])
    grouped["impact_category"] = grouped["impact_category"].map(lambda m: METHODS[m][0])
    return grouped


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def __getattr__(self, name):
        return getattr(self.streams[0], name)


class WarningLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def check_inputs(p, years: list[int], variables: list[str]) -> list[dict]:
    """Check every selected variable's supplier and unit conversion before LCA."""
    from pathways.utils import (
        fetch_indices,
        read_indices_csv,
        get_unit_conversion_factors,
    )
    from pathways.utils import harmonize_units
    from premise.geomap import Geomap

    if len(p.scenarios.model) != 1 or len(p.scenarios.pathway) != 1:
        raise ValueError(
            "Pass one model/pathway per ZIP, as produced by create_data_packages.py"
        )
    if "CH" not in p.scenarios.region.values:
        raise ValueError("CH is missing from scenario data")
    if missing := set(years) - set(p.scenarios.year.values.tolist()):
        raise ValueError(f"Years missing from package: {sorted(missing)}")
    for variable in variables:
        sector_for_variable(variable)
    scenario = harmonize_units(p.scenarios.copy(deep=True), variables)
    demand = scenario.sel(region="CH", variables=variables, year=years)
    if not np.isfinite(demand.values).all() or (demand.values < 0).any():
        raise ValueError("Non-finite or negative final-energy demand")
    geo = Geomap(str(p.scenarios.model.values[0]))
    records = []
    for year in years:
        paths = [
            Path(f)
            for f in p.filepaths
            if Path(f).name == "A_matrix_index.csv" and Path(f).parent.name == str(year)
        ]
        if len(paths) != 1:
            raise ValueError(f"Expected one activity index for {year}, got {paths}")
        indices = read_indices_csv(paths[0])
        # All activity categories are summed for these indicators. Record absent
        # labels as undefined, as Pathways does, without printing tens of
        # thousands of individual classification notices. No activity is removed.
        unclassified = {
            activity[:2]
            for activity in indices
            if activity[:2] not in p.classifications
        }
        for activity in sorted(unclassified):
            p.classifications[activity] = "undefined"
            p.reverse_classifications["undefined"].append(activity)
        resolved = fetch_indices(p.mapping, ["CH"], variables, indices, geo)["CH"]
        if missing := set(variables) - resolved.keys():
            raise ValueError(
                f"Unlinked final-energy variables in {year}: {sorted(missing)}"
            )
        reverse = {index: activity for activity, index in indices.items()}
        for variable, info in resolved.items():
            activity = reverse[info["idx"]]
            unit = scenario.attrs["units"][variable]
            factor = get_unit_conversion_factors(unit, activity[2], p.units).astype(
                float
            )
            if not np.isfinite(factor).all() or np.any(factor <= 0):
                raise ValueError(f"Invalid demand conversion for {variable}: {factor}")
            records.append(
                {
                    "year": year,
                    "variable": variable,
                    "sector": sector_for_variable(variable),
                    "name": activity[0],
                    "reference_product": activity[1],
                    "unit": activity[2],
                    "location": activity[3],
                    "scenario_unit": unit,
                    "conversion_factor": float(factor),
                    "scenario_demand": float(
                        demand.sel(variables=variable, year=year).item()
                    ),
                }
            )
    return records


def validate_result_coverage(
    raw: pd.DataFrame, demands: list[dict], years: list[int]
) -> dict:
    if not np.isfinite(raw["value"]).all():
        raise ValueError("Non-finite LCIA results")
    expected = {(year, method) for year in years for method in METHODS}
    actual = set(raw[["year", "impact_category"]].itertuples(index=False, name=None))
    if missing := expected - actual:
        raise ValueError(f"Missing year/indicator results: {sorted(missing)}")
    active = {
        (row["year"], row["variable"]) for row in demands if row["scenario_demand"] > 0
    }
    present = set(
        raw.loc[raw["value"].ne(0), ["year", "variable"]].itertuples(
            index=False, name=None
        )
    )
    if missing := active - present:
        raise ValueError(
            f"Positive demands without any impact results: {sorted(missing)}"
        )
    return {
        "supplier_checks": len(demands),
        "unlinked_variables": 0,
        "active_variable_years": len(active),
        "year_indicator_combinations": len(expected),
        "nonfinite_results": 0,
        "aggregation_preserves_totals": True,
    }


def export_tables(df: pd.DataFrame, output: Path, stem: str, html: bool) -> None:
    df.to_parquet(output / f"{stem}_indicators.parquet", index=False)
    pivot = df.pivot_table(
        index=[column for column in GROUP_COLUMNS if column != "year"] + ["unit"],
        columns="year",
        values="value",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    pivot.to_excel(output / f"{stem}_indicators.xlsx", index=False)
    if html:
        from pivottablejs import pivot_ui

        pivot_ui(df, outfile_path=str(output / f"pivottable_{stem}.html"))


def run(args, output: Path) -> None:
    # Import only after installing Pathways' supported directory overrides. Its
    # constructor clears the cache; keep this separate from other Pathways runs.
    from pathways import Pathways, __version__ as pathways_version
    from pathways.lcia import get_lcia_method_names
    from pathways.filesystem_constants import DATA_DIR
    from premise.geomap import Geomap
    import pathways
    from indicator_coverage import prepare_package, export_impact_omissions

    unavailable = set(METHODS) - set(get_lcia_method_names(args.ecoinvent_version))
    if unavailable:
        raise ValueError(
            f"Unavailable ecoinvent {args.ecoinvent_version} LCIA methods: {unavailable}"
        )
    geo = Geomap("remind")
    europe = set(geo.iam_to_ecoinvent_location("EUR")) | set(
        geo.iam_to_ecoinvent_location("NEU")
    )
    europe = (europe | {"EUR", "NEU"}) - {"CH"}
    cf_file = DATA_DIR / f"lcia_ei{args.ecoinvent_version.replace('.', '')}.json"
    versions = {
        name: metadata.version(name)
        for name in ("numpy", "pandas", "scipy", "bw2calc", "bw2data", "premise")
    }
    versions["pathways"] = ".".join(map(str, pathways_version))
    frames = []
    for package in args.packages:
        start = time.monotonic()
        working_package, coverage = prepare_package(
            package,
            args.config,
            Path(__file__).with_name("SWEET-SURE - indicators generation.ipynb"),
            output,
        )
        stem = "results_" + package.stem
        manifest_path = output / f"{stem}.manifest.json"
        raw_path = output / f"{stem}.gzip"
        signature = {
            "package_sha256": sha256(package),
            "ecoinvent_version": args.ecoinvent_version,
            "years": args.years,
            "methods": list(METHODS),
            "region": "CH",
            "variable_prefix": "EXT - 0 - FE",
            "use_distributions": 0,
            "aggregate_by": ["act_category"],
            "cf_sha256": sha256(cf_file),
            "working_package_sha256": coverage["working_sha256"],
        }
        previous = (
            json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        )
        if (
            args.resume
            and previous.get("calculation") == signature
            and raw_path.exists()
        ):
            if previous.get("raw_sha256") != sha256(raw_path):
                raise ValueError(f"Result checksum mismatch: {raw_path}")
            print(f"Reusing verified calculations: {raw_path}")
            manifest = previous
            demands = pd.read_csv(output / f"{stem}_demands.csv").to_dict("records")
        else:
            if raw_path.exists() or (
                manifest_path.exists()
                and not (args.resume and previous.get("calculation") == signature)
            ):
                raise FileExistsError(
                    f"Existing results in {output}; use --resume for matching inputs or another output directory"
                )
            build_path = package.with_suffix(".build.json")
            build = json.loads(build_path.read_text()) if build_path.exists() else {}
            if (
                build.get("source_version", args.ecoinvent_version)
                != args.ecoinvent_version
            ):
                raise ValueError(
                    "Requested ecoinvent version differs from package build manifest"
                )
            if (
                build.get("package_sha256", signature["package_sha256"])
                != signature["package_sha256"]
            ):
                raise ValueError("Package checksum differs from its build manifest")
            manifest = {
                "status": "running",
                "package": str(package),
                "calculation": signature,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "software": versions,
                "pathways_source": str(Path(pathways.__file__).parent),
                "pathways_source_sha256": {
                    name: sha256(Path(pathways.__file__).parent / name)
                    for name in ("lca.py", "pathways.py", "utils.py")
                },
                "script_sha256": sha256(Path(__file__)),
                "package_build": build,
                "coverage": coverage,
                "europe_locations": sorted(europe),
                "methods": METHODS,
                "multiprocessing": args.multiprocessing,
                "notes": [
                    "Scenario-weighted annual final-energy impacts, not per-unit intensities.",
                    "Activity categories are summed before caching; locations are retained.",
                    "EU wo CH follows REMIND EUR + NEU, not political EU membership.",
                ],
            }
            write_json(manifest_path, manifest)
            try:
                print(f"Loading {package.name}", flush=True)
                p = Pathways(
                    str(working_package),
                    debug=False,
                    ecoinvent_version=args.ecoinvent_version,
                )
                variables = [
                    str(v)
                    for v in p.scenarios.variables.values
                    if str(v).startswith("EXT - 0 - FE")
                ]
                if len(variables) != coverage["final_energy_variables"]:
                    raise ValueError(
                        "Pathways omitted final-energy variables while loading the package"
                    )
                selected = p.scenarios.sel(variables=variables)
                if (selected < -1e-12).any():
                    raise ValueError(
                        "Negative final-energy demand exceeds roundoff tolerance"
                    )
                manifest["clipped_negative_roundoff_values"] = int((selected < 0).sum())
                p.scenarios.loc[dict(variables=variables)] = selected.clip(min=0)
                demands = check_inputs(p, args.years, variables)
                manifest["undefined_activity_classifications"] = len(
                    p.reverse_classifications["undefined"]
                )
                pd.DataFrame(demands).to_csv(
                    output / f"{stem}_demands.csv", index=False
                )
                manifest["variables"] = variables
                write_json(manifest_path, manifest)
                print(
                    f"Calculating {len(variables)} variables, {len(METHODS)} indicators, {len(args.years)} years",
                    flush=True,
                )
                p.calculate(
                    methods=list(METHODS),
                    regions=["CH"],
                    scenarios=p.scenarios.pathway.values.tolist(),
                    variables=variables,
                    years=args.years,
                    multiprocessing=args.multiprocessing,
                    use_distributions=0,
                    aggregate_by=["act_category"],
                )
                if not np.isfinite(p.lca_results.values).all():
                    raise ValueError("Non-finite Pathways result array")
                p.export_results(filename=str(raw_path.with_suffix("")))
                manifest["raw_sha256"] = sha256(raw_path)
                manifest["status"] = "calculated"
                write_json(manifest_path, manifest)
            except Exception as error:
                manifest.update(
                    status="failed", error=f"{type(error).__name__}: {error}"
                )
                write_json(manifest_path, manifest)
                raise
        raw = pd.read_parquet(raw_path)
        validation = validate_result_coverage(raw, demands, args.years)
        df = aggregate_results(raw, europe)
        export_tables(df, output, stem, not args.no_html)
        export_impact_omissions(raw, coverage, output, stem)
        manifest.update(
            status="complete",
            validation=validation,
            raw_rows=len(raw),
            aggregated_rows=len(df),
            elapsed_seconds=round(time.monotonic() - start, 2),
        )
        write_json(manifest_path, manifest)
        frames.append(df)
        print(f"Completed {package.name}: {len(df)} indicator rows", flush=True)
    combined = pd.concat(frames, ignore_index=True)
    combined.to_excel(output / "df_final.xlsx", index=False)
    combined.to_parquet(output / "df_final.parquet", index=False)
    totals = combined.groupby(TOTAL_COLUMNS + ["unit"], as_index=False)["value"].sum()
    totals.to_csv(output / "indicator_totals.csv", index=False)
    if not args.no_html:
        from pivottablejs import pivot_ui

        pivot_ui(combined, outfile_path=str(output / "pivottable_all_scenarios.html"))
    print(f"Outputs: {output}", flush=True)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "packages", nargs="+", type=Path, help="Exported premise ZIP package(s)"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("results/indicators"))
    parser.add_argument("--years", nargs="+", type=int, default=DEFAULT_YEARS)
    parser.add_argument(
        "--ecoinvent-version", choices=["3.10", "3.11", "3.12"], default="3.12"
    )
    parser.add_argument(
        "--multiprocessing",
        action="store_true",
        help="Parallelize years; can require substantial RAM",
    )
    parser.add_argument(
        "--no-html", action="store_true", help="Skip interactive pivot HTML exports"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse checksum-verified raw results from matching calculations",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "configuration_file/config.yaml",
        help="Exact build configuration, used to verify any missing mapping repair",
    )
    args = parser.parse_args(argv)
    args.packages = [p.expanduser().resolve(strict=True) for p in args.packages]
    args.config = args.config.expanduser().resolve(strict=True)
    if len({p.stem for p in args.packages}) != len(args.packages):
        parser.error("Package names must be unique within a run")
    args.years = sorted(set(args.years))
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / ".runtime"
    runtime.mkdir(exist_ok=True)
    # JSON is valid YAML. Pathways reads this supported configuration at import.
    write_json(
        runtime / "variables.yaml",
        {
            "USER_DATA_BASE_DIR": str(runtime / "data"),
            "USER_LOGS_DIR": str(runtime / "logs"),
            "STATS_DIR": str(runtime / "stats"),
        },
    )
    warning_log = WarningLog()
    logging.basicConfig(
        filename=output / "pathways.log", filemode="w", level=logging.INFO
    )
    logging.getLogger().addHandler(warning_log)
    original_dir = Path.cwd()
    os.chdir(runtime)
    caught = []
    try:
        with (output / "run.log").open("w") as log, redirect_stdout(
            Tee(sys.stdout, log)
        ), redirect_stderr(Tee(sys.stderr, log)):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                run(args, output)
    finally:
        os.chdir(original_dir)
        messages = [
            {"category": w.category.__name__, "message": str(w.message)} for w in caught
        ]
        write_json(
            output / "warnings.json",
            {"python_warnings": messages, "logging_warnings": warning_log.messages},
        )
        logging.getLogger().removeHandler(warning_log)
        if messages or warning_log.messages:
            print(
                f"Recorded {len(messages)} Python warnings and {len(warning_log.messages)} logging warnings in {output / 'warnings.json'}"
            )


if __name__ == "__main__":
    main()
