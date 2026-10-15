"""Use scenario-adjusted vehicle equipment in Swiss final-energy wrappers."""

from contextlib import contextmanager
from copy import deepcopy
import json
from uuid import uuid4


def physical_cng_supply(base, lookup):
    """Follow market forwarding links to the kg-to-m3 compression recipe."""
    from premise.vehicle_energy_use import identity, resolve

    seen = set()
    while identity(base) not in seen:
        seen.add(identity(base))
        inputs = [e for e in base["exchanges"] if e["type"] == "technosphere"]
        if any(
            e.get("product", "").startswith("natural gas, high pressure")
            and e["unit"] == "cubic meter"
            for e in inputs
        ):
            return base
        forwarding = [
            e
            for e in inputs
            if e["unit"] == "kilogram"
            and e.get("product") == "natural gas, medium pressure, vehicle grade"
        ]
        if (
            len(inputs) != 1
            or len(forwarding) != 1
            or abs(forwarding[0]["amount"] - 1) > 1e-6
        ):
            break
        base = resolve(lookup, forwarding[0])
    raise ValueError(
        f"Cannot identify physical CNG compression recipe: {identity(base)} / {[(identity(e), e['amount']) for e in inputs]}"
    )


@contextmanager
def swiss_vehicle_energy(audit_path):
    import premise.external as external
    from premise.vehicle_energy_use import (
        normalize,
        select_source,
        source_name,
        resolve,
        VehicleLookup,
    )

    original = external.check_inventories
    records = []

    def checked(*args, **kwargs):
        result = original(*args, **kwargs)
        inventories, database, configuration, mapping = result
        all_data = list(database) + list(inventories)
        vehicles = VehicleLookup(all_data)
        aliases = {
            v["ecoinvent alias"]["name"]
            for v in configuration["production pathways"].values()
        }
        required = {
            v["ecoinvent alias"].get("source name", v["ecoinvent alias"]["name"])
            for v in configuration["production pathways"].values()
            if source_name(v["ecoinvent alias"]["name"])
        }
        aliases |= required
        present = {
            ds["name"]
            for ds in all_data
            if ds.get("location") == "CH" and ds.get("unit") == "megajoule"
        }
        if required - present:
            raise ValueError(
                f"Missing Swiss vehicle-energy endpoints: {sorted(required - present)}"
            )
        seen = set()
        cng_supplier = None
        for ds in all_data:
            name = source_name(ds["name"])
            if (
                not name
                or ds["name"] not in aliases
                or ds.get("unit") != "megajoule"
                or ds.get("location") != "CH"
                or id(ds) in seen
            ):
                continue
            seen.add(id(ds))
            # SWEET's Swiss transport is represented by REMIND NEU vehicles.
            # Prefer this prospective physical inventory to a residual RER copy.
            source = select_source(vehicles, name, "NEU", region=ds["location"])
            record = normalize(ds, source, vehicles)
            # Keep the delivered CNG kg basis and its compression recipe. The
            # SPS methane market supplies m3: replacing the kg input directly
            # would be a unit error, while leaving RER compression would miss
            # the Swiss scenario gas mix.
            for e in ds["exchanges"]:
                if (
                    e.get("type") != "technosphere"
                    or e.get("product") != "natural gas, medium pressure, vehicle grade"
                ):
                    continue
                if cng_supplier is None:
                    base = physical_cng_supply(resolve(vehicles, e), vehicles)
                    cng_supplier = deepcopy(dict(base))
                    cng_supplier["exchanges"] = [
                        deepcopy(dict(x)) for x in base["exchanges"]
                    ]
                    cng_supplier.update(
                        name="natural gas, vehicle grade, at fuelling station (SPS)",
                        location="CH",
                        code=uuid4().hex,
                    )
                    for field in (
                        "emptied",
                        "regionalized",
                        "regionalize",
                        "regions",
                        "region mapping",
                        "input",
                        "adjust efficiency",
                        "absolute efficiency",
                    ):
                        cng_supplier.pop(field, None)
                    for x in cng_supplier["exchanges"]:
                        x.pop("input", None)
                        if x["type"] == "production":
                            x.update(name=cng_supplier["name"], location="CH")
                        elif x["type"] == "technosphere" and (
                            x.get("product", "").startswith("electricity,")
                            or x.get("product")
                            in (
                                "natural gas, high pressure",
                                "natural gas, high pressure, vehicle grade",
                                "natural gas, low pressure",
                            )
                        ):
                            x["location"] = "CH"
                            if x.get("product", "").startswith("electricity,"):
                                x["name"] = "market for " + x["product"]
                    cng_supplier["comment"] = (
                        "Source vehicle-grade gas recipe and compression retained per kg delivered; high-pressure gas and electricity supplied by Swiss SPS markets."
                    )
                    database.append(cng_supplier)
                    all_data.append(cng_supplier)
                e.update(name=cng_supplier["name"], location="CH")
                e.pop("input", None)
            # The motorcycle's original gasoline-blend helper is otherwise a
            # CH service fuel proxy that bypasses the SPS petrol market.
            for e in ds["exchanges"]:
                if (
                    e.get("type") == "technosphere"
                    and e["name"] == "fuel supply for gasoline vehicles"
                ):
                    e.update(
                        name="market for petrol, low-sulfur",
                        product="petrol, low-sulfur",
                        location="CH",
                    )
                    e.pop("input", None)
                elif (
                    e.get("type") == "technosphere"
                    and e.get("product") in ("diesel", "diesel, low-sulfur")
                    and e["name"].startswith("market group for diesel")
                ):
                    # Native freight shunting remains an explicit auxiliary.
                    # Use the alias handled by the Swiss SPS diesel replacement.
                    e.update(name="market for " + e["product"], location="CH")
                    e.pop("input", None)
            records.append(
                {
                    "year": kwargs["year"],
                    "wrapper": ds["name"],
                    "location": ds["location"],
                    **record,
                }
            )
        return result

    external.check_inventories = checked
    try:
        yield
    finally:
        external.check_inventories = original
        audit_path.write_text(json.dumps(records, indent=2) + "\n")
