"""Historical chemical/glider audit of the original SPS batch.

The glider sensitivity predates the accepted energy-equipment boundary and is
not a proposed addition. The current review imports only chemical arithmetic.
"""

import json
import re
import shutil
from collections import Counter

import numpy as np
import pandas as pd
import yaml

from audit_metal_quantities import ROOT, Inventory, digest, write_json
from audit_metal_coefficients import children


def formula_fraction(formula, symbol, weights):
    terms = re.findall(r"([A-Z][a-z]?)(\d*)", formula)
    assert "".join(a + b for a, b in terms) == formula
    counts = Counter()
    for atom, number in terms:
        counts[atom] += int(number or 1)
    return (
        weights[symbol]
        * counts[symbol]
        / sum(weights[a] * n for a, n in counts.items())
    )


def run():
    base = ROOT / "results/sps_base"
    results = base / "refined_metals"
    out = base / "metal_audit_20261012"
    registry = json.loads((results / "method_registry.json").read_text())
    methods = [m for m in registry["methods"] if "definition" in m]
    config = yaml.safe_load(
        (ROOT / "configuration_file/refined_metal_inputs.yaml").read_text()
    )
    documents = json.loads((results / "product_documentation.json").read_text())
    rows = []
    for method in methods:
        definition = method["definition"]
        is_alloy = definition.get("kind") == "alloy_mass"
        entry = next(
            x
            for x in config["alloy_indicators" if is_alloy else "indicators"]
            if x["name"] == method["label"]
        )
        for product in definition["products"]:
            settings = {} if is_alloy else entry["products"][product["product"]]
            expected = (
                1.0
                if is_alloy
                else (
                    settings["factor"]
                    if "factor" in settings
                    else formula_fraction(
                        settings["formula"], entry["symbol"], config["atomic_weights"]
                    )
                )
            )
            metadata = [
                r for r in documents if r["reference product"] == product["product"]
            ]
            rows.append(
                {
                    "indicator": method["indicator"],
                    "product": product["product"],
                    "formula": settings.get("formula"),
                    "saved_factor": product["factor"],
                    "independent_factor": expected,
                    "passed": bool(
                        np.isclose(expected, product["factor"], rtol=1e-13, atol=1e-15)
                        and 0 < expected <= 1
                    ),
                    "source_basis": settings.get(
                        "basis",
                        (
                            "nominal formula"
                            if not is_alloy
                            else "kg alloy, no element composition inferred"
                        ),
                    ),
                    "local_documentation_records": len(metadata),
                    "unit_records_all_kg": all(
                        r["unit"] == "kilogram" for r in metadata
                    ),
                }
            )
    assert all(r["passed"] for r in rows)
    pd.DataFrame(rows).to_csv(out / "chemical_factor_checks.csv", index=False)
    # A scaling coefficient only: 100 kg is not an asserted glider mass.
    scenario, year = "SPS1_bas0", 2050
    inv = Inventory(
        next((base / "build/packages").glob(f"*-{scenario}.zip")), year, methods
    )
    hydroxide = inv.identity[
        ("barium hydroxide production", "barium hydroxide", "kilogram", "GLO")
    ]
    oxide_inputs = [
        c
        for i, c in children(inv, hydroxide)
        if inv.nodes.loc[i, "reference product"] == "barium oxide"
    ]
    assert len(oxide_inputs) == 1
    w = config["atomic_weights"]
    dry = w["Ba"] + 2 * w["O"] + 2 * w["H"]
    wet = dry + 8 * (2 * w["H"] + w["O"])
    oxide = w["Ba"] + w["O"]
    write_json(
        out / "barium_basis_evidence.json",
        {
            "dataset": "barium hydroxide production:GLO",
            "scenario": scenario,
            "year": year,
            "observed_BaO_input_kg": oxide_inputs[0],
            "source_efficiency": 0.95,
            "anhydrous_BaO_input_at_95percent": oxide / dry / 0.95,
            "octahydrate_BaO_input_at_95percent": oxide / wet / 0.95,
            "contained_Ba_factor": w["Ba"] / dry,
            "interpretation": "Process prose describes octahydrate crystallization, but the inventory input and reference-product identity support an anhydrous reference mass basis. Retain the existing factor.",
            "source_metadata": [
                r for r in documents if r["name"] == "barium hydroxide production"
            ],
        },
    )
    demands = pd.read_csv(
        results / "scenarios" / scenario / "pathways_demands.csv",
        float_precision="round_trip",
    )
    group = demands[demands.year.eq(year)]
    glider = inv.identity[
        ("market for glider, passenger car", "glider, passenger car", "kilogram", "GLO")
    ]
    extras = []
    for r in group.to_dict("records"):
        if not r["name"].startswith("electricity, used in passenger car vehicle"):
            continue
        root, amount = inv.demand(r)
        extra = amount * 100 / (200000 * 0.75)
        for k, m in enumerate(methods):
            extras.append(
                {
                    "scenario": scenario,
                    "year": year,
                    "variable": r["variable"],
                    "indicator": m["indicator"],
                    "extra_glider_kg_per_vehicle": 100,
                    "lifetime_energy_MJ": 150000,
                    "additional_glider_kg": extra,
                    "additional_material_kg": extra * inv.Z[k, glider],
                    "glider_intensity_kg_per_kg": inv.Z[k, glider],
                }
            )
    pd.DataFrame(extras).to_csv(out / "glider_per_100kg_sensitivity.csv", index=False)
    # Transparent two-tier back-calculation for an independently sampled non-transport use.
    r = (
        group[group.variable.eq("EXT - 0 - FE_residential_space_heating_heat_pump")]
        .iloc[0]
        .to_dict()
    )
    root, amount = inv.demand(r)
    heat = children(inv, root)
    heat_rows = []
    for h, q in heat:
        for i, c in children(inv, h):
            for k, m in enumerate(methods):
                if m["label"] not in [
                    "Copper",
                    "Iron and steel alloys",
                    "Aluminium alloys",
                ]:
                    continue
                heat_rows.append(
                    {
                        "scenario": scenario,
                        "year": year,
                        "variable": r["variable"],
                        "annual_final_energy_MJ": amount,
                        "heat_per_final_MJ": q,
                        "supplier": inv.nodes.loc[i, "name"],
                        "product": inv.nodes.loc[i, "reference product"],
                        "unit": inv.nodes.loc[i, "unit"],
                        "input_per_heat_MJ": c,
                        "annual_input": amount * q * c,
                        "indicator": m["indicator"],
                        "kg_per_input_unit": inv.Z[k, i],
                        "branch_kg": amount * q * c * inv.Z[k, i],
                        "consumer_total_kg": amount * inv.Z[k, root],
                    }
                )
    pd.DataFrame(heat_rows).to_csv(
        out / "heat_pump_equipment_backcalculation.csv", index=False
    )
    write_json(
        out / "boundary_review_provenance.json",
        {
            "script_sha256": digest(__file__),
            "configuration_sha256": digest(
                ROOT / "configuration_file/refined_metal_inputs.yaml"
            ),
            "factor_checks": len(rows),
            "factor_failures": sum(not r["passed"] for r in rows),
            "limitation": "Arithmetic consistency does not verify every chemical identity or hydration state; source evidence and unresolved assumptions are assessed separately in the report.",
        },
    )
    shutil.copy2(__file__, out / "audit_metal_boundary_review.py")


if __name__ == "__main__":
    run()
