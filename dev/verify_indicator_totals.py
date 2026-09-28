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
from scikits.umfpack import splu

from generate_indicators import sha256


def verify(manifest_path, years):
    manifest = json.loads(manifest_path.read_text())
    directory = manifest_path.parent
    stem = manifest_path.name.removesuffix(".manifest.json")
    raw = pd.read_parquet(directory / f"{stem}.gzip")
    demands = pd.read_csv(directory / f"{stem}_demands.csv")
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
                    return pd.read_csv(stream, sep=";")

            a, b = read("A_matrix"), read("B_matrix")
            ai, bi = read("A_matrix_index"), read("B_matrix_index")
            index = {
                (r["name"], r["reference product"], r["unit"], r["location"]): r[
                    "index"
                ]
                for r in ai.to_dict("records")
            }
            demand = np.zeros(len(ai))
            for row in demands.loc[demands.year.eq(year)].to_dict("records"):
                key = (
                    row["name"],
                    row["reference_product"],
                    row["unit"],
                    row["location"],
                )
                demand[index[key]] += row["scenario_demand"] * row["conversion_factor"]
            matrix = coo_matrix(
                (
                    a.value * np.where(a.flip.eq(1), -1, 1),
                    (a["index of product"], a["index of activity"]),
                ),
                shape=(len(ai), len(ai)),
            ).tocsc()
            supply = splu(matrix).solve(demand)
            residual = float(
                np.max(np.abs(matrix @ supply - demand)) / np.max(np.abs(demand))
            )
            if not np.isfinite(supply).all() or residual > 1e-10:
                raise ValueError(f"Unstable independent solve: {year}, {residual}")
            biosphere = coo_matrix(
                (b.value, (b["index of biosphere flow"], b["index of activity"])),
                shape=(len(bi), len(ai)),
            ).tocsr()
            inventory = biosphere @ supply
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
                np.testing.assert_allclose(score, pathways_score, rtol=1e-8, atol=1e-5)
                results.append(
                    {
                        "year": year,
                        "method": method,
                        "independent_score": score,
                        "pathways_score": pathways_score,
                        "relative_difference": abs(score - pathways_score)
                        / max(abs(score), 1e-30),
                        "solve_relative_residual": residual,
                    }
                )
            print(f"Verified all six indicators independently for {year}", flush=True)
    destination = directory / f"{stem}_independent_validation.json"
    destination.write_text(
        json.dumps({"passed": True, "checks": results}, indent=2) + "\n"
    )
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--years", type=int, nargs="+", default=[2020, 2050])
    args = parser.parse_args()
    verify(args.manifest.resolve(), args.years)
