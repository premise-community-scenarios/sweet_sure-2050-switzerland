import pandas as pd
import copy
import numpy as np
df = pd.read_excel("STEM_to_Premise_SPS1.xlsx", sheet_name="SPS1")
df.columns = ["model", "scenario", "region", "variables", "unit", 2020, 2022, 2025, 2030, 2040, 2050]

# subtract export from imports
export = df.loc[df["variables"] == "Exports|Electricity"]
imports = df.loc[df["variables"] == "Imports|Electricity"]
diff = imports.loc[:, 2020:].values - export.loc[:, 2020:].values
# if the result is a negative value, set it to 0
diff[diff < 0] = 0

df.loc[df["variables"] == "Imports|Electricity", 2020:] = diff

# remove two specific rows
rows_to_remove = [
    "Fuel input for heat generation from CHPs and heat plants|District heating|CHP|Oil|Fossil liquids",
    "Fuel input for heat generation from CHPs and heat plants|District heating|CHP|Oil|Synthetic liquids"
]

df = df[~df["variables"].isin(rows_to_remove)]


labels = [
    ("Electricity generation", "Fuel input for electricity generation"),
    ("Heat generation from CHPs and heat plants|District heating", "Fuel input for heat generation from CHPs and heat plants|District heating"),
]

for label in labels:

    # recalculate efficiencies
    output_labels = df["variables"].str.startswith(label[0])
    input_labels = df["variables"].str.startswith(label[1])

    efficiency_labels = [x.replace(label[0], "Efficiency") for x in list(df.loc[output_labels, "variables"].unique())]

    # Replace zeros in the denominator with NaN
    df.loc[output_labels, 2020:] = df.loc[output_labels, 2020:].replace(0, np.nan)
    df.loc[input_labels, 2020:] = df.loc[input_labels, 2020:].replace(0, np.nan)

    result = 1 / df.loc[input_labels, 2020:].div(df.loc[output_labels, 2020:].values, axis=1)


    # append new rows to the dataframe
    for i, label in enumerate(efficiency_labels):
        df = pd.concat(
            [
                df,
                pd.DataFrame(pd.Series({
                    "model": "STEM",
                    "scenario": "SPS1",
                    "region": "CH",
                    "variables": label,
                    "unit": "%",
                    2020: result.iloc[i, 0],
                    2025: result.iloc[i, 1],
                    2030: result.iloc[i, 3],
                    2040: result.iloc[i, 4],
                    2050: result.iloc[i, 5],
                })).T
            ],
        )


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

print(df.columns)

# create new rows for each variable
for var in vars:
    df = pd.concat(
        [
            df,
            pd.DataFrame(pd.Series({
                "model": "STEM",
                "scenario": "SPS1",
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

# save to csv
df.to_csv("../scenario_data/scenario_data.csv", index=False, sep=",")