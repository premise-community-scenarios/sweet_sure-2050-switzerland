import os
import glob
import copy

import numpy as np
import pandas as pd
pd.set_option("future.no_silent_downcasting", True)

final_df = pd.DataFrame()

folder = "STEM raw data"

scenario_names_map = {
    "SPS1":"SPS1_bas0",
    "SPS2":"SPS2_bas0",
    "SPS3":"SPS3_bas0",
    "SPS4":"SPS4_bas0",
    "SPS1_FIN_Low":"SPS1_fin1",
    "SPS1_FIN_Mid":"SPS1_fin2",
    "SPS1_FIN_High":"SPS1_fin3",
    "SPS2_FIN_Low":"SPS2_fin1",
    "SPS2_FIN_Mid":"SPS2_fin2",
    "SPS2_FIN_High":"SPS2_fin3",
    "SPS3_FIN_Low":"SPS3_fin1",
    "SPS3_FIN_Mid":"SPS3_fin2",
    "SPS3_FIN_High":"SPS3_fin3",
    "SPS4_FIN_Low":"SPS4_fin1",
    "SPS4_FIN_Mid":"SPS4_fin2",
    "SPS4_FIN_High":"SPS4_fin3",
    "SPS1_SOC_Low":"SPS1_soc1",
    "SPS1_SOC_Mid":"SPS1_soc2",
    "SPS1_SOC_High":"SPS1_soc3",
    "SPS2_SOC_Low":"SPS2_soc1",
    "SPS2_SOC_Mid":"SPS2_soc2",
    "SPS2_SOC_High":"SPS2_soc3",
    "SPS3_SOC_Low":"SPS3_soc1",
    "SPS3_SOC_Mid":"SPS3_soc2",
    "SPS3_SOC_High":"SPS3_soc3",
    "SPS4_SOC_Low":"SPS4_soc1",
    "SPS4_SOC_Mid":"SPS4_soc2",
    "SPS4_SOC_High":"SPS4_soc3",
    "SPS1_NUC_Low":"SPS1_nuc1",
    "SPS1_NUC_Mid":"SPS1_nuc2",
    "SPS1_NUC_High":"SPS1_nuc3",
    "SPS2_NUC_Low":"SPS2_nuc1",
    "SPS2_NUC_Mid":"SPS2_nuc2",
    "SPS2_NUC_High":"SPS2_nuc3",
    "SPS3_NUC_Low":"SPS3_nuc1",
    "SPS3_NUC_Mid":"SPS3_nuc2",
    "SPS3_NUC_High":"SPS3_nuc3",
    "SPS4_NUC_Low":"SPS4_nuc1",
    "SPS4_NUC_Mid":"SPS4_nuc2",
    "SPS4_NUC_High":"SPS4_nuc3",
}

# loop over all Excel files in the folder
for filepath in sorted(glob.glob(os.path.join(folder, "*.xlsx"))):
    print(filepath)
    filename = os.path.basename(filepath)

    # scenario name from file name:
    # remove "STEM_to_Premise_" and "_2035.xlsx"
    scenario_name = filename
    scenario_name = scenario_name.replace("STEM_to_Premise_", "").replace("_2035.xlsx", "")
    scenario_name = scenario_names_map[scenario_name]

    # open workbook and find sheets starting with "SPS"
    xls = pd.ExcelFile(filepath)
    sps_sheets = [s for s in xls.sheet_names if s.startswith("SPS")]

    for sheet in sps_sheets:
        # read only the SPS* sheet
        df = pd.read_excel(xls, sheet_name=sheet)

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

        df["variables"] = df["variables"].str.strip()
        df["model"] = "STEM"
        df["scenario"] = scenario_name

        # subtract export from imports
        export = df.loc[df["variables"] == "Exports|Electricity"]
        imports = df.loc[df["variables"] == "Imports|Electricity"]
        diff = imports.loc[:, 2020:].values - export.loc[:, 2020:].values
        # if the result is a negative value, set it to 0
        diff[diff < 0] = 0
        df.loc[df["variables"] == "Imports|Electricity", 2020:] = diff

        # Wastes incineration → CHP wastes (renewable)
        wastes_incineration = df.loc[
            df["variables"] == "Electricity generation|Wastes|Renewable|Wastes Incineration (electric only)"
        ]
        chp_wastes = df.loc[
            df["variables"] == "Electricity generation|Wastes|Renewable|CHP Wastes (for District Heating)"
        ]
        chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
        df = df.drop(wastes_incineration.index)

        # Wastes incineration CCS → CHP wastes CCS (renewable)
        wastes_incineration = df.loc[
            df["variables"]
            == "Electricity generation|Wastes|Renewable|Wastes Incineration (electric only) CCS"
        ]
        chp_wastes = df.loc[
            df["variables"]
            == "Electricity generation|Wastes|Renewable|CHP Wastes (for District Heating) CCS"
        ]
        chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
        df = df.drop(wastes_incineration.index)

        # Wastes incineration → CHP wastes (non-renewable)
        wastes_incineration = df.loc[
            df["variables"] == "Electricity generation|Wastes|Non Renewable|Wastes Incineration (electric only)"
        ]
        chp_wastes = df.loc[
            df["variables"]
            == "Electricity generation|Wastes|Non Renewable|CHP Wastes (for District Heating)"
        ]
        chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
        df = df.drop(wastes_incineration.index)

        # Wastes incineration CCS → CHP wastes CCS (non-renewable)
        wastes_incineration = df.loc[
            df["variables"]
            == "Electricity generation|Wastes|Non Renewable|Wastes Incineration (electric only) CCS"
        ]
        chp_wastes = df.loc[
            df["variables"]
            == "Electricity generation|Wastes|Non Renewable|CHP Wastes (for District Heating) CCS"
        ]
        chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
        df = df.drop(wastes_incineration.index)

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
        df.loc[mask_eff, 2020:] = (
            df.loc[mask_eff, 2020:].bfill(axis=1).ffill(axis=1)
        )

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

        # add to final df
        final_df = pd.concat([final_df, df], ignore_index=True)

# save to csv
final_df.to_csv("../scenario_data/scenario_data.csv", index=False, sep=",")
