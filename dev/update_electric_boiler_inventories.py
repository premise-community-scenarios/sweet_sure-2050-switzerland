#!/usr/bin/env python3
"""Build dedicated boiler hardware and per-MJ wrappers from sourced parameters.

The declarations give equipment mass, not a complete manufacturing LCI. All
material-class proxies and unquantified processes remain explicit in metadata.
"""

from copy import deepcopy
import json
import argparse

import pandas as pd
import yaml

from update_vehicle_energy_inventories import ROOT, read_workbook, write_blocks, digest

KEY = "electric boiler equipment"


def equipment_amount(capacity_gw, energy_pj, power_kw, life_years):
    """Units per MJ: installed kW / unit kW / years / annual final-energy MJ."""
    import math

    if not all(math.isfinite(v) and v >= 0 for v in (capacity_gw, energy_pj)):
        raise ValueError("Non-finite or negative boiler scenario values")
    if power_kw <= 0 or life_years <= 0:
        raise ValueError("Positive power and life required")
    if energy_pj == 0:
        if capacity_gw > 0:
            # An idle stock has no lifetime-attributed energy demand this year.
            return 0.0
        return 0.0
    if capacity_gw == 0:
        raise ValueError("Active boiler electricity with zero installed capacity")
    return capacity_gw * 1e6 / power_kw / life_years / (energy_pj * 1e9)


def material_masses(profile):
    if "materials_kg" in profile:
        result = profile["materials_kg"]
    else:
        result = {
            k: profile["mass_kg"] * f for k, f in profile["material_fractions"].items()
        }
    if abs(sum(result.values()) - profile["mass_kg"]) > 1e-8:
        raise ValueError("Equipment material mass does not close")
    return result


def build(write=False):
    import bw2data as bd

    bd.projects.set_current("ecoinvent-3.12-cutoff")
    cfg = yaml.safe_load(
        (ROOT / "configuration_file/electric_boiler_equipment.yaml").read_text()
    )
    db = list(bd.Database("ecoinvent-3.12-cutoff"))
    suppliers = {}
    for label, name in cfg["materials"].items():
        candidates = [a for a in db if a["name"] == name and a["unit"] == "kilogram"]
        for location in ("RER", "Europe without Switzerland", "GLO", "RoW", "CH"):
            chosen = [a for a in candidates if a["location"] == location]
            if len(chosen) == 1:
                suppliers[label] = chosen[0]
                break
        else:
            raise ValueError(f"No unambiguous material supplier: {label}: {name}")
    path = ROOT / "inventories/lci-sweet_sure.xlsx"
    data = read_workbook(path)
    byname = {d["name"]: d for d in data}
    original = byname["electricity, used in residential electric boiler"]
    base = pd.read_csv(ROOT / "scenario_data/scenario_data.csv")
    base = base[base.scenario.eq("SPS1_bas0") & base.region.eq("CH")].set_index(
        "variables"
    )
    replacements, records = [], []
    next_row = max(d["end"] for d in data) + 1

    def place(ds):
        nonlocal next_row
        if ds["name"] in byname:
            old = byname[ds["name"]]
            ds.update({k: old[k] for k in ("sheet", "start", "end")})
        else:
            ds.update(
                sheet=original["sheet"], start=next_row, end=next_row, append=True
            )
            next_row += 1
        replacements.append(ds)

    for label, profile in cfg["profiles"].items():
        hardware_name = f"electric boiler equipment production, {profile['model']}"
        hardware = {
            "name": hardware_name,
            "reference product": hardware_name.removeprefix(
                "electric boiler equipment production, "
            ),
            "unit": "unit",
            "location": "RER",
            "exchanges": [],
        }
        hardware["comment"] = (
            "Dedicated electric heating hardware; sourced mass with explicit background-LCI proxies. "
            + " ".join(profile["limitations"])
        )
        hardware["exchanges"].append(
            dict(
                name=hardware_name,
                product=hardware["reference product"],
                unit="unit",
                location="RER",
                type="production",
                amount=1,
            )
        )
        for material, mass in material_masses(profile).items():
            if material == "unmodelled":
                continue
            a = suppliers[material]
            hardware["exchanges"].append(
                dict(
                    name=a["name"],
                    product=a["reference product"],
                    unit="kilogram",
                    location=a["location"],
                    amount=mass,
                    type="technosphere",
                    comment=f"{material}: sourced material-class mass; mapping limitations in activity metadata",
                )
            )
        evidence = {
            **profile,
            "id": label,
            "sources": {k: cfg["sources"][k] for k in profile["source_ids"]},
            "boundary": cfg["boundary"],
            "hardware": [
                hardware["name"],
                hardware["reference product"],
                "RER",
                "unit",
            ],
            "materials_kg": material_masses(profile),
        }
        evidence["material_suppliers"] = {
            material: [
                suppliers[material]["name"],
                suppliers[material]["reference product"],
                suppliers[material]["location"],
                "kilogram",
            ]
            for material in evidence["materials_kg"]
            if material != "unmodelled"
        }
        evidence["unmodelled_mass_kg"] = evidence["materials_kg"].get("unmodelled", 0)
        hardware[KEY] = json.dumps(evidence, ensure_ascii=False)
        place(hardware)
        wrapper = deepcopy(original)
        wrapper["name"] = profile["name"]
        wrapper["exchanges"] = [
            e
            for e in wrapper["exchanges"]
            if e["type"] == "production"
            or e.get("product") == "electricity, low voltage"
        ]
        for e in wrapper["exchanges"]:
            if e["type"] == "production":
                e["name"] = wrapper["name"]
                e["amount"] = 1
            else:
                e["amount"] = 1 / 3.6
        capacity = float(base.loc[profile["capacity_variable"], "2020"])
        energy = float(base.loc[profile["energy_variable"], "2020"])
        amount = equipment_amount(
            capacity, energy, profile["power_kW"], profile["lifetime_years"]
        )
        wrapper["exchanges"].append(
            dict(
                name=hardware["name"],
                product=hardware["reference product"],
                location="RER",
                unit="unit",
                type="technosphere",
                amount=amount,
            )
        )
        wrapper[KEY] = json.dumps(evidence, ensure_ascii=False)
        wrapper["comment"] = (
            "Consumes exactly 1 MJ final electricity. Baseline hardware allocation uses SPS1/2020 installed capacity divided by annual electricity and lifetime; the build adapter replaces this for every scenario/year. "
            + " ".join(profile["limitations"])
        )
        place(wrapper)
        records.append(
            {
                **evidence,
                "baseline capacity GW": capacity,
                "baseline energy PJ": energy,
                "baseline units per MJ": amount,
            }
        )
    if write:
        write_blocks(path, replacements)
        config_path = ROOT / "configuration_file/config.yaml"
        # Limit textual edits to each selected alias, preserving comments/layout.
        text = config_path.read_text()
        for profile in cfg["profiles"].values():
            start = text.index("  " + profile["pathway"] + ":")
            end = text.find("\n  FE_", start + 1)
            block = text[start:end]
            import re

            changed = re.sub(
                r"(      name: ).*", lambda m: m[1] + profile["name"], block, count=1
            )
            text = text[:start] + changed + text[end:]
        config_path.write_text(text)
    output = ROOT / "results/sps_updated/electric_boiler_inventory_audit.json"
    output.parent.mkdir(exist_ok=True, parents=True)
    output.write_text(
        json.dumps(
            {"workbook_sha256": digest(path), "profiles": records},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"Prepared {len(records)} dedicated electric-boiler profiles; write={write}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    build(parser.parse_args().write)
