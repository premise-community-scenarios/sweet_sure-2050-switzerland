"""Separate STEM wood CHP from the energy coproducts of wood conversion.

Original exported variables remain as provenance. Derived quantities come from
labelled cells in the workbook's saved external cache and must reconcile to the
exports. No efficiency floor, cap, extrapolation or scenario exception is used.
"""

from contextlib import contextmanager
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from uuid import uuid4

import numpy as np
import pandas as pd

try:
    from .stem_source_cache import external_caches, raw_series
except ImportError:
    from stem_source_cache import external_caches, raw_series

CHP = "CHP Wood (for District Heating)"
ELECTRICITY = f"Electricity generation|Wood|{CHP}"
HEAT = f"Heat generation from CHPs and heat plants|District heating|CHP|Wood|{CHP}"
FUEL_E = f"Fuel input for electricity generation|Wood|{CHP}"
FUEL_H = f"Fuel input for heat generation from CHPs and heat plants|District heating|CHP|Wood|{CHP}"
EFFICIENCY = f"Efficiency of power plants (electric)|Wood|{CHP}"
CHP_ONLY = "|CHP only"
BIOGAS = "Domestic biogas production|Wood|Gasification"
BIOGAS_E = "Electricity from domestic biogas production|Wood|Gasification"
BIOGAS_H = "Heat from domestic biogas production|Wood|Gasification"
BIODIESEL = "Domestic biodiesel production|Wood|Fischer Tropsch"
BIODIESEL_H = "Heat from domestic biodiesel production|Wood|Fischer Tropsch"
RESIDUAL_BIOGAS = "Domestic biogas production|Excluding wood gasification without CCS"

FT_SOURCE = "diesel production, synthetic, Fischer Tropsch process, hydrogen from wood gasification, energy allocation"
GAS_SOURCE = "biomethane production, high pressure from synthetic gas, wood, fluidised technology"
HEAT_PRODUCT = "heat, district or industrial, other than natural gas"

PROVIDERS = {
    "biodiesel, wood, FT (domestic)": (
        "diesel production, from wood, STEM coproduct allocation",
        "diesel, synthetic",
        "kilogram",
        "FT",
        "fuel",
    ),
    "DH_biodiesel_wood_coproduct": (
        "heat production, from wood-to-biodiesel, STEM coproduct allocation",
        HEAT_PRODUCT,
        "megajoule",
        "FT",
        "heat",
    ),
    "biomethane, wood gasification (domestic)": (
        "biomethane production, from wood gasification, STEM coproduct allocation",
        "biomethane, high pressure",
        "cubic meter",
        "gas",
        "fuel",
    ),
    "electricity, wood biogas coproduct": (
        "electricity production, from wood-to-biogas, STEM coproduct allocation",
        "electricity, high voltage",
        "kilowatt hour",
        "gas",
        "electricity",
    ),
    "DH_biogas_wood_coproduct": (
        "heat production, from wood-to-biogas, STEM coproduct allocation",
        HEAT_PRODUCT,
        "megajoule",
        "gas",
        "heat",
    ),
}
# FT source documentation: 43.3 MJ/kg. Ecoinvent 3.12 biomethane production
# exchange property 'heating value, net': 34.97 MJ/m3 (not the older comment).
LHV = {"FT": 43.3, "gas": 34.97}


def assert_close(left, right, label):
    if not np.allclose(left, right, rtol=1e-10, atol=1e-12):
        raise ValueError(f"STEM wood-coproduct reconciliation failed: {label}")


def source_components(payload, frame):
    """Use the same external-link/year selection as each exported energy cell."""
    from openpyxl import load_workbook
    from io import BytesIO

    workbook = load_workbook(BytesIO(payload), read_only=True, data_only=False)
    (sheet_name,) = [name for name in workbook.sheetnames if name.startswith("SPS")]
    sheet = workbook[sheet_name]
    # Read rows once; repeated read-only random access is expensive.
    rows = {
        str(row[3].value).strip(): row
        for row in sheet.iter_rows(min_row=2)
        if len(row) > 3
    }
    caches = external_caches(payload)
    years = [c for c in frame.columns if isinstance(c, int)]
    results = {
        key: [] for key in ("chp_e", "chp_h", "chp_fuel", "gas_e", "gas_h", "diesel_h")
    }
    for year in years:
        col = 5 + years.index(year)
        formula = rows[ELECTRICITY][col].value
        match = re.search(r"'\[(\d+)\]Electricity and CHPs'!", formula or "")
        if match is None:
            raise ValueError(f"Cannot establish external source for {year}: {formula}")
        cache = caches[int(match[1])]
        for key, technology, kind, commodity in (
            ("chp_e", "CHPWOD", "VAR_fout", "ELC35"),
            ("chp_h", "CHPWOD", "VAR_fout", "LTH"),
            ("chp_fuel", "CHPWOD", "VAR_fin", "EWOD"),
            ("gas_e", "SUP_WODBIO", "VAR_fout", "ELC35"),
            ("gas_h", "SUP_WODBIO", "VAR_fout", "LTH"),
            ("diesel_h", "CT_CWODBDS", "VAR_fout", "LTH"),
        ):
            results[key].append(
                raw_series(
                    cache,
                    technology,
                    kind,
                    commodity,
                    allow_absent_flow=not key.startswith("chp"),
                )[year][0]
            )
    workbook.close()
    return {key: np.asarray(value, dtype=float) for key, value in results.items()}


def add_derived_series(frame, components):
    years = [c for c in frame.columns if isinstance(c, int)]
    data = frame.set_index("variables")
    get = lambda key: data.loc[key, years].to_numpy(float)
    c = components
    for key, values in c.items():
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError(f"Invalid cached energy values: {key}")
    assert_close(get(ELECTRICITY), c["chp_e"] + c["gas_e"], "electricity total")
    assert_close(get(HEAT), c["chp_h"] + c["gas_h"] + c["diesel_h"], "heat total")
    assert_close(get(FUEL_E) + get(FUEL_H), c["chp_fuel"], "CHP fuel")
    assert_close(get(BIOGAS_E), c["gas_e"], "separate biogas electricity")
    assert_close(get(BIODIESEL_H), c["diesel_h"], "separate biodiesel heat")
    residual = get("Domestic biogas production") - get(BIOGAS)
    if (residual < -1e-12).any():
        raise ValueError("Wood biogas exceeds total domestic biogas")
    useful = c["chp_e"] + c["chp_h"]
    if (c["chp_fuel"] <= 0).any() or (useful <= 0).any():
        raise ValueError("Cannot derive CHP-only efficiencies with zero fuel/output")
    allocated_e = c["chp_fuel"] * c["chp_e"] / useful
    derived = {
        ELECTRICITY + CHP_ONLY: c["chp_e"],
        HEAT + CHP_ONLY: c["chp_h"],
        FUEL_E + CHP_ONLY: allocated_e,
        FUEL_H + CHP_ONLY: c["chp_fuel"] - allocated_e,
        EFFICIENCY + CHP_ONLY: c["chp_e"] / c["chp_fuel"],
        BIOGAS_H: c["gas_h"],
        RESIDUAL_BIOGAS: np.maximum(residual, 0),
    }
    records = []
    for variable, values in derived.items():
        if variable in data.index:
            if variable == BIOGAS_H:
                assert_close(get(variable), values, "separate biogas heat")
                continue
            raise ValueError(f"Derived variable already exists: {variable}")
        record = data.loc[ELECTRICITY].to_dict()
        record.update(
            variables=variable,
            unit="%" if variable.startswith("Efficiency") else "PJ/yr.",
        )
        record.update(zip(years, values))
        records.append(record)
    return pd.concat([frame, pd.DataFrame(records)], ignore_index=True)


def allocation(fuel, electricity, heat):
    values = (fuel, electricity, heat)
    if not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError(f"Invalid coproduct outputs: {values}")
    total = sum(values)
    if total and fuel == 0:
        raise ValueError(
            "Energy coproducts without the source inventory's fuel product"
        )
    return fuel / total if total else 1.0


def allocated_provider(source, spec, alpha):
    """Allocate a fuel-inventory burden over useful output energy, exactly once.

    All products use the SAME unmodified source. The weighted source demand is
    (F + E + H) * alpha / LHV = F/LHV. This conserves all upstream technosphere
    and biosphere burdens, including embodied carbon, over the joint outputs.
    """
    name, product, unit, process, output = spec
    per_unit = (
        1
        if output == "fuel"
        else (3.6 if unit == "kilowatt hour" else 1) / LHV[process]
    )
    amount = alpha * per_unit
    return {
        "name": name,
        "reference product": product,
        "unit": unit,
        "location": "CH",
        "code": uuid4().hex,
        "has_downstream_consumer": True,
        "comment": (
            f"SWEET energy-allocation proxy for {process} {output}. Source: {source['name']} "
            f"[{source['location']}]. Fuel energy share={alpha:.15g}; source LHV={LHV[process]} "
            f"MJ/{source['unit']}. Source demand={amount:.15g} {source['unit']} per {unit}. "
            "Fuel and recovered heat/electricity jointly retain the complete original fuel-production burden. "
            "This is an allocated process proxy, not combustion of additional fuel. "
            "Original inventories and their supply chains remain unchanged."
        ),
        "exchanges": [
            {
                "name": name,
                "product": product,
                "unit": unit,
                "location": "CH",
                "amount": 1.0,
                "type": "production",
                "uncertainty type": 0,
            },
            {
                "name": source["name"],
                "product": source["reference product"],
                "unit": source["unit"],
                "location": source["location"],
                "amount": amount,
                "type": "technosphere",
                "uncertainty type": 0,
            },
        ],
    }


def process_basis(source):
    """Make a dedicated Swiss process before applying the output allocations."""
    basis = deepcopy(dict(source))
    basis["exchanges"] = [deepcopy(dict(e)) for e in source["exchanges"]]
    for field in (
        "regions",
        "region mapping",
        "regionalize",
        "regionalized",
        "emptied",
        "adjust efficiency",
        "absolute efficiency",
        "technosphere filters",
        "biosphere filters",
        "excludes technosphere",
        "excludes biosphere",
        "replaces",
        "replaces in",
        "replacement ratio",
        "production volume variable",
        "production volume",
        "log parameters",
        "input",
    ):
        basis.pop(field, None)
    basis.update(
        name=source["name"] + ", STEM coproduct basis", location="CH", code=uuid4().hex
    )
    basis["has_downstream_consumer"] = True
    basis["STEM coproduct basis"] = True
    basis["STEM coproduct needs regionalization"] = source["location"] != "CH"
    for exchange in basis["exchanges"]:
        exchange.pop("input", None)
        if exchange["type"] == "production":
            exchange.update(name=basis["name"], location="CH")
            exchange.pop("production volume", None)
    return basis


class ScenarioValues:
    def __init__(self, path, scenario):
        data = pd.read_csv(path, float_precision="round_trip")
        self.data = data[data.scenario.eq(scenario) & data.region.eq("CH")].set_index(
            "variables"
        )
        self.years = sorted(int(c) for c in data if c.isdigit())
        if not self.data.index.is_unique:
            raise ValueError("Duplicate scenario variables")

    def value(self, variable, year):
        if not self.years[0] <= year <= self.years[-1]:
            raise ValueError("Coproduct output extrapolation is not permitted")
        return float(
            np.interp(
                year,
                self.years,
                self.data.loc[variable, [str(y) for y in self.years]].to_numpy(float),
            )
        )


@contextmanager
def wood_coproduct_inventories(scenario_data, scenario, audit_path):
    import premise.external as external
    from premise.geomap import Geomap

    values = ScenarioValues(scenario_data, scenario)
    original = external.check_inventories
    original_regionalize = external.ExternalScenario.regionalize_inventories
    records = []

    def check(*args, **kwargs):
        database = kwargs["database"]
        pathways = kwargs["configuration"]["production pathways"]
        year = kwargs["year"]
        region = Geomap(model=kwargs["model"]).ecoinvent_to_iam_location("CH")
        outputs = {
            "FT": (values.value(BIODIESEL, year), 0.0, values.value(BIODIESEL_H, year)),
            "gas": (
                values.value(BIOGAS, year),
                values.value(BIOGAS_E, year),
                values.value(BIOGAS_H, year),
            ),
        }
        sources = {}
        for process, name, product in (
            ("FT", FT_SOURCE, "diesel, synthetic"),
            ("gas", GAS_SOURCE, "biomethane, high pressure"),
        ):
            candidates = [
                d
                for d in database
                if d["name"] == name and d["reference product"] == product
            ]
            for location in ("CH", region, "RER"):
                matches = [d for d in candidates if d["location"] == location]
                if len(matches) == 1:
                    sources[process] = matches[0]
                    break
                if matches:
                    raise ValueError(f"Ambiguous coproduct source: {name}, {location}")
            else:
                raise ValueError(f"No supported coproduct source: {name}")
        for pathway, spec in PROVIDERS.items():
            if pathways[pathway]["ecoinvent alias"]["name"] != spec[0]:
                raise ValueError(f"Unexpected coproduct alias for {pathway}")
            source = sources[spec[3]]
            basis_name = source["name"] + ", STEM coproduct basis"
            bases = [
                d for d in database if d["name"] == basis_name and d["location"] == "CH"
            ]
            if not bases:
                database.append(process_basis(source))
                bases = [database[-1]]
            if len(bases) != 1:
                raise ValueError(f"Ambiguous coproduct process basis: {basis_name}")
            basis = bases[0]
            alpha = allocation(*outputs[spec[3]])
            dataset = allocated_provider(basis, spec, alpha)
            if any(
                d["name"] == dataset["name"] and d["location"] == "CH" for d in database
            ):
                raise ValueError(f"Duplicate coproduct provider: {dataset['name']}")
            database.append(dataset)
            records.append(
                {
                    "scenario": scenario,
                    "year": year,
                    "pathway": pathway,
                    "provider": {
                        k: dataset[k]
                        for k in ("name", "reference product", "unit", "location")
                    },
                    "source": {
                        k: source[k]
                        for k in ("name", "reference product", "unit", "location")
                    },
                    "basis": {
                        k: basis[k]
                        for k in ("name", "reference product", "unit", "location")
                    },
                    "outputs_PJ": dict(
                        zip(("fuel", "electricity", "heat"), outputs[spec[3]])
                    ),
                    "fuel_energy_share": alpha,
                    "lhv_MJ_per_source_unit": LHV[spec[3]],
                    "source_units_per_output_unit": dataset["exchanges"][1]["amount"],
                }
            )
        result = original(*args, **kwargs)
        Path(audit_path).write_text(json.dumps(records, indent=2) + "\n")
        return result

    def regionalize(self, *args, **kwargs):
        result = original_regionalize(self, *args, **kwargs)
        # Use the same regional supplier resolution as ordinary Premise proxies.
        # Only dedicated basis copies are changed; their background sources stay
        # available for other pathways. Swiss market replacement follows later.
        for dataset in self.database:
            if dataset.get("STEM coproduct needs regionalization"):
                self.relink_technosphere_exchanges(dataset)
                dataset["STEM coproduct needs regionalization"] = False
        return result

    external.check_inventories = check
    external.ExternalScenario.regionalize_inventories = regionalize
    try:
        yield records
    finally:
        external.check_inventories = original
        external.ExternalScenario.regionalize_inventories = original_regionalize
