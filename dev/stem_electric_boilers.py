"""Allocate dedicated boiler equipment with each STEM scenario's utilization."""

from contextlib import contextmanager
import json
import math

import numpy as np
import pandas as pd

KEY = "electric boiler equipment"


def allocation(capacity_gw, energy_pj, power_kw, years):
    if not all(math.isfinite(v) and v >= 0 for v in (capacity_gw, energy_pj)):
        raise ValueError("Invalid boiler capacity or final electricity")
    if power_kw <= 0 or years <= 0:
        raise ValueError("Invalid boiler power or lifetime")
    if energy_pj == 0:
        return 0.0
    if capacity_gw == 0:
        raise ValueError("Active boiler demand has zero installed capacity")
    return capacity_gw * 1e6 / power_kw / years / (energy_pj * 1e9)


@contextmanager
def swiss_electric_boilers(scenario_data, scenario, audit_path):
    import premise.external as external

    data = pd.read_csv(scenario_data, float_precision="round_trip")
    data = data[data.scenario.eq(scenario) & data.region.eq("CH")].set_index(
        "variables"
    )
    years = sorted(int(c) for c in data.columns if c.isdigit())
    if not data.index.is_unique:
        raise ValueError("Non-unique STEM boiler source rows")

    def value(variable, year, unit):
        if data.loc[variable, "unit"] != unit or not years[0] <= year <= years[-1]:
            raise ValueError(f"Invalid boiler source unit/year: {variable}")
        values = data.loc[variable, [str(y) for y in years]].to_numpy(float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError(f"Invalid boiler source series: {variable}")
        return float(np.interp(year, years, values))

    original = external.check_inventories
    records = []

    def check(*args, **kwargs):
        result = original(*args, **kwargs)
        inventories, database, configuration, _ = result
        seen = set()
        for ds in list(database) + list(inventories):
            if (
                id(ds) in seen
                or ds.get("unit") != "megajoule"
                or ds.get("location") != "CH"
                or KEY not in ds
            ):
                continue
            seen.add(id(ds))
            evidence = json.loads(ds[KEY])
            year = kwargs["year"]
            capacity = value(evidence["capacity_variable"], year, "GWth/yr.")
            energy = value(evidence["energy_variable"], year, "PJ/yr.")
            coefficient = allocation(
                capacity, energy, evidence["power_kW"], evidence["lifetime_years"]
            )
            hardware = [
                e
                for e in ds["exchanges"]
                if e["type"] == "technosphere" and e["name"] == evidence["hardware"][0]
            ]
            if len(hardware) != 1:
                raise ValueError(f"Missing dedicated boiler equipment: {ds['name']}")
            hardware[0]["amount"] = coefficient
            records.append(
                {
                    **evidence,
                    "scenario": scenario,
                    "year": year,
                    "wrapper": ds["name"],
                    "capacity_GW": capacity,
                    "electricity_PJ": energy,
                    "units_per_MJ": coefficient,
                    "capacity unit interpretation": "Installed thermal capacity in GW; legacy CSV label GWth/yr. is retained. Capacity-addition series are excluded.",
                    "purchased electricity equivalent full-load hours": energy
                    * 1e9
                    / (capacity * 1e6 * 3.6)
                    if capacity
                    else None,
                }
            )
        if len([r for r in records if r["year"] == kwargs["year"]]) != 4:
            raise ValueError("Expected four distinct Swiss boiler endpoints per year")
        return result

    external.check_inventories = check
    try:
        yield
    finally:
        external.check_inventories = original
        audit_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
