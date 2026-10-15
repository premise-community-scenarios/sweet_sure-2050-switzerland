#!/usr/bin/env python3
"""Independently reconcile boiler capacity, allocation and exported material mass."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd


def audit(package, scenario_data):
    proof = json.loads(package.with_suffix(".electric_boilers.json").read_text())
    source = pd.read_csv(scenario_data, float_precision="round_trip")
    years = sorted(int(c) for c in source.columns if c.isdigit())
    rows = []
    with ZipFile(package) as z:
        for year in sorted({p["year"] for p in proof}):
            (member,) = [n for n in z.namelist() if n.endswith(f"/{year}/A_matrix.csv")]
            a = pd.read_csv(z.open(member), sep=";")
            nodes = pd.read_csv(
                z.open(member.replace("A_matrix.csv", "A_matrix_index.csv")), sep=";"
            )

            def inputs(name, location, unit):
                chosen = nodes[
                    nodes.name.eq(name)
                    & nodes.location.eq(location)
                    & nodes.unit.eq(unit)
                ]
                if len(chosen) != 1:
                    raise ValueError(f"Non-unique boiler node {name}/{location}")
                col = a[a["index of activity"].eq(chosen["index"].item())]
                production = col[col.flip.eq(0)]
                assert len(production) == 1 and production.value.item() == 1
                return col[col.flip.eq(1)].merge(
                    nodes, left_on="index of product", right_on="index"
                )

            for p in [p for p in proof if p["year"] == year]:
                data = source[
                    source.scenario.eq(p["scenario"]) & source.region.eq("CH")
                ].set_index("variables")
                capacity, energy = [
                    float(
                        np.interp(
                            year,
                            years,
                            data.loc[p[key], [str(y) for y in years]].to_numpy(float),
                        )
                    )
                    for key in ("capacity_variable", "energy_variable")
                ]
                expected = (
                    0
                    if energy == 0
                    else (capacity * 1e6 / p["power_kW"])
                    / (p["lifetime_years"] * energy * 1e9)
                )
                wrapper = inputs(p["wrapper"], "CH", "megajoule")
                actual = wrapper[wrapper.name.eq(p["hardware"][0])].value.sum()
                rows.append(
                    dict(
                        year=year,
                        profile=p["id"],
                        check="units per MJ from raw scenario capacity and energy",
                        expected=expected,
                        actual=actual,
                        passed=bool(
                            np.isclose(actual, expected, rtol=2e-6, atol=1e-15)
                        ),
                    )
                )
                grid = wrapper[
                    wrapper["reference product"].eq("electricity, low voltage")
                    & wrapper.unit.eq("kilowatt hour")
                ]
                assert (
                    len(grid) == 1
                    and "SPS" in grid.name.item()
                    and np.isclose(grid.value.item() * 3.6, 1, rtol=2e-6)
                )
                hardware = inputs(p["hardware"][0], "RER", "unit")
                grouped = defaultdict(float)
                for label, mass in p["materials_kg"].items():
                    if label == "unmodelled":
                        continue
                    grouped[tuple(p["material_suppliers"][label])] += mass
                for (name, product, location, unit), mass in grouped.items():
                    actual = hardware[
                        hardware.name.eq(name)
                        & hardware["reference product"].eq(product)
                        & hardware.unit.eq(unit)
                    ].value.sum()
                    rows.append(
                        dict(
                            year=year,
                            profile=p["id"],
                            check=name,
                            expected=mass,
                            actual=actual,
                            passed=bool(
                                np.isclose(actual, mass, rtol=2e-6, atol=1e-12)
                            ),
                        )
                    )
                assert np.isclose(
                    hardware.value.sum(),
                    p["mass_kg"] - p.get("unmodelled_mass_kg", 0),
                    rtol=2e-6,
                )
    frame = pd.DataFrame(rows)
    frame.to_csv(package.with_suffix(".electric_boiler_checks.csv"), index=False)
    if frame.empty or not frame.passed.all():
        raise ValueError("Boiler equipment back-calculation failed")
    summary = {
        "passed": True,
        "checks": len(frame),
        "profiles": 4,
        "years": sorted({p["year"] for p in proof}),
        "interpretation": "Arithmetic and declared mass checked; component/material proxies and manufacturing omissions remain explicit in source metadata.",
    }
    package.with_suffix(".electric_boiler_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--scenario-data", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.package, args.scenario_data)))
