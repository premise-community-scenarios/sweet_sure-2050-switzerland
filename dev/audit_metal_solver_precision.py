"""Resolve every strict independent-audit flag with SuperLU CSR solves.

The first audit and its UMFPACK flags remain preserved. Rechecks use original
package matrices and unchanged strict tolerances, with no Edges code imports.
"""

import argparse
import json
from pathlib import Path
import shutil
import numpy as np
import pandas as pd
from scipy.sparse.linalg import spsolve
from audit_metal_quantities import Inventory, ROOT, digest, write_json


def run(base, output):
    output.mkdir(parents=True, exist_ok=True)
    methods = [
        m
        for m in json.loads((base / "refined_metals/method_registry.json").read_text())[
            "methods"
        ]
        if "definition" in m
    ]
    initial = pd.read_parquet(output / "adjoint_checks.parquet")
    regional = pd.read_parquet(output / "location_checks.parquet")
    systems = pd.read_parquet(output / "system_checks.parquet")
    rows = []
    for (scenario, year), group in initial.groupby(["scenario", "year"], sort=True):
        flags = group[~group.passed | ~group.forward_passed]
        region_flags = regional[
            (regional.scenario == scenario) & (regional.year == year) & ~regional.passed
        ]
        system_flags = systems[
            (systems.scenario == scenario) & (systems.year == year) & ~systems.passed
        ]
        if not (len(flags) + len(region_flags) + len(system_flags)):
            continue
        inv = Inventory(
            next((base / "build/packages").glob(f"*-{scenario}.zip")),
            int(year),
            methods,
        )
        demands = pd.read_csv(
            base / "refined_metals/scenarios" / scenario / "pathways_demands.csv",
            float_precision="round_trip",
        )
        demands = demands[demands.year.eq(year)]
        variables = sorted(set(flags.variable) | set(region_flags.variable))
        rhs = np.zeros((len(inv.nodes), len(variables) + 1))
        for row in demands.to_dict("records"):
            root, amount = inv.demand(row)
            if row["variable"] in variables:
                rhs[root, variables.index(row["variable"])] += amount
            rhs[root, -1] += amount
        x = spsolve(inv.A, rhs, use_umfpack=False)
        if x.ndim == 1:
            x = x[:, None]
        manifest = json.loads(
            next(
                (base / "indicators/scenarios" / scenario).glob("*.manifest.json")
            ).read_text()
        )
        location = np.where(
            inv.nodes.location.eq("CH"),
            "CH",
            np.where(
                inv.nodes.location.isin(manifest["europe_locations"]), "EU wo CH", "RoW"
            ),
        )
        direct = inv.C @ x
        for kind, frame in [
            ("consumer", flags),
            ("location", region_flags),
            ("system", system_flags),
        ]:
            for r in frame.to_dict("records"):
                k = next(
                    i for i, m in enumerate(methods) if m["indicator"] == r["indicator"]
                )
                column = -1 if kind == "system" else variables.index(r["variable"])
                actual = (
                    float(direct[k, column])
                    if kind != "location"
                    else float((inv.C[k] * (location == r["location"])) @ x[:, column])
                )
                saved = r.get("saved_edges_kg", r.get("saved_kg"))
                rows.append(
                    {
                        "scenario": scenario,
                        "year": int(year),
                        "kind": kind,
                        "variable": r.get("variable", "SYSTEM"),
                        "location": r.get("location", "TOTAL"),
                        "indicator": r["indicator"],
                        "saved_edges_kg": saved,
                        "superlu_kg": actual,
                        "difference_kg": actual - saved,
                        "passed": bool(np.isclose(actual, saved, rtol=1e-8, atol=1e-6)),
                    }
                )
        pd.DataFrame(rows).to_csv(output / "solver_flag_rechecks.csv", index=False)
        print(
            f"Rechecked {scenario}/{year}: {len(flags) + len(region_flags) + len(system_flags)} flags",
            flush=True,
        )
    failures = sum(not r["passed"] for r in rows)
    write_json(
        output / "solver_backend_provenance.json",
        {
            "script_sha256": digest(__file__),
            "selection": "Every strict flag from all consumer, location and system checks; no tolerance relaxed",
            "rechecks": len(rows),
            "failures": failures,
            "max_absolute_difference_kg": max(
                (abs(r["difference_kg"]) for r in rows), default=0
            ),
        },
    )
    shutil.copy2(__file__, output / "audit_metal_solver_precision.py")
    if failures:
        raise ValueError(f"{failures} independent solver checks remain unresolved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / "results/sps_base")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run(args.base, args.output or args.base / "metal_audit_20261012")
