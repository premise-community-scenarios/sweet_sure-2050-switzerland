"""Format STEM exports, retaining electricity-only waste as separate pathways.

Use --archive to read a new export directly without replacing the raw workbooks.
Without arguments, read dev/STEM raw data and write scenario_data/scenario_data.csv.
"""

import argparse
import copy
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd

pd.set_option("future.no_silent_downcasting", True)
ROOT = Path(__file__).resolve().parents[1]

scenario_names_map = {
    "SPS1": "SPS1_bas0",
    "SPS2": "SPS2_bas0",
    "SPS3": "SPS3_bas0",
    "SPS4": "SPS4_bas0",
    "SPS1_FIN_Low": "SPS1_fin1",
    "SPS1_FIN_Mid": "SPS1_fin2",
    "SPS1_FIN_High": "SPS1_fin3",
    "SPS2_FIN_Low": "SPS2_fin1",
    "SPS2_FIN_Mid": "SPS2_fin2",
    "SPS2_FIN_High": "SPS2_fin3",
    "SPS3_FIN_Low": "SPS3_fin1",
    "SPS3_FIN_Mid": "SPS3_fin2",
    "SPS3_FIN_High": "SPS3_fin3",
    "SPS4_FIN_Low": "SPS4_fin1",
    "SPS4_FIN_Mid": "SPS4_fin2",
    "SPS4_FIN_High": "SPS4_fin3",
    "SPS1_SOC_Low": "SPS1_soc1",
    "SPS1_SOC_Mid": "SPS1_soc2",
    "SPS1_SOC_High": "SPS1_soc3",
    "SPS2_SOC_Low": "SPS2_soc1",
    "SPS2_SOC_Mid": "SPS2_soc2",
    "SPS2_SOC_High": "SPS2_soc3",
    "SPS3_SOC_Low": "SPS3_soc1",
    "SPS3_SOC_Mid": "SPS3_soc2",
    "SPS3_SOC_High": "SPS3_soc3",
    "SPS4_SOC_Low": "SPS4_soc1",
    "SPS4_SOC_Mid": "SPS4_soc2",
    "SPS4_SOC_High": "SPS4_soc3",
    "SPS1_NUC_Low": "SPS1_nuc1",
    "SPS1_NUC_Mid": "SPS1_nuc2",
    "SPS1_NUC_High": "SPS1_nuc3",
    "SPS2_NUC_Low": "SPS2_nuc1",
    "SPS2_NUC_Mid": "SPS2_nuc2",
    "SPS2_NUC_High": "SPS2_nuc3",
    "SPS3_NUC_Low": "SPS3_nuc1",
    "SPS3_NUC_Mid": "SPS3_nuc2",
    "SPS3_NUC_High": "SPS3_nuc3",
    "SPS4_NUC_Low": "SPS4_nuc1",
    "SPS4_NUC_Mid": "SPS4_nuc2",
    "SPS4_NUC_High": "SPS4_nuc3",
}


def format_frame(df, scenario_name):
    # enforce column names as in original script
    df.columns = [
        "model",
        "scenario",
        "region",
        "variables",
        "unit",
        2020,
        2022,
        2025,
        2030,
        2035,
        2040,
        2050,
    ]

    df["model"] = "STEM"
    df["scenario"] = scenario_name
    # STEM exports include trailing spaces on rail/coach labels. premise
    # matches these strings exactly against config.yaml variable names.
    df["variables"] = df["variables"].str.strip()

    # subtract export from imports
    export = df.loc[df["variables"] == "Exports|Electricity"]
    imports = df.loc[df["variables"] == "Imports|Electricity"]
    diff = imports.loc[:, 2020:].values - export.loc[:, 2020:].values
    # if the result is a negative value, set it to 0
    diff[diff < 0] = 0
    df.loc[df["variables"] == "Imports|Electricity", 2020:] = diff

    # Preserve electricity-only waste production separately from CHP.
    # Each series has its own inventory and physical efficiency in config.yaml.

    # sub-split nuclear generation into pressure water (60%) and boiling water (40%)
    nuclear_pw = copy.deepcopy(
        df.loc[df["variables"] == "Electricity generation|Nuclear Fuel"]
    )
    nuclear_pw["variables"] = "Electricity generation|Nuclear fuel|Pressure water"
    nuclear_pw.loc[:, 2020:] *= 0.6

    nuclear_bw = copy.deepcopy(
        df.loc[df["variables"] == "Electricity generation|Nuclear Fuel"]
    )
    nuclear_bw["variables"] = "Electricity generation|Nuclear fuel|Boiling water"
    nuclear_bw.loc[:, 2020:] *= 0.4

    df = pd.concat([df, nuclear_pw, nuclear_bw], ignore_index=True)

    # replace "-" variants with NaN
    df = df.replace(["-", "- ", " ", "n.a."], np.nan).infer_objects(copy=False)

    # back-fill and forward-fill NaN for "Efficiency*" variables
    mask_eff = df["variables"].str.startswith("Efficiency")
    df.loc[mask_eff, 2020:] = df.loc[mask_eff, 2020:].replace(0, np.nan)
    df.loc[mask_eff, 2020:] = df.loc[mask_eff, 2020:].bfill(axis=1).ffill(axis=1)

    # fill remaining NaN with 0
    df = df.fillna(0)

    # add extra variables
    vars_to_add = [
        "Production|Electricity|Medium to high",
        "Production|Electricity|Low to medium",
    ]

    for var in vars_to_add:
        df = pd.concat(
            [
                df,
                pd.DataFrame(
                    pd.Series(
                        {
                            "model": "STEM",
                            "scenario": scenario_name,
                            "region": "CH",
                            "variables": var,
                            "unit": "TWh",
                            2020: 1,
                            2022: 1,
                            2025: 1,
                            2030: 1,
                            2035: 1,
                            2040: 1,
                            2050: 1,
                        }
                    )
                ).T,
            ]
        )

    return df


def format_exports(folder=None, archive=None):
    try:
        from .stem_wood_coproducts import add_derived_series, source_components
        from . import stem_fuel_cells
    except ImportError:
        from stem_wood_coproducts import add_derived_series, source_components
        import stem_fuel_cells

    if archive is not None:
        with ZipFile(archive) as bundle:
            sources = [
                (Path(n).name, BytesIO(bundle.read(n)))
                for n in sorted(bundle.namelist())
                if n.endswith(".xlsx") and not n.startswith("__MACOSX/")
            ]
    else:
        sources = [(p.name, p) for p in sorted(Path(folder).glob("*.xlsx"))]
    frames = []
    for filename, source in sources:
        name = filename.replace("STEM_to_Premise_", "").replace("_2035.xlsx", "")
        scenario = scenario_names_map[name]
        with pd.ExcelFile(source) as workbook:
            sheets = [s for s in workbook.sheet_names if s.startswith("SPS")]
            if len(sheets) != 1:
                raise ValueError(f"Expected one SPS sheet in {filename}: {sheets}")
            frame = format_frame(
                pd.read_excel(workbook, sheet_name=sheets[0]), scenario
            )
        payload = (
            source.getvalue() if isinstance(source, BytesIO) else source.read_bytes()
        )
        frame = add_derived_series(frame, source_components(payload, frame))
        frames.append(
            stem_fuel_cells.add_derived_series(
                frame, stem_fuel_cells.source_components(payload, frame)
            )
        )
    if not frames:
        raise ValueError("No STEM workbooks found")
    result = pd.concat(frames, ignore_index=True)
    if result.duplicated(["model", "scenario", "region", "variables"]).any():
        raise ValueError("Duplicate scenario variables")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--folder", type=Path, default=ROOT / "dev/STEM raw data")
    source.add_argument("--archive", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "scenario_data/scenario_data.csv"
    )
    args = parser.parse_args()
    result = format_exports(args.folder, args.archive)
    result.to_csv(args.output, index=False)
    print(
        f"Wrote {len(result)} rows across {result.scenario.nunique()} scenarios to {args.output}"
    )


if __name__ == "__main__":
    main()
