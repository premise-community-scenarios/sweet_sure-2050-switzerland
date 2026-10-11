"""Recover industrial gas fuel-cell fuel/efficiency from saved STEM rows.

The export's fuel formulas point to a fixed zero cell. Read the labelled
ITOT-CHPGFC flows from the same external source/year as its electricity export.
Keep all original variables and add explicitly named derived series.
"""

from io import BytesIO
import re

import numpy as np
import pandas as pd
from openpyxl import load_workbook

try:
    from .stem_source_cache import external_caches, raw_series
except ImportError:
    from stem_source_cache import external_caches, raw_series

TECHNOLOGY = "Industrial CHPs fuel cells (gas reformer)"
ELECTRICITY = f"Electricity generation|Gas|{TECHNOLOGY}"
HEAT = f"Heat generation from CHPs and heat plants|On-site CHPs|Gas|{TECHNOLOGY}"
FUEL_E = f"Fuel input for electricity generation|Gas|{TECHNOLOGY}"
FUEL_H = f"Fuel input for heat generation from CHPs and heat plants|On-site CHPs|Gas|{TECHNOLOGY}"
EFFICIENCY = f"Efficiency of power plants (electric)|Gas|{TECHNOLOGY}"
RECOVERED = "|Recovered from STEM"


def source_components(payload, frame):
    workbook = load_workbook(BytesIO(payload), read_only=True, data_only=False)
    try:
        (sheet,) = [s for s in workbook.sheetnames if s.startswith("SPS")]
        rows = list(workbook[sheet].iter_rows())
        columns = {
            int(c.value): i for i, c in enumerate(rows[0]) if str(c.value).isdigit()
        }
        (row,) = [
            r for r in rows[1:] if len(r) > 3 and str(r[3].value).strip() == ELECTRICITY
        ]
        caches = external_caches(payload)
        result = {key: [] for key in ("electricity", "heat", "fuel")}
        for year in [c for c in frame.columns if str(c).isdigit()]:
            formula = row[columns[int(year)]].value
            links = set(re.findall(r"'\[(\d+)\]Electricity and CHPs'!", formula or ""))
            if len(links) != 1:
                raise ValueError(
                    f"Cannot establish fuel-cell source for {year}: {formula}"
                )
            cache = caches[int(links.pop())]
            for key, kind, commodity in (
                ("electricity", "VAR_fout", "ITOT_NKEV_GAS_ELC_CHP"),
                ("heat", "VAR_fout", "ITOT_LTH_CHP"),
                ("fuel", "VAR_fin", "INGA"),
            ):
                result[key].append(
                    raw_series(cache, "ITOT-CHPGFC", kind, commodity)[int(year)][0]
                )
        return {key: np.asarray(values, dtype=float) for key, values in result.items()}
    finally:
        workbook.close()


def recovered_values(electricity, heat, fuel):
    electricity, heat, fuel = np.broadcast_arrays(electricity, heat, fuel)
    if any(
        not np.isfinite(v).all() or (v < 0).any() for v in (electricity, heat, fuel)
    ):
        raise ValueError("Invalid cached fuel-cell energy values")
    useful = electricity + heat
    if ((useful > 0) & (fuel == 0)).any():
        raise ValueError("Fuel-cell output without cached fuel input")
    if ((fuel > 0) & (useful == 0)).any():
        raise ValueError("Cannot allocate fuel-cell fuel without useful output")
    allocated_e = np.divide(
        fuel * electricity,
        useful,
        out=np.zeros_like(fuel, dtype=float),
        where=useful > 0,
    )
    efficiency = np.divide(
        electricity, fuel, out=np.zeros_like(fuel, dtype=float), where=fuel > 0
    )
    return {
        FUEL_E + RECOVERED: allocated_e,
        FUEL_H + RECOVERED: fuel - allocated_e,
        EFFICIENCY + RECOVERED: efficiency,
    }


def add_derived_series(frame, components):
    years = [c for c in frame.columns if str(c).isdigit()]
    data = frame.set_index("variables")
    for variable, key in ((ELECTRICITY, "electricity"), (HEAT, "heat")):
        if data.loc[variable, "unit"] != "PJ/yr.":
            raise ValueError(f"Unexpected fuel-cell output unit: {variable}")
        if not np.allclose(
            data.loc[variable, years].to_numpy(float),
            components[key],
            rtol=1e-10,
            atol=1e-12,
        ):
            raise ValueError(f"Fuel-cell source does not match exported {key}")
    derived = recovered_values(**components)
    records = []
    for variable, values in derived.items():
        if variable in data.index:
            raise ValueError(f"Derived variable already exists: {variable}")
        record = data.loc[ELECTRICITY].to_dict()
        record.update(
            variables=variable,
            unit="%" if variable.startswith("Efficiency") else "PJ/yr.",
        )
        record.update(zip(years, values))
        records.append(record)
    return pd.concat([frame, pd.DataFrame(records)], ignore_index=True)


def validate_recovered_series(frame):
    """Check source-year balances; interpolation is handled by Premise later."""
    years = [c for c in frame.columns if str(c).isdigit()]
    checks = []
    for (scenario, region), group in frame.groupby(["scenario", "region"]):
        data = group.set_index("variables")
        if ELECTRICITY not in data.index:
            continue
        get = lambda v: data.loc[v, years].to_numpy(float)
        fuel = get(FUEL_E + RECOVERED) + get(FUEL_H + RECOVERED)
        expected = recovered_values(get(ELECTRICITY), get(HEAT), fuel)
        for variable, values in expected.items():
            if not np.allclose(get(variable), values, rtol=1e-10, atol=1e-12):
                raise ValueError(
                    f"Inconsistent recovered fuel-cell series: {scenario}, {variable}"
                )
        checks.append(
            {
                "scenario": scenario,
                "region": region,
                "source_years_checked": len(years),
                "active_years": int((get(ELECTRICITY) > 0).sum()),
            }
        )
    return checks
