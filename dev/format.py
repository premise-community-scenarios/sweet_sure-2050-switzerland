import pandas as pd
import copy
import numpy as np
df = pd.read_excel("STEM_to_Premise_SPS1.xlsx", sheet_name="SPS1")

iam_model = "image"
iam_scenario = "SSP2-RCP26"

# subtract export from imports
export = df.loc[df["Variable"] == "Exports|Electricity"]
imports = df.loc[df["Variable"] == "Imports|Electricity"]
diff = imports.loc[:, 2020:].values - export.loc[:, 2020:].values
# if the result is a negative value, set it to 0
diff[diff < 0] = 0

df.loc[df["Variable"] == "Imports|Electricity", 2020:] = diff

# sub-split nuclear generation into pressure water (60%) and boiling water (40%)
nuclear_pw = copy.deepcopy(df.loc[df["Variable"] == "Electricity generation|Nuclear Fuel"])
nuclear_pw["Variable"] = "Electricity generation|Nuclear fuel|Pressure water"
nuclear_pw.loc[:, 2020:] *= 0.6
nuclear_bw = copy.deepcopy(df.loc[df["Variable"] == "Electricity generation|Nuclear Fuel"])
nuclear_bw["Variable"] = "Electricity generation|Nuclear fuel|Boiling water"
nuclear_bw.loc[:, 2020:] *= 0.4
df = pd.concat([df, nuclear_pw, nuclear_bw], ignore_index=True)

# change column "Model" to "model"
df = df.rename(columns={"Model": "model"})
df["model"] = iam_model
df["Pathway"] = iam_scenario

# convert all non-numeric column names to lower case
df.columns = [c if isinstance(c, int) else c.lower() for c in df.columns]

# move column "pathway" from last to second position
cols = list(df.columns)
cols.insert(1, cols.pop())
df = df[cols]

# replace "-" with 0
df = df.replace("-", np.nan)
df = df.replace("- ", np.nan)
df = df.replace(" ", np.nan)
df = df.replace("n.a.", np.nan)

# back-fill and forward-fill nan values
# for all rows in columns "variables"
# starting with "Efficiency"
df.loc[df["variable"].str.contains("Efficiency"), 2020:] = df.loc[df["variable"].str.contains("Efficiency"), 2020:].replace(0, np.nan)
df.loc[df["variable"].str.contains("Efficiency"), 2020:] = df.loc[df["variable"].str.contains("Efficiency"), 2020:].bfill(axis=1).ffill(axis=1)

# fill empty cells with 0
df = df.fillna(0)

# rename "variable" to "variables"
df = df.rename(columns={"variable": "variables"})

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
                "model": iam_model,
                "pathway": iam_scenario,
                "scenario": "SPS1",
                "variables": var,
                "region": "CH",
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