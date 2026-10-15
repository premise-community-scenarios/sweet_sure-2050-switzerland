#!/usr/bin/env python3
"""Add traceable energy components to aggregate electricity endpoints.

Energy shares are never renormalized to the subset with an equipment inventory.
Unresolved categories retain operating energy and an explicit coverage gap.
"""

import argparse
from copy import deepcopy
import json
import re

import yaml

from update_vehicle_energy_inventories import ROOT, read_workbook, write_blocks

KEY = "aggregate electric equipment"


def shares(profile):
    energy = profile["energy_PJ"]
    if not energy or min(energy.values()) < 0 or sum(energy.values()) <= 0:
        raise ValueError("Invalid aggregate electricity shares")
    return {k: v / sum(energy.values()) for k, v in energy.items()}


def build(write=False):
    import bw2data as bd

    bd.projects.set_current("ecoinvent-3.12-cutoff")
    db = list(bd.Database("ecoinvent-3.12-cutoff"))
    cfg = yaml.safe_load(
        (ROOT / "configuration_file/aggregate_electric_equipment.yaml").read_text()
    )
    path = ROOT / "inventories/lci-sweet_sure.xlsx"
    data = read_workbook(path)
    byname = {d["name"]: d for d in data}
    template = byname["electricity, used in industrial electric processes"]
    replacements, records = [], []
    next_row = max(d["end"] for d in data) + 1

    def place(ds):
        nonlocal next_row
        if ds["name"] in byname:
            ds.update({k: byname[ds["name"]][k] for k in ("sheet", "start", "end")})
        else:
            ds.update(
                sheet=template["sheet"], start=next_row, end=next_row, append=True
            )
            next_row += 1
        replacements.append(ds)

    def supplier(name, location=None):
        matches = [
            a
            for a in db
            if a["name"] == name and (location is None or a["location"] == location)
        ]
        for loc in (location, "GLO", "RER", "Europe without Switzerland", "RoW"):
            selected = [a for a in matches if a["location"] == loc]
            if len(selected) == 1:
                return selected[0]
        raise ValueError(f"Ambiguous aggregate equipment supplier: {name}")

    def exchange(a, amount):
        return dict(
            name=a["name"],
            product=a["reference product"],
            location=a["location"],
            unit=a["unit"],
            type="technosphere",
            amount=float(amount),
        )

    def hardware(name, inputs, comment):
        ds = dict(
            name=name,
            **{"reference product": name},
            location="RER",
            unit="unit",
            comment=comment,
            exchanges=[
                dict(
                    name=name,
                    product=name,
                    location="RER",
                    unit="unit",
                    type="production",
                    amount=1,
                ),
                *inputs,
            ],
        )
        place(ds)
        return ds

    equipment = {}
    # Functional motor mass; no external frame, driven machine or packaging.
    motor_materials = [
        ("market for steel, low-alloyed", 134),
        ("market for cast iron", 116),
        ("market for aluminium, cast alloy", 4.1),
        ("market for copper, cathode", 12),
        ("market for glass fibre", 0.3),
        ("market for epoxy resin, liquid", 1.2),
        ("market for alkyd paint, white, without water, in 60% solution state", 1.6),
    ]
    motor = hardware(
        "energy equipment production, industrial induction motor, 22 kW (SPS)",
        [exchange(supplier(n), mass) for n, mass in motor_materials],
        "ABB M3JP 180MLB EPD: 22 kW shaft, 93.1% efficiency, 15 years at 5000 h/year. Old heavy flameproof motor is a technology proxy. Electrical steel represented by low-alloy steel; unspecified aluminium by cast alloy; insulation/resin/paint use generic LCI proxies. No driven-machine frame or packaging. Assembly energy and drives/controllers not reconstructed.",
    )
    equipment["motor"] = (motor, 1 / (22 / 0.931 * 15 * 5000 * 3.6))
    lamp = supplier("compact fluorescent lamp production", "GLO")
    equipment["lighting"] = (
        dict(
            name=lamp["name"],
            **{"reference product": lamp["reference product"]},
            location=lamp["location"],
            unit=lamp["unit"],
        ),
        1 / (0.010 * 10000 * 3.6),
    )
    for label, source_name, annual_kwh, life in [
        ("refrigerator", "refrigerator production", 214, 10),
        ("washer", "washing machine production", 255, 10),
        ("dishwasher", "dishwasher production", 203, 12.5),
    ]:
        source = supplier(source_name, "GLO")
        selected = [
            e
            for e in source.technosphere()
            if e.input["reference product"]
            in (
                "copper, cathode",
                "cable, unspecified",
                "printed wiring board, surface mounted, unspecified, Pb free",
            )
            and e["amount"] > 0
        ]
        if not selected:
            raise ValueError(f"No identifiable electrical components: {source_name}")
        ds = hardware(
            f"energy equipment production, identified electrical components of {label} (SPS)",
            [exchange(e.input, e["amount"]) for e in selected],
            f"Selected electrical materials from ecoinvent 3.12 {source_name}. Excludes unseparated structural materials, housing, ballast, and associated disposal. This is partial component coverage, not a complete appliance LCI. Lifetime {life} years; Swiss 2020 stock consumption {annual_kwh} kWh/year.",
        )
        equipment[label] = (ds, 1 / (annual_kwh * life * 3.6))
    for label, profile in cfg["profiles"].items():
        weights = shares(profile)
        wrapper = deepcopy(template)
        wrapper["name"] = profile["name"]
        wrapper["exchanges"] = [
            e
            for e in wrapper["exchanges"]
            if e["type"] == "production"
            or e.get("product") == "electricity, low voltage"
        ]
        for e in wrapper["exchanges"]:
            e["amount"] = 1 if e["type"] == "production" else 1 / 3.6
            if e["type"] == "production":
                e["name"] = wrapper["name"]
        evidence = {
            **profile,
            "id": label,
            "sources": cfg["sources"],
            "boundary": cfg["boundary"],
            "interpretation": cfg["interpretation"],
            "shares": weights,
            "components": [],
        }
        for category, weight in weights.items():
            if category not in equipment:
                continue
            ds, coefficient = equipment[category]
            amount = weight * coefficient
            e = dict(
                name=ds["name"],
                product=ds["reference product"],
                location=ds["location"],
                unit=ds["unit"],
                type="technosphere",
                amount=amount,
            )
            wrapper["exchanges"].append(e)
            evidence["components"].append(
                {
                    "category": category,
                    "share": weight,
                    "per_MJ_of_category": coefficient,
                    "exchange": e,
                }
            )
        evidence["fraction with some identified components"] = sum(
            weights[k] for k in equipment if k in weights
        )
        evidence["fraction without equipment"] = sum(
            v for k, v in weights.items() if k not in equipment
        )
        wrapper[KEY] = json.dumps(evidence)
        wrapper["comment"] = (
            cfg["boundary"]
            + " "
            + cfg["interpretation"]
            + " "
            + " ".join(profile["limitations"])
        )
        place(wrapper)
        records.append(evidence)
    if write:
        write_blocks(path, replacements)
        config_path = ROOT / "configuration_file/config.yaml"
        text = config_path.read_text()
        for profile in cfg["profiles"].values():
            start = text.index("  " + profile["pathway"] + ":")
            end = text.find("\n  FE_", start + 1)
            block = re.sub(
                r"(      name: ).*",
                lambda m: m[1] + profile["name"],
                text[start:end],
                count=1,
            )
            text = text[:start] + block + text[end:]
        config_path.write_text(text)
    output = ROOT / "results/sps_updated/aggregate_electric_inventory_audit.json"
    output.write_text(json.dumps(records, indent=2) + "\n")
    print(
        f"Prepared {len(records)} aggregate endpoints with explicit residual gaps; write={write}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    build(parser.parse_args().write)
