#!/usr/bin/env python3
"""Add Edges metal-input indicators to the validated SPS Pathways results.

Runs from existing package matrices and the exact previously validated Pathways
scenario demands. No Brightway database is imported or changed.
"""

import argparse
from datetime import datetime, timezone
import gc
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import time
from zipfile import ZipFile

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.sparse.linalg import spsolve
import yaml

if __package__:
    from .refined_metal_edges import characterize, independent_coefficients
    from .refined_metal_methods import digest, prepare
    from .verify_indicator_totals import solve_residuals
    from .refine_inventory_solution import refine
else:
    from refined_metal_edges import characterize, independent_coefficients
    from refined_metal_methods import digest, prepare
    from verify_indicator_totals import solve_residuals
    from refine_inventory_solution import refine

ROOT = Path(__file__).resolve().parents[1]
YEARS = [2020, 2025, 2030, 2035, 2040, 2045, 2050]


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def verify_base(base):
    summary = json.loads((base / "completion_summary.json").read_text())
    assert summary["indicators"] == 81 and summary["scenarios"] == 4
    assert summary["independent_checks"] == 2268
    for name, expected in summary["outputs_sha256"].items():
        if digest(base / name) != expected:
            raise ValueError(f"Base results changed: {name}")
    return summary


def setup_runtime(output):
    runtime = output / ".runtime"
    runtime.mkdir(exist_ok=True)
    variables = {
        "USER_DATA_BASE_DIR": str(runtime / "data"),
        "DIR_CACHED_DB": str(runtime / "cache"),
        "USER_LOGS_DIR": str(runtime / "logs"),
        "STATS_DIR": str(runtime / "stats"),
    }
    (runtime / "variables.yaml").write_text(yaml.safe_dump(variables))
    os.chdir(runtime)
    return runtime


def sources():
    import edges
    import pathways

    roots = {
        "edges": Path(edges.__file__).parent,
        "pathways": Path(pathways.__file__).parent,
    }
    return {
        "versions": {
            p: metadata.version(p)
            for p in [
                "edges",
                "pathways",
                "bw2calc",
                "bw2data",
                "numpy",
                "scipy",
                "pandas",
                "clipspy",
                "numba",
            ]
        },
        "source_roots": {k: str(v) for k, v in roots.items()},
        "python_sources_sha256": {
            f"{name}/{p.relative_to(root)}": digest(p)
            for name, root in roots.items()
            for p in sorted(root.rglob("*.py"))
        },
        "scripts_sha256": {
            name: digest(ROOT / "dev" / name)
            for name in [
                "run_refined_metals.py",
                "refined_metal_edges.py",
                "refined_metal_methods.py",
                "verify_indicator_totals.py",
                "refine_inventory_solution.py",
            ]
        },
    }


def verify_demands(package, demands):
    """Independently reconcile saved demands with the package's annual PJ data."""
    with ZipFile(package) as archive:
        with archive.open("scenario_data/scenario_data.csv") as stream:
            scenario = pd.read_csv(stream, float_precision="round_trip")
        mapping = yaml.safe_load(archive.read("mapping/mapping.yaml"))
    selected = scenario.loc[
        scenario.region.eq("CH") & scenario.variables.str.startswith("EXT - 0 - FE")
    ]
    if selected.duplicated(["variables", "year"]).any():
        raise ValueError("Ambiguous scenario demands")
    selected = selected.set_index(["variables", "year"])
    for row in demands.to_dict("records"):
        source = selected.loc[(row["variable"], row["year"])]
        if (
            source.unit != "PJ/yr."
            or row["scenario_unit"] != "PJ/yr."
            or row["unit"] != "megajoule"
            or row["conversion_factor"] != 1e9
        ):
            raise ValueError("Unexpected final-energy unit conversion")
        if source.value < -1e-12:
            raise ValueError(
                "Negative annual demand exceeds the original roundoff policy"
            )
        np.testing.assert_allclose(
            row["scenario_demand"], max(0, source.value), rtol=1e-14, atol=1e-15
        )
        allowed = mapping[row["variable"]]["dataset"]
        if not any(
            (d["name"], d["reference product"], d["unit"])
            == (row["name"], row["reference_product"], row["unit"])
            for d in allowed
        ):
            raise ValueError("Demand supplier differs from the package mapping")


def calculate_year(package, year, demands, manifest, methods, runtime, output):
    import bw2calc
    from edges.matrix_builders import build_technosphere_edges_matrix
    from pathways.lca import get_lca_matrices

    start = time.monotonic()
    with ZipFile(package) as archive:
        names = [
            n
            for n in archive.namelist()
            if f"/{year}/" in n
            and n.endswith(
                (
                    "A_matrix.csv",
                    "B_matrix.csv",
                    "A_matrix_index.csv",
                    "B_matrix_index.csv",
                )
            )
        ]
        if len(names) != 4:
            raise ValueError(f"Expected four matrix resources for {year}")
        for name in names:
            destination = (runtime / name).resolve()
            if not destination.is_relative_to(runtime):
                raise ValueError("Unsafe package member path")
            archive.extract(name, runtime)
        a_name = next(n for n in names if n.endswith("/A_matrix.csv"))
        with archive.open(a_name) as stream:
            independent_a = pd.read_csv(stream, sep=";", float_precision="round_trip")
    package_scenario = Path(a_name).parent.parent.name
    data, tech_index, _, _, _ = get_lca_matrices(
        [str(runtime / n) for n in names],
        model="remind",
        scenario=package_scenario,
        year=year,
    )
    records = demands.loc[demands.year.eq(year)].to_dict("records")
    if len(records) != 101 or len({r["variable"] for r in records}) != 101:
        raise ValueError("Expected 101 unique Pathways final-energy demands")
    functional_units = {}
    for r in records:
        key = (r["name"], r["reference_product"], r["unit"], r["location"])
        functional_units[r["variable"]] = {
            tech_index[key]: r["scenario_demand"] * r["conversion_factor"]
        }
    lca = bw2calc.MultiLCA(
        demands=functional_units,
        method_config={"impact_categories": []},
        data_objs=[data],
        use_distributions=False,
    )
    lca.lci()
    matrix = lca.technosphere_matrix.tocsr()
    nodes = {
        lca.dicts.activity[i]: dict(
            zip(["name", "reference product", "unit", "location"], key)
        )
        for key, i in tech_index.items()
    }
    if any(lca.dicts.activity[i] != lca.dicts.product[i] for i in tech_index.values()):
        raise ValueError(
            "Distinct activity/product positions require an explicit adapter"
        )
    supply = np.column_stack([lca.supply_arrays[r["variable"]] for r in records])
    del lca.inventories
    # Independent assembly from CSV with pandas, independent scipy demand solve.
    ids = np.array(
        [lca.dicts.product[int(i)] for i in independent_a["index of product"]]
    )
    cols = np.array(
        [lca.dicts.activity[int(i)] for i in independent_a["index of activity"]]
    )
    independent_matrix = sparse.coo_matrix(
        (
            independent_a.value.to_numpy() * np.where(independent_a.flip.eq(1), -1, 1),
            (ids, cols),
        ),
        shape=matrix.shape,
    ).tocsr()
    difference = matrix - independent_matrix
    if difference.nnz and np.max(np.abs(difference.data)) > 1e-15:
        raise ValueError(
            "Independent CSV matrix differs from Pathways/Brightway assembly"
        )
    demand_matrix = np.zeros_like(supply)
    for c, r in enumerate(records):
        for activity, amount in functional_units[r["variable"]].items():
            demand_matrix[lca.dicts.product[activity], c] += amount
    independent_supply = spsolve(independent_matrix, demand_matrix, use_umfpack=False)
    supply, supply_refinement = refine(matrix, demand_matrix, supply)
    independent_supply, independent_refinement = refine(
        independent_matrix, demand_matrix, independent_supply
    )
    _, error = solve_residuals(independent_matrix, independent_supply, demand_matrix)
    _, pathway_error = solve_residuals(matrix, supply, demand_matrix)
    if (
        not np.isfinite(supply).all()
        or not np.isfinite(independent_supply).all()
        or max(error, pathway_error) > 1e-12
    ):
        raise ValueError("Unreliable inventory solution")
    total_supply = supply.sum(axis=1)
    unit_edges = build_technosphere_edges_matrix(matrix, np.ones(matrix.shape[1]))
    inventory = build_technosphere_edges_matrix(matrix, total_supply)
    europe = set(manifest["europe_locations"])
    regions = np.array(
        [
            (
                "CH"
                if nodes[i]["location"] == "CH"
                else "EU wo CH" if nodes[i]["location"] in europe else "RoW"
            )
            for i in range(len(nodes))
        ]
    )
    frames, checks, route_rows = [], [], []
    suppliers_are_market = np.array(
        [
            nodes[i]["name"].startswith(("market for ", "market group for "))
            for i in range(len(nodes))
        ]
    )
    for method in methods:
        definition = method["definition"]
        coefficients, score, audit = characterize(
            matrix, nodes, total_supply, definition, inventory=inventory
        )
        weighted = coefficients.multiply(unit_edges).tocsr()
        consumer_weights = np.asarray(weighted.sum(axis=0)).ravel()
        independent_cf = independent_coefficients(independent_matrix, nodes, definition)
        # Independent coefficient selection does not call Edges or its flow builder.
        selected = independent_cf.tocoo()
        independent_weights = np.bincount(
            selected.col,
            weights=selected.data
            * -np.asarray(independent_matrix[selected.row, selected.col]).ravel(),
            minlength=len(nodes),
        )
        independent_by_demand = independent_weights @ independent_supply
        by_demand = consumer_weights @ supply
        np.testing.assert_allclose(
            by_demand, independent_by_demand, rtol=1e-8, atol=1e-7
        )
        np.testing.assert_allclose(score, by_demand.sum(), rtol=1e-12, atol=1e-7)
        location_sum = np.zeros(len(records))
        for location in ["CH", "EU wo CH", "RoW"]:
            values = (consumer_weights * (regions == location)) @ supply
            location_sum += values
            frames.append(
                pd.DataFrame(
                    {
                        "sector": [r["sector"] for r in records],
                        "variable": [r["variable"] for r in records],
                        "year": year,
                        "region": "CH",
                        "model": "remind",
                        "scenario": f"SSP2-PkBudg1000 - {package.stem.rsplit('-stem-', 1)[1]}",
                        "impact_category": method["indicator"],
                        "location": location,
                        "unit": method["unit"],
                        "value": values,
                    }
                )
            )
        np.testing.assert_allclose(location_sum, by_demand, rtol=1e-12, atol=1e-7)
        characterized = coefficients.multiply(inventory).tocsr()
        direct = float(characterized[~suppliers_are_market].sum())
        via_market = float(characterized[suppliers_are_market].sum())
        np.testing.assert_allclose(direct + via_market, score, rtol=1e-12, atol=1e-7)
        route_rows.append(
            {
                "year": year,
                "indicator": method["indicator"],
                "unit": method["unit"],
                "direct_supplier": direct,
                "market_supplier": via_market,
                "total": score,
                "positive_contributions": float(
                    characterized.data[characterized.data > 0].sum()
                ),
                "negative_contributions": float(
                    characterized.data[characterized.data < 0].sum()
                ),
            }
        )
        checks.append(
            {
                "year": year,
                "indicator": method["indicator"],
                "score": score,
                "independent_score": float(independent_by_demand.sum()),
                "relative_difference": float(
                    abs(score - independent_by_demand.sum()) / max(abs(score), 1e-30)
                ),
                "verified_demand_count": len(records),
                "normwise_backward_error": error,
                "pathways_normwise_backward_error": pathway_error,
                **audit,
            }
        )
    frame = pd.concat(frames, ignore_index=True)
    if not np.isfinite(frame.value).all():
        raise ValueError("Nonfinite indicator result")
    frame.to_parquet(output / f"{year}.parquet", index=False)
    pd.DataFrame(route_rows).to_csv(output / f"{year}_supply_routes.csv", index=False)
    result = {
        "passed": True,
        "year": year,
        "checks": checks,
        "rows": len(frame),
        "elapsed_seconds": time.monotonic() - start,
        "supply_refinement": supply_refinement,
        "independent_supply_refinement": independent_refinement,
        "outputs_sha256": {
            f"{year}.parquet": digest(output / f"{year}.parquet"),
            f"{year}_supply_routes.csv": digest(output / f"{year}_supply_routes.csv"),
        },
    }
    write_json(output / f"{year}_validation.json", result)
    for name in names:
        (runtime / name).unlink()
    del lca, data, matrix, supply, independent_supply, inventory
    gc.collect()
    print(
        f"Verified {package.stem} / {year}: {len(checks)} indicators, {len(checks)*101} demand checks in {result['elapsed_seconds']:.1f}s",
        flush=True,
    )
    return result


def combine(base, output, registry, years):
    base_summary = verify_base(base)
    if years != YEARS:
        return
    frames, validations, routes = [], [], []
    for n in range(1, 5):
        scenario = f"SPS{n}_bas0"
        for year in years:
            directory = output / "scenarios" / scenario
            validation = json.loads((directory / f"{year}_validation.json").read_text())
            assert validation["passed"] and len(validation["checks"]) == len(
                registry["methods"]
            )
            for name, checksum in validation["outputs_sha256"].items():
                if digest(directory / name) != checksum:
                    raise ValueError("Changed validated metal output")
            frames.append(pd.read_parquet(directory / f"{year}.parquet"))
            validations.extend(
                dict(row, scenario=scenario) for row in validation["checks"]
            )
            routes.append(
                pd.read_csv(directory / f"{year}_supply_routes.csv").assign(
                    scenario=scenario
                )
            )
    metals = pd.concat(frames, ignore_index=True)
    metals.to_parquet(output / "refined_metal_results.parquet", index=False)
    pd.concat(routes, ignore_index=True).to_csv(
        output / "supply_routes.csv", index=False
    )
    pd.DataFrame(validations).to_csv(output / "independent_checks.csv", index=False)
    combined = pd.concat(
        [pd.read_parquet(base / "df_final.parquet"), metals], ignore_index=True
    )
    keys = [c for c in combined if c != "value"]
    if (
        combined.duplicated(keys).any()
        or combined.isna().any().any()
        or not np.isfinite(combined.value).all()
    ):
        raise ValueError("Invalid combined result grid")
    combined.to_parquet(output / "df_final.parquet", index=False)
    original = pd.read_parquet(base / "df_final.parquet")
    restored = pd.read_parquet(output / "df_final.parquet")
    pd.testing.assert_frame_equal(
        restored.iloc[: len(original)].reset_index(drop=True),
        original,
        check_dtype=False,
    )
    combined.to_csv(output / "df_final.csv", index=False)
    totals = combined.groupby(
        ["model", "scenario", "region", "year", "impact_category", "unit"],
        as_index=False,
    ).value.sum()
    totals.to_csv(output / "indicator_totals.csv", index=False)
    original_registry = json.loads((base / "method_registry.json").read_text())
    combined_registry = {
        **registry,
        "name": "ef31-relics-refined-metals",
        "methods": original_registry["methods"] + registry["methods"],
    }
    coverage_path = output / "equipment_coverage.csv"
    if coverage_path.exists():
        combined_registry["energy_equipment_boundary"] = {
            "definition": "Lifetime-attributed energy-specific equipment; excludes vehicle chassis/body, roads and general buildings. Ordinary upstream manufacturing infrastructure is retained.",
            "coverage": pd.read_csv(coverage_path).fillna("").to_dict("records"),
            "coverage_sha256": digest(coverage_path),
            "interpretation": "Scenario totals, not marginal policy additions or annual equipment purchases; unresolved equipment coverage is not zero demand.",
        }
    write_json(output / "method_registry.json", combined_registry)
    summary = {
        **base_summary,
        "finished_at": now(),
        "indicators": len(combined_registry["methods"]),
        "rows": len(combined),
        "independent_checks": base_summary["independent_checks"] + len(validations),
        "base_results": str(base),
        "base_results_sha256": base_summary["outputs_sha256"],
        "refined_metal_validation": {
            "checks": len(validations),
            "demand_checks": sum(r["verified_demand_count"] for r in validations),
            "max_relative_difference": max(
                r["relative_difference"] for r in validations
            ),
            "unmatched_eligible_edges": sum(
                r["unmatched_eligible_edges"] for r in validations
            ),
        },
        "outputs_sha256": {
            name: digest(output / name)
            for name in [
                "df_final.parquet",
                "df_final.csv",
                "indicator_totals.csv",
                "method_registry.json",
                "refined_metal_results.parquet",
                "supply_routes.csv",
                "independent_checks.csv",
            ]
        },
    }
    summary["refined_metal_validation"]["source_inventory_flags"] = [
        {
            "scenario": row["scenario"],
            "year": row["year"],
            "indicator": row["indicator"],
            **flag,
        }
        for row in validations
        for flag in row["nonpositive_reference_suppliers"]
    ]
    summary["refined_metal_validation"]["base_values_preserved_exactly"] = True
    summary["refined_metal_validation"]["families"] = {
        family: sum(m["family"] == family for m in registry["methods"])
        for family in sorted({m["family"] for m in registry["methods"]})
    }
    summary["scenario_summaries_scope"] = (
        "The inherited scenario summaries describe the 81 original EF 3.1 and RELICS indicators. New per-year validation records are in scenarios/<scenario>/<year>_validation.json."
    )
    summary["outputs_sha256"]["product_coverage.json"] = digest(
        output / "product_coverage.json"
    )
    if coverage_path.exists():
        summary["outputs_sha256"]["equipment_coverage.csv"] = digest(coverage_path)
    summary["outputs_sha256"]["provenance.json"] = digest(output / "provenance.json")
    assert len(combined) == 4 * 7 * 101 * 3 * summary["indicators"]
    write_json(output / "completion_summary.json", summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-results", type=Path, default=ROOT / "results/sps_base/indicators"
    )
    parser.add_argument(
        "--packages", type=Path, default=ROOT / "results/sps_base/build/packages"
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=ROOT / "configuration_file/refined_metal_inputs.yaml",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/sps_base/refined_metals"
    )
    parser.add_argument(
        "--scenarios", nargs="+", default=[f"SPS{n}_bas0" for n in range(1, 5)]
    )
    parser.add_argument("--years", nargs="+", type=int, default=YEARS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--equipment-coverage", type=Path)
    args = parser.parse_args()
    if len(set(args.scenarios)) != len(args.scenarios) or not set(args.scenarios) <= {
        f"SPS{n}_bas0" for n in range(1, 5)
    }:
        raise ValueError("Expected distinct SPS baseline scenario names")
    if args.years != sorted(set(args.years)) or not set(args.years) <= set(YEARS):
        raise ValueError("Expected distinct supported years in ascending order")
    base, output = args.base_results.resolve(), args.output.resolve()
    packages = sorted(args.packages.resolve().glob("*.zip"))
    mapping = args.mapping.resolve()
    equipment_coverage = (
        args.equipment_coverage.resolve() if args.equipment_coverage else None
    )
    if len(packages) != 4:
        raise ValueError("Expected four SPS baseline packages")
    verify_base(base)
    output.mkdir(parents=True, exist_ok=True)
    # Sources and the run signature must be fixed before any expensive work.
    runtime = setup_runtime(output)
    software = sources()
    signature = {
        "mapping_sha256": digest(mapping),
        "base_summary_sha256": digest(base / "completion_summary.json"),
        "packages": {p.name: digest(p) for p in packages},
        "base_demands_sha256": {
            str(p.relative_to(base)): digest(p)
            for p in sorted(base.glob("scenarios/*/*_demands.csv"))
        },
        "base_manifests_sha256": {
            str(p.relative_to(base)): digest(p)
            for p in sorted(base.glob("scenarios/*/*.manifest.json"))
        },
        "software": software,
    }
    if equipment_coverage:
        signature["equipment_coverage_sha256"] = digest(equipment_coverage)
    evidence = [
        ROOT / "results/sps_base/refined_metals/product_documentation.json",
        ROOT / "results/sps_base/refined_metals/candidate_products.json",
        ROOT.parent / "premise/premise/data/additional_inventories/lci-lithium.xlsx",
        ROOT.parent / "premise/premise/data/additional_inventories/lci-cobalt.xlsx",
    ]
    signature["factor_evidence_sha256"] = {
        str(p): digest(p) for p in evidence if p.exists()
    }
    provenance = output / "provenance.json"
    if provenance.exists() and json.loads(provenance.read_text()) != signature:
        raise ValueError("Run inputs or source changed; use another output directory")
    if args.resume and (output / "completion_summary.json").exists():
        complete = json.loads((output / "completion_summary.json").read_text())
        for name, checksum in complete["outputs_sha256"].items():
            if digest(output / name) != checksum:
                raise ValueError("Completed result checksum mismatch")
        print("Complete validated results already exist.")
        return
    write_json(provenance, signature)
    if equipment_coverage:
        shutil.copy2(equipment_coverage, output / "equipment_coverage.csv")
    registry = prepare(mapping, packages, output)
    shutil.copy2(mapping, output / mapping.name)
    snapshot = output / "software"
    snapshot.mkdir(exist_ok=True)
    for name in software["scripts_sha256"]:
        shutil.copy2(ROOT / "dev" / name, snapshot / name)
    progress_path = output / "progress.json"
    progress = {"status": "running", "started_at": now(), "completed": []}
    write_json(progress_path, progress)
    try:
        for scenario in args.scenarios:
            package = next(p for p in packages if p.stem.endswith(f"-stem-{scenario}"))
            directory = output / "scenarios" / scenario
            directory.mkdir(parents=True, exist_ok=True)
            manifest_path = (
                base / "scenarios" / scenario / f"results_{package.stem}.manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            if digest(package) != manifest["calculation"]["working_package_sha256"]:
                raise ValueError("Package differs from the validated Pathways run")
            demand_path = manifest_path.with_name(f"results_{package.stem}_demands.csv")
            demands = pd.read_csv(demand_path, float_precision="round_trip")
            verify_demands(package, demands)
            demand_copy = directory / "pathways_demands.csv"
            if demand_copy.exists() and digest(demand_copy) != digest(demand_path):
                raise ValueError("Pathways demand file changed")
            shutil.copy2(demand_path, demand_copy)
            for year in args.years:
                check_path = directory / f"{year}_validation.json"
                if check_path.exists():
                    if not args.resume:
                        raise FileExistsError("Existing results require --resume")
                    check = json.loads(check_path.read_text())
                    for name, checksum in check["outputs_sha256"].items():
                        if digest(directory / name) != checksum:
                            raise ValueError("Cached output checksum mismatch")
                else:
                    calculate_year(
                        package,
                        year,
                        demands,
                        manifest,
                        registry["methods"],
                        runtime,
                        directory,
                    )
                progress["completed"].append(f"{scenario}/{year}")
                write_json(progress_path, progress)
        if sources() != software:
            raise ValueError("Calculation source changed during the run")
        if len(args.scenarios) == 4:
            combine(base, output, registry, args.years)
        progress.update(
            status=(
                "complete"
                if len(args.scenarios) == 4 and args.years == YEARS
                else "partial"
            ),
            finished_at=now(),
        )
        write_json(progress_path, progress)
    except Exception as exc:
        progress.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        write_json(progress_path, progress)
        raise


if __name__ == "__main__":
    main()
