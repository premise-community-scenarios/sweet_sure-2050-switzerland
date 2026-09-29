"""Fuel-based absolute STEM efficiencies for allocated CHP and CCS wrappers.

Scoped to external STEM transformations; source Brightway databases are read only.
The ecoinvent production allocation factor is retained. Physical fuel demand is
recovered as allocated fuel demand / allocation factor. A CCS wrapper's fuel
basis is traced through its electricity/heat supplier, not inferred from kWh
conversion alone. Absolute targets are never clipped or converted to trends.
"""

from contextlib import contextmanager
import json
import math
from pathlib import Path


def identity(dataset):
    return (
        dataset["name"].lower(),
        dataset.get("reference product", dataset.get("product", "")).lower(),
        dataset["location"],
    )


def source_allocations(source_db):
    from bw2data.backends.schema import ActivityDataset, ExchangeDataset

    activities = {
        row.code: (row.name.lower(), row.product.lower(), row.location)
        for row in ActivityDataset.select(
            ActivityDataset.code,
            ActivityDataset.name,
            ActivityDataset.product,
            ActivityDataset.location,
        ).where(ActivityDataset.database == source_db)
    }
    result = {}
    for row in ExchangeDataset.select(
        ExchangeDataset.output_code, ExchangeDataset.data
    ).where(
        (ExchangeDataset.output_database == source_db)
        & (ExchangeDataset.type == "production")
    ):
        factor = (
            row.data.get("properties", {}).get("allocation factor", {}).get("amount", 1)
        )
        if not (0 < factor <= 1):
            continue
        result[activities[row.output_code]] = float(factor)
    return result


class FuelBasis:
    def __init__(self, database, allocations, energy_for_exchange):
        self.database = {identity(d): d for d in database}
        self.allocations = allocations
        self.energy_for_exchange = energy_for_exchange

    def allocation(self, dataset):
        name, product, location = identity(dataset)
        name = name.split("_")[0]  # premise's pathway-specific duplicates
        location = dataset.get("region mapping", {}).get(location, location)
        return self.allocations.get((name, product, location), 1.0)

    def fuel_energy(self, dataset, visited=()):
        key = identity(dataset)
        if key in visited:
            raise ValueError(f"Cycle while tracing the physical fuel basis: {key}")
        # The custom waste inventories declare the combustion efficiency but
        # contain no waste-fuel input: treatment burdens were allocated to
        # energy in the workbook. Their small auxiliary fuel inputs are not
        # the combustion fuel basis. Retain the documented physical baseline.
        if "STEM declared fuel energy" in dataset:
            return dataset["STEM declared fuel energy"]
        if any(
            label in dataset["name"]
            for label in ("incineration", "biowaste", "plastic waste-fired")
        ) and dataset.get("current efficiency"):
            output = {"kilowatt hour": 3.6, "megajoule": 1.0}[dataset["unit"]]
            dataset["STEM declared fuel energy"] = (
                output / dataset["current efficiency"]
            )
            return dataset["STEM declared fuel energy"]
        fuel = sum(
            self.energy_for_exchange(e)
            for e in dataset["exchanges"]
            if e["type"] == "technosphere"
        )
        if fuel > 0:
            return fuel / self.allocation(dataset)
        # Only generating suppliers: electricity/heat inputs to CCS wrappers.
        energy_inputs = [
            e
            for e in dataset["exchanges"]
            if e["type"] == "technosphere"
            and e["unit"] in ("kilowatt hour", "megajoule")
            and e["amount"] > 0
        ]
        if not energy_inputs:
            inputs = [
                (e["name"], e["amount"], e["unit"])
                for e in dataset["exchanges"]
                if e["type"] == "technosphere"
            ]
            raise ValueError(
                f"No physical fuel basis found for {key}; cannot enforce an absolute STEM efficiency. Inputs: {inputs}"
            )
        total = 0.0
        for exc in energy_inputs:
            supplier_key = identity(exc)
            if supplier_key not in self.database:
                raise ValueError(f"Unresolved energy supplier {supplier_key} for {key}")
            total += exc["amount"] * self.fuel_energy(
                self.database[supplier_key], (*visited, key)
            )
        return total / self.allocation(dataset)

    def efficiency(self, dataset):
        output = {"kilowatt hour": 3.6, "megajoule": 1.0}[dataset["unit"]]
        return output / self.fuel_energy(dataset)


def apply_absolute(dataset, basis, target, rescale):
    if not math.isfinite(target) or target <= 0:
        raise ValueError(f"Invalid absolute efficiency: {target}")
    before = basis.efficiency(dataset)
    factor = before / target
    for exchange in dataset["exchanges"]:
        if exchange["type"] in ("technosphere", "biosphere"):
            rescale(exchange, factor)
    if "STEM declared fuel energy" in dataset:
        dataset["STEM declared fuel energy"] *= factor
    after = basis.efficiency(dataset)
    if not math.isclose(after, target, rel_tol=1e-9):
        raise ValueError(
            f"Failed STEM efficiency check for {identity(dataset)}: {after} != {target}"
        )
    dataset["current efficiency"] = after
    dataset.setdefault("log parameters", {}).update(
        {
            "old efficiency": before,
            "new efficiency": after,
            "technosphere scaling factor": factor,
            "biosphere scaling factor": factor,
        }
    )
    dataset["comment"] = dataset.get("comment", "") + (
        f" STEM absolute physical efficiency: {before:.8g} -> {after:.8g}. "
        "Fuel basis accounts for source allocation and upstream generating inputs; allocation retained."
    )
    return {
        "activity": dataset["name"],
        "product": dataset["reference product"],
        "location": dataset["location"],
        "old_physical_efficiency": before,
        "stem_target_efficiency": target,
        "verified_physical_efficiency": after,
        "scaling_factor": factor,
        "allocation_factor": basis.allocation(dataset),
        "basis": (
            "declared waste-combustion efficiency"
            if "STEM declared fuel energy" in dataset
            else "traced fuel inputs, allocation corrected"
        ),
    }


@contextmanager
def absolute_stem_efficiencies(source_db, audit_path):
    import premise.external as external
    from premise.external_data_validation import flag_activities_to_adjust
    from premise.transformation import calculate_input_energy, prepare_fuel_filters
    from premise.utils import rescale_exchange

    allocations = source_allocations(source_db)
    original_adjust = external.adjust_efficiency
    original_regionalize = external.ExternalScenario.regionalize_inventories
    original_check = external.check_inventories
    context = {}
    records = []

    def check_inventories(*args, **kwargs):
        result = original_check(*args, **kwargs)
        inventories, database, configuration, _ = result
        by_alias = {}
        for dataset in [*database, *inventories]:
            name, product, _ = identity(dataset)
            by_alias.setdefault((name, product), []).append(dataset)
        # premise tags a source before cloning it for another pathway. This
        # overwrites the first pathway's efficiency with the last clone's.
        # Rebind each final alias to its own configuration after cloning.
        for pathway, settings in configuration["production pathways"].items():
            alias = settings["ecoinvent alias"]
            candidates = by_alias.get(
                (alias["name"].lower(), alias["reference product"].lower()), []
            )
            regions = kwargs["scenario_data"]["regions"]
            absolute = any(e.get("absolute") for e in settings.get("efficiency", []))
            regionalize_needed = absolute and not all(
                any(d["location"] == r for d in candidates) for r in regions
            )
            for dataset in candidates:
                if dataset["location"] not in regions and "regions" not in dataset:
                    continue
                for field in (
                    "adjust efficiency",
                    "technosphere filters",
                    "biosphere filters",
                    "absolute efficiency",
                    "excludes technosphere",
                    "excludes biosphere",
                ):
                    dataset.pop(field, None)
                values = {
                    "production volume variable": pathway,
                    "efficiency": settings.get("efficiency", []),
                    "replaces": [],
                    "replaces in": [],
                    "replacement ratio": 1,
                    "regionalize": alias.get("regionalize", False)
                    or regionalize_needed,
                }
                flag_activities_to_adjust(
                    dataset, kwargs["scenario_data"], kwargs["year"], values
                )
        return result

    def regionalize(self, *args, **kwargs):
        context["transformer"] = self
        return original_regionalize(self, *args, **kwargs)

    def adjust(dataset, fuels_specs, fuel_map_reverse):
        active = {}
        for variable, absolute in dataset.get("absolute efficiency", {}).items():
            setting = dataset.get("technosphere filters", {}).get(variable)
            if absolute and setting and dataset["location"] in setting[1]:
                target = setting[1][dataset["location"]]
                if target != 0:
                    if setting[0] or dataset.get("excludes technosphere", {}).get(
                        variable
                    ):
                        raise ValueError(
                            "Filtered absolute STEM efficiencies need a dedicated fuel audit."
                        )
                    active[variable] = float(target)
        if not active:
            return original_adjust(dataset, fuels_specs, fuel_map_reverse)
        if len(set(active.values())) != 1:
            raise ValueError(
                f"Conflicting STEM efficiency targets for {identity(dataset)}: {active}"
            )
        transformer = context["transformer"]
        fuel_filters = prepare_fuel_filters(tuple(fuel_map_reverse))

        def energy(exc):
            if exc["amount"] <= 0 or not fuel_filters.matches(exc["name"]):
                return 0.0
            return float(
                calculate_input_energy(
                    exc["name"],
                    exc["amount"],
                    exc["unit"],
                    fuels_specs,
                    fuel_map_reverse,
                )
            )

        basis = FuelBasis(transformer.database, allocations, energy)
        record = apply_absolute(
            dataset,
            basis,
            next(iter(active.values())),
            lambda exc, factor: rescale_exchange(exc, factor, remove_uncertainty=False),
        )
        record.update(year=transformer.year, variables=list(active))
        records.append(record)
        Path(audit_path).write_text(json.dumps(records, indent=2) + "\n")
        return dataset

    external.adjust_efficiency = adjust
    external.check_inventories = check_inventories
    external.ExternalScenario.regionalize_inventories = regionalize
    try:
        yield records
    finally:
        external.adjust_efficiency = original_adjust
        external.check_inventories = original_check
        external.ExternalScenario.regionalize_inventories = original_regionalize
