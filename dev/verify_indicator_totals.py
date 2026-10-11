"""Independently verify Pathways totals by solving A x = summed final demand.

Usage: python dev/verify_indicator_totals.py results_<package>.manifest.json
Reads exported CSV matrices directly; does not use Pathways calculation code.
"""

import argparse
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve

from generate_indicators import sha256


def solve_residuals(matrix, supply, demand):
    """Measure equation accuracy against each row's actual numerical scale.

    Demand-only normalization is misleading when large internal flows cancel.
    Normwise backward error scales each functional unit by its matrix, supply
    and demand magnitudes, including zero-demand rows in the residual norm.
    """
    error = np.abs(matrix @ supply - demand)
    relative = float(np.max(error) / max(float(np.max(np.abs(demand))), 1e-300))
    matrix_norm = float(abs(matrix).sum(axis=1).max())
    scale = matrix_norm * np.max(np.abs(supply), axis=0) + np.max(
        np.abs(demand), axis=0
    )
    error_norm = np.max(error, axis=0)
    normwise = np.divide(
        error_norm, scale, out=np.zeros_like(error_norm), where=scale != 0
    )
    if np.any((scale == 0) & (error_norm != 0)):
        return relative, float("inf")
    return relative, float(np.max(normwise))


def verify(manifest_path, years, rtol=1e-8, solver="umfpack", demand_mode="summed"):
    if not np.isfinite(rtol) or rtol < 0:
        raise ValueError("Relative tolerance must be finite and non-negative")
    if solver == "umfpack":
        from scikits.umfpack import splu
    elif solver != "superlu":
        raise ValueError(f"Unknown solver: {solver}")
    if demand_mode not in ("summed", "separate"):
        raise ValueError(f"Unknown demand mode: {demand_mode}")
    if demand_mode == "separate" and solver != "superlu":
        raise ValueError("Separate-demand verification requires SuperLU")
    manifest = json.loads(manifest_path.read_text())
    directory = manifest_path.parent
    stem = manifest_path.name.removesuffix(".manifest.json")
    raw = pd.read_parquet(directory / f"{stem}.gzip")
    demands = pd.read_csv(
        directory / f"{stem}_demands.csv", float_precision="round_trip"
    )
    package = Path(manifest["coverage"]["working_package"])
    if sha256(package) != manifest["calculation"]["working_package_sha256"]:
        raise ValueError("Working package checksum changed")
    if sha256(directory / f"{stem}.gzip") != manifest["raw_sha256"]:
        raise ValueError("Result checksum changed")
    version = manifest["calculation"]["ecoinvent_version"].replace(".", "")
    cf_path = Path(manifest["pathways_source"]) / "data" / f"lcia_ei{version}.json"
    if sha256(cf_path) != manifest["calculation"]["cf_sha256"]:
        raise ValueError("Characterization factors changed")
    registry = {
        " - ".join(method["name"]): method for method in json.loads(cf_path.read_text())
    }
    results = []
    with zipfile.ZipFile(package) as archive:
        for year in years:
            paths = [
                n for n in archive.namelist() if n.endswith(f"/{year}/A_matrix.csv")
            ]
            if len(paths) != 1:
                raise ValueError(f"Ambiguous matrix directory for {year}")
            base = str(Path(paths[0]).parent)

            def read(name):
                with archive.open(f"{base}/{name}.csv") as stream:
                    return pd.read_csv(stream, sep=";", float_precision="round_trip")

            a, b = read("A_matrix"), read("B_matrix")
            ai, bi = read("A_matrix_index"), read("B_matrix_index")
            index = {
                (r["name"], r["reference product"], r["unit"], r["location"]): r[
                    "index"
                ]
                for r in ai.to_dict("records")
            }
            year_demands = demands.loc[demands.year.eq(year)].to_dict("records")
            demand = (
                np.zeros((len(ai), len(year_demands)))
                if demand_mode == "separate"
                else np.zeros(len(ai))
            )
            for column, row in enumerate(year_demands):
                key = (
                    row["name"],
                    row["reference_product"],
                    row["unit"],
                    row["location"],
                )
                target = (
                    (index[key], column) if demand_mode == "separate" else index[key]
                )
                demand[target] += row["scenario_demand"] * row["conversion_factor"]
            # Sum repeated exchanges directly in CSR, as the calculation stack
            # does. An intermediate CSC conversion changes their addition order
            # and can perturb ill-conditioned systems by one floating-point bit.
            matrix = coo_matrix(
                (
                    a.value * np.where(a.flip.eq(1), -1, 1),
                    (a["index of product"], a["index of activity"]),
                ),
                shape=(len(ai), len(ai)),
            ).tocsr()
            supply = (
                spsolve(matrix.tocsr(), demand, use_umfpack=False)
                if solver == "superlu"
                else splu(matrix).solve(demand)
            )
            residual, backward_error = solve_residuals(matrix, supply, demand)
            if (
                not np.isfinite(supply).all()
                or not np.isfinite(backward_error)
                or backward_error > 1e-12
            ):
                raise ValueError(
                    f"Inaccurate independent solve: {year}, normwise backward error {backward_error}"
                )
            biosphere = coo_matrix(
                (b.value, (b["index of biosphere flow"], b["index of activity"])),
                shape=(len(bi), len(ai)),
            ).tocsr()
            inventory = biosphere @ (
                supply.sum(axis=1) if demand_mode == "separate" else supply
            )
            flows = {
                (r["name"], r["compartment"], r["subcompartment"]): r["index"]
                for r in bi.to_dict("records")
            }
            for method in manifest["calculation"]["methods"]:
                coefficients = {
                    (
                        r["name"],
                        r["categories"][0],
                        (
                            r["categories"][1]
                            if len(r["categories"]) > 1
                            else "unspecified"
                        ),
                    ): r["amount"]
                    for r in registry[method]["exchanges"]
                }
                score = float(
                    sum(
                        cf * inventory[flows[flow]]
                        for flow, cf in coefficients.items()
                        if flow in flows
                    )
                )
                pathways_score = float(
                    raw.loc[
                        raw.year.eq(year) & raw.impact_category.eq(method), "value"
                    ].sum()
                )
                np.testing.assert_allclose(score, pathways_score, rtol=rtol, atol=1e-5)
                results.append(
                    {
                        "year": year,
                        "method": method,
                        "independent_score": score,
                        "pathways_score": pathways_score,
                        "relative_difference": abs(score - pathways_score)
                        / max(abs(score), 1e-30),
                        "solve_relative_residual": residual,
                        "solve_normwise_backward_error": backward_error,
                    }
                )
            print(f"Verified all six indicators independently for {year}", flush=True)
    destination = directory / f"{stem}_independent_validation.json"
    destination.write_text(
        json.dumps(
            {
                "passed": True,
                "relative_tolerance": rtol,
                "absolute_tolerance": 1e-5,
                "normwise_backward_error_tolerance": 1e-12,
                "solver": solver,
                "demand_mode": demand_mode,
                "verification_script_sha256": sha256(Path(__file__)),
                "checks": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--years", type=int, nargs="+", default=[2020, 2050])
    parser.add_argument(
        "--solver",
        choices=["superlu", "umfpack"],
        default="umfpack",
        help="Independent sparse solver; SuperLU matches SciPy's dense multi-RHS backend used by Pathways",
    )
    parser.add_argument(
        "--demand-mode",
        choices=["summed", "separate"],
        default="summed",
        help="Check summed demand or independently solve each demand before summing supplies",
    )
    parser.add_argument(
        "--rtol",
        type=float,
        default=1e-8,
        help="Relative tolerance for the independent solver comparison (default: 1e-8)",
    )
    args = parser.parse_args()
    verify(
        args.manifest.resolve(),
        args.years,
        rtol=args.rtol,
        solver=args.solver,
        demand_mode=args.demand_mode,
    )
