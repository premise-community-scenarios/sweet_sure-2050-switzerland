"""Resolve strict cross-solver audit flags using independent direct CSR solves."""

import argparse
import json
from pathlib import Path
import warnings
import numpy as np
import pandas as pd
from scipy.sparse.linalg import spsolve
from workflow_audit_core import MatrixAudit, digest


def recheck(batch):
    out = batch / "workflow_audit"
    checks = pd.read_csv(out / "random_score_checks.csv")
    flags = checks[~checks.passed]
    records = []
    for (scenario, year), group in flags.groupby(["scenario", "year"]):
        folder = batch / "scenarios" / scenario
        manifest = json.loads(next(folder.glob("*.manifest.json")).read_text())
        demands = pd.read_csv(
            next(folder.glob("*_demands.csv")), float_precision="round_trip"
        )
        variables = sorted(group.variable.unique())
        with warnings.catch_warnings(record=True):
            m = MatrixAudit(
                manifest["package"],
                int(year),
                Path(manifest["pathways_source"]) / "data/lcia_ei312.json",
                manifest["calculation"]["methods"],
            )
            f = np.zeros((len(m.ai), len(variables)))
            for j, var in enumerate(variables):
                row = demands[demands.year.eq(year) & demands.variable.eq(var)].iloc[0]
                f[
                    m.find(row["name"], row.reference_product, row.unit, row.location),
                    j,
                ] = (
                    row.scenario_demand * row.conversion_factor
                )
            # CSR is intentional: preserve the original assembly and ordering.
            x = spsolve(m.A, f, use_umfpack=False)
        residual = np.max(np.abs(m.A @ x - f), axis=0) / (
            float(abs(m.A).sum(axis=1).max()) * np.max(np.abs(x), axis=0)
            + np.max(np.abs(f), axis=0)
        )
        assert np.max(residual) < 1e-12
        geography = np.where(
            m.ai.location.eq("CH"),
            "CH",
            np.where(
                m.ai.location.isin(manifest["europe_locations"]), "EU wo CH", "RoW"
            ),
        )
        labels = [
            manifest["methods"][method][0]
            for method in manifest["calculation"]["methods"]
        ]
        for row in group.to_dict("records"):
            j = variables.index(row["variable"])
            k = labels.index(row["impact"])
            mask = (
                np.ones(len(m.ai), bool)
                if row["location"] == "TOTAL"
                else geography == row["location"]
            )
            score = float(np.sum(m.direct[k, mask] * x[mask, j]))
            relative = abs(score - row["latest_score"]) / max(
                abs(row["latest_score"]), 1e-30
            )
            passed = bool(np.isclose(score, row["latest_score"], rtol=1e-8, atol=1e-10))
            records.append(
                {
                    **row,
                    "independent_superlu_score": score,
                    "superlu_relative_difference": relative,
                    "recheck_passed": passed,
                    "backward_error": float(np.max(residual)),
                }
            )
        pd.DataFrame(records).to_csv(
            out / "random_solver_flag_rechecks.csv", index=False
        )
        print(f"Rechecked {scenario}/{year}: {len(group)} strict flags", flush=True)
    summary = {
        "strict_flags": len(flags),
        "rechecked": len(records),
        "unresolved": sum(not r["recheck_passed"] for r in records),
        "max_recheck_relative_difference": max(
            (r["superlu_relative_difference"] for r in records), default=0
        ),
        "script_sha256": digest(Path(__file__)),
        "note": "Original UMFPACK results, strict flags and warnings remain in the first audit. Independent CSR SuperLU calculations use exported matrices without Pathways code.",
    }
    (out / "random_solver_recheck_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("batch", type=Path)
    recheck(p.parse_args().batch)
