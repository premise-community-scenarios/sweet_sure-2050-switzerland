import pandas as pd
import copy
import numpy as np

final_df = pd.DataFrame()

for scenario in [
    "SPS1",
    "SPS2",
    "SPS3",
    "SPS4",
]:

    df = pd.read_excel(f"STEM_to_Premise_{scenario}.xlsx", sheet_name="SPS1")
    df.columns = ["model", "scenario", "region", "variables", "unit", 2020, 2022, 2025, 2030, 2040, 2050]

    df["model"] = "STEM"
    df["scenario"] = scenario

    # subtract export from imports
    export = df.loc[df["variables"] == "Exports|Electricity"]
    imports = df.loc[df["variables"] == "Imports|Electricity"]
    diff = imports.loc[:, 2020:].values - export.loc[:, 2020:].values
    # if the result is a negative value, set it to 0
    diff[diff < 0] = 0

    df.loc[df["variables"] == "Imports|Electricity", 2020:] = diff

    # add row with variable == "Electricity generation|Wastes|Renewable|Wastes Incineration (electric only)"
    # to row with variable == "Electricity generation|Wastes|Renewable|CHP Wastes (for District Heating)"
    # and remove the former

    wastes_incineration = df.loc[df["variables"] == "Electricity generation|Wastes|Renewable|Wastes Incineration (electric only)"]
    chp_wastes = df.loc[df["variables"] == "Electricity generation|Wastes|Renewable|CHP Wastes (for District Heating)"]
    chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
    df = df.drop(wastes_incineration.index)

    # same with "Electricity generation|Wastes|Renewable|Wastes Incineration (electric only) CCS"
    # and "Electricity generation|Wastes|Renewable|CHP Wastes (for District Heating) CCS"
    wastes_incineration = df.loc[df["variables"] == "Electricity generation|Wastes|Renewable|Wastes Incineration (electric only) CCS"]
    chp_wastes = df.loc[df["variables"] == "Electricity generation|Wastes|Renewable|CHP Wastes (for District Heating) CCS"]
    chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
    df = df.drop(wastes_incineration.index)

    # sane with "Electricity generation|Wastes|Non Renewable|Wastes Incineration (electric only)"
    # and "Electricity generation|Wastes|Non Renewable|CHP Wastes (for District Heating)"
    wastes_incineration = df.loc[df["variables"] == "Electricity generation|Wastes|Non Renewable|Wastes Incineration (electric only)"]
    chp_wastes = df.loc[df["variables"] == "Electricity generation|Wastes|Non Renewable|CHP Wastes (for District Heating)"]
    chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
    df = df.drop(wastes_incineration.index)

    # and same with "Electricity generation|Wastes|Non Renewable|Wastes Incineration (electric only) CCS"
    # and "Electricity generation|Wastes|Non Renewable|CHP Wastes (for District Heating) CCS"
    wastes_incineration = df.loc[df["variables"] == "Electricity generation|Wastes|Non Renewable|Wastes Incineration (electric only) CCS"]
    chp_wastes = df.loc[df["variables"] == "Electricity generation|Wastes|Non Renewable|CHP Wastes (for District Heating) CCS"]
    chp_wastes.loc[:, 2020:] += wastes_incineration.loc[:, 2020:]
    df = df.drop(wastes_incineration.index)

    # sub-split nuclear generation into pressure water (60%) and boiling water (40%)
    nuclear_pw = copy.deepcopy(df.loc[df["variables"] == "Electricity generation|Nuclear Fuel"])
    nuclear_pw["variables"] = "Electricity generation|Nuclear fuel|Pressure water"
    nuclear_pw.loc[:, 2020:] *= 0.6
    nuclear_bw = copy.deepcopy(df.loc[df["variables"] == "Electricity generation|Nuclear Fuel"])
    nuclear_bw["variables"] = "Electricity generation|Nuclear fuel|Boiling water"
    nuclear_bw.loc[:, 2020:] *= 0.4
    df = pd.concat([df, nuclear_pw, nuclear_bw], ignore_index=True)

    # replace "-" with 0
    df = df.replace("-", np.nan)
    df = df.replace("- ", np.nan)
    df = df.replace(" ", np.nan)
    df = df.replace("n.a.", np.nan)


    # back-fill and forward-fill nan values
    # for all rows in columns "variables"
    # starting with "Efficiency"
    df.loc[df["variables"].str.startswith("Efficiency"), 2020:] = df.loc[df["variables"].str.startswith("Efficiency"), 2020:].replace(0, np.nan)
    df.loc[df["variables"].str.startswith("Efficiency"), 2020:] = df.loc[df["variables"].str.startswith("Efficiency"), 2020:].bfill(axis=1).ffill(axis=1)

    # fill empty cells with 0
    df = df.fillna(0)

    # we want to add the following variables:
    vars =[
        'Production|Electricity|Medium to high',
        'Production|Electricity|Low to medium',
    ]

    # create new rows for each variable
    for var in vars:
        df = pd.concat(
            [
                df,
                pd.DataFrame(pd.Series({
                    "model": "STEM",
                    "scenario": scenario,
                    "region": "CH",
                    "variables": var,
                    "unit": "TWh",
                    2020: 1,
                    2022: 1,
                    2025: 1,
                    2030: 1,
                    2040: 1,
                    2050: 1,
                })).T
            ],
        )

    # add to final df
    final_df = pd.concat([final_df, df])


# save to csv
final_df.to_csv(f"../scenario_data/scenario_data.csv", index=False, sep=",")