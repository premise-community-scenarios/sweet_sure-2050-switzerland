"""Swiss heat-pump processes with STEM COPs and final-electricity accounting.

Applied only by the SWEET build. Materialize the physical heat-pump inventory
behind the Swiss forwarding process, keep equipment/refrigerant flows per MJ
of heat, and give each end use a dedicated process. Final-energy wrappers demand
COP MJ of heat per MJ of electricity; district heating demands MJ of heat.
The existing Swiss/NEU background heat-pump providers remain untouched.
"""

from contextlib import contextmanager
from copy import deepcopy
import json
import math
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

HEAT_NAME = "heat production, air-water heat pump 10kW"
HEAT_PRODUCT = "heat, air-water heat pump 10kW"
GRID_NAME = "market for electricity, low voltage (SPS)"
GRID_PRODUCT = "electricity, low voltage"
COP_VARIABLES = {
    "DH_heat_pump": "Efficiency of heat plants (thermal)|District heating|Heat plants|Electricity|Centralised Heat pump",
    "FE_industry_process_heat_pump": "Efficiency in industry sectors|Electricity|Heat pump electric",
    "FE_services_heat_pump": "Efficiency in Services sectors|Electricity|Heat pump electric",
    "FE_residential_space_heating_heat_pump": "Efficiency in Residential sector|Space heating|Electricty|Heat pump electric",
    "FE_residential_water_heating_heat_pump": "Efficiency in Residential sector|Water heating|Electricty|Heat pump electric",
}


def heat_name(pathway):
    return f"{HEAT_NAME}, STEM {pathway}"


def identity(dataset):
    return (
        dataset["name"],
        dataset.get("reference product", dataset.get("product")),
        dataset["location"],
        dataset["unit"],
    )


def unique(database, key):
    matches = [d for d in database if identity(d) == key]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one heat-pump supplier for {key}; found {len(matches)}"
        )
    return matches[0]


def operating_electricity(dataset):
    return [
        e
        for e in dataset["exchanges"]
        if e["type"] == "technosphere"
        and e.get("product") == GRID_PRODUCT
        and e["unit"] == "kilowatt hour"
    ]


def physical_heat_pump(database):
    """Resolve forwarding links; fail instead of guessing a different technology."""
    key = (HEAT_NAME, HEAT_PRODUCT, "CH", "megajoule")
    seen = []
    while key not in seen:
        seen.append(key)
        dataset = unique(database, key)
        if operating_electricity(dataset):
            return dataset, seen
        exchanges = [e for e in dataset["exchanges"] if e["type"] != "production"]
        if len(exchanges) != 1 or exchanges[0]["type"] != "technosphere":
            raise ValueError(
                f"Not a physical heat pump or a single forwarding link: {key}"
            )
        exc = exchanges[0]
        if exc.get("product") != HEAT_PRODUCT or not math.isclose(exc["amount"], 1):
            raise ValueError(f"Unexpected heat-pump forwarding exchange: {exc}")
        key = identity(exc)
    raise ValueError(f"Cycle in heat-pump forwarding chain: {seen}")


def deterministic_amount(exchange, amount):
    exchange["amount"] = float(amount)
    for field in ("loc", "scale", "shape", "minimum", "maximum", "negative", "input"):
        exchange.pop(field, None)
    exchange["uncertainty type"] = 0


def make_heat_pump(source, pathway, cop):
    if not math.isfinite(cop) or cop <= 0:
        raise ValueError(f"Invalid STEM heat-pump COP for {pathway}: {cop}")
    dataset = deepcopy(dict(source))
    dataset["exchanges"] = [deepcopy(dict(e)) for e in source["exchanges"]]
    for field in (
        "regions",
        "region mapping",
        "production volume variable",
        "production volume",
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
        "log parameters",
        "input",
    ):
        dataset.pop(field, None)
    dataset.update(name=heat_name(pathway), location="CH", code=uuid4().hex)
    dataset["current efficiency"] = cop
    dataset["has_downstream_consumer"] = True
    dataset["comment"] = str(dataset.get("comment", "")) + (
        f" SWEET Swiss heat pump for {pathway}. Physical proxy: {source['name']} "
        f"[{source['location']}]. STEM COP={cop:.12g}; operating electricity="
        "1/(3.6*COP) kWh per MJ useful heat. Equipment, refrigerant and biosphere "
        "exchanges retain their source quantities per MJ useful heat."
    )
    production = [e for e in dataset["exchanges"] if e["type"] == "production"]
    if len(production) != 1 or production[0]["amount"] != 1:
        raise ValueError("Heat-pump proxy must produce exactly one MJ of heat")
    production[0].update(name=dataset["name"], location="CH")
    production[0].pop("input", None)
    production[0].pop("production volume", None)
    electricity = operating_electricity(dataset)
    if len(electricity) != 1:
        raise ValueError("Expected one low-voltage operating-electricity exchange")
    electricity[0].update(name="market for electricity, low voltage", location="CH")
    deterministic_amount(electricity[0], 1 / (3.6 * cop))
    electricity[0][
        "comment"
    ] = f"STEM COP {cop:.12g}: 1/(3.6*COP) kWh/MJ heat; supplied by Swiss SPS electricity."
    return dataset


def normalize_wrapper(wrapper, heat, cop):
    if wrapper["unit"] != "megajoule" or wrapper["reference product"] != GRID_PRODUCT:
        raise ValueError(
            f"Expected a final-electricity wrapper measured in MJ: {identity(wrapper)}"
        )
    inputs = [e for e in wrapper["exchanges"] if e["type"] == "technosphere"]
    if len(inputs) != 1 or inputs[0].get("product") != HEAT_PRODUCT:
        raise ValueError(
            f"Unexpected exchanges in heat-pump wrapper: {identity(wrapper)}"
        )
    # Keep the workbook's direct biosphere bookkeeping flows; this correction
    # changes only the heat supplier and the electricity-to-heat conversion.
    exchange = inputs[0]
    exchange.update(
        name=heat["name"], product=HEAT_PRODUCT, location="CH", unit="megajoule"
    )
    deterministic_amount(exchange, cop)
    exchange["comment"] = (
        "STEM final electricity: COP MJ useful heat per MJ electricity; COP/(3.6*COP)=1/3.6 kWh operating electricity."
    )
    wrapper["comment"] = str(wrapper.get("comment", "")) + " " + exchange["comment"]


def swiss_wrapper(database, inventories, alias):
    """Ensure the final-energy wrapper exists in CH, without moving NEU."""
    candidates = [
        d
        for d in [*database, *inventories]
        if d["name"] == alias["name"]
        and d["reference product"] == alias["reference product"]
        and d["unit"] == "megajoule"
    ]
    local = [d for d in candidates if d["location"] == "CH"]
    if len(local) == 1:
        return local[0]
    if local or len(candidates) != 1:
        raise ValueError(
            f"Ambiguous source for Swiss heat-pump wrapper: {alias}, {len(candidates)} candidates"
        )
    # premise can duplicate the NEU residential wrapper for water heating.
    # Keep that background provider and create a separate CH endpoint.
    wrapper = deepcopy(dict(candidates[0]))
    wrapper["exchanges"] = [deepcopy(dict(e)) for e in candidates[0]["exchanges"]]
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
        "input",
    ):
        wrapper.pop(field, None)
    wrapper.update(location="CH", code=uuid4().hex)
    wrapper["has_downstream_consumer"] = True
    for exchange in wrapper["exchanges"]:
        if exchange["type"] == "production":
            exchange["location"] = "CH"
            exchange.pop("input", None)
    database.append(wrapper)
    return unique(
        database, (alias["name"], alias["reference product"], "CH", "megajoule")
    )


class StemCOPs:
    def __init__(self, path, scenario):
        data = pd.read_csv(path, float_precision="round_trip")
        self.data = data[data.scenario.eq(scenario) & data.region.eq("CH")].set_index(
            "variables"
        )
        self.years = sorted(int(c) for c in data.columns if c.isdigit())
        if not self.data.index.is_unique:
            raise ValueError(f"Duplicate scenario data for {scenario}")

    def value(self, variable, year):
        if not self.years[0] <= year <= self.years[-1]:
            raise ValueError("Heat-pump COP extrapolation is not permitted")
        values = self.data.loc[variable, [str(y) for y in self.years]].to_numpy(float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError(f"Missing or invalid heat-pump COP: {variable}")
        return float(np.interp(year, self.years, values))


@contextmanager
def swiss_stem_heat_pumps(scenario_data, scenario, audit_path):
    import premise.external as external

    cops = StemCOPs(scenario_data, scenario)
    original_check = external.check_inventories
    original_markets = external.ExternalScenario.create_markets
    records = []

    def check_inventories(*args, **kwargs):
        configuration = kwargs["configuration"]
        pathways = configuration["production pathways"]
        missing = COP_VARIABLES.keys() - pathways.keys()
        if missing:
            raise ValueError(f"Missing SWEET heat-pump pathways: {sorted(missing)}")
        database = kwargs["database"]
        source, chain = physical_heat_pump(database)
        prototypes = {}
        for pathway, variable in COP_VARIABLES.items():
            cop = cops.value(variable, kwargs["year"])
            heat = make_heat_pump(source, pathway, cop)
            if any(identity(d) == identity(heat) for d in database):
                raise ValueError(f"Duplicate dedicated heat pump: {identity(heat)}")
            database.append(heat)
            prototypes[pathway] = (heat, cop)
        # The district-heating pathway produces heat, while FE wrappers represent
        # electricity consumed. Only the heat-producing alias changes here.
        dh = pathways["DH_heat_pump"]
        dh["ecoinvent alias"]["name"] = heat_name("DH_heat_pump")
        # COP is already applied explicitly to operating electricity. Generic
        # efficiency scaling would also rescale equipment/refrigerants a second time.
        dh.pop("efficiency", None)
        result = original_check(*args, **kwargs)
        inventories, checked_database, checked_configuration, _ = result
        for pathway, (heat, cop) in prototypes.items():
            alias = checked_configuration["production pathways"][pathway][
                "ecoinvent alias"
            ]
            wrapper = None
            if pathway != "DH_heat_pump":
                wrapper = swiss_wrapper(checked_database, inventories, alias)
                normalize_wrapper(wrapper, heat, cop)
            records.append(
                {
                    "scenario": scenario,
                    "year": kwargs["year"],
                    "pathway": pathway,
                    "cop_variable": COP_VARIABLES[pathway],
                    "stem_cop": cop,
                    "source_chain": chain,
                    "source": identity(source),
                    "heat_process": identity(heat),
                    "wrapper": identity(wrapper) if wrapper else None,
                    "heat_MJ_per_MJ_final_electricity": cop if wrapper else None,
                    "operating_kWh_per_MJ_heat": 1 / (3.6 * cop),
                    "operating_MJ_per_MJ_final_electricity": 1.0 if wrapper else None,
                    "preserved_wrapper_biosphere": (
                        [
                            dict(e)
                            for e in wrapper["exchanges"]
                            if e["type"] == "biosphere"
                        ]
                        if wrapper
                        else []
                    ),
                    "source_non_electric_exchanges": [
                        dict(e)
                        for e in source["exchanges"]
                        if e["type"] != "production"
                        and e not in operating_electricity(source)
                    ],
                }
            )
        return result

    def create_markets(self, *args, **kwargs):
        result = original_markets(self, *args, **kwargs)
        for pathway in COP_VARIABLES:
            heat = unique(
                self.database, (heat_name(pathway), HEAT_PRODUCT, "CH", "megajoule")
            )
            electricity = operating_electricity(heat)
            if len(electricity) != 1 or identity(electricity[0]) != (
                GRID_NAME,
                GRID_PRODUCT,
                "CH",
                "kilowatt hour",
            ):
                raise ValueError(
                    f"Swiss heat-pump operating electricity was not linked to SPS: {electricity}"
                )
        Path(audit_path).write_text(json.dumps(records, indent=2) + "\n")
        return result

    external.check_inventories = check_inventories
    external.ExternalScenario.create_markets = create_markets
    try:
        yield records
    finally:
        external.check_inventories = original_check
        external.ExternalScenario.create_markets = original_markets
