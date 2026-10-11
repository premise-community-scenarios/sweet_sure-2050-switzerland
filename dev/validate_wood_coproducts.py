"""Independently check split production, exported shares and joint allocation."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
import yaml

try:
    from . import stem_wood_coproducts as wood
    from .compare_heat_pump_inventories import read_matrices
except ImportError:
    import stem_wood_coproducts as wood
    from compare_heat_pump_inventories import read_matrices


def check_inputs(current, previous, raw_audit, output):
    keys = ["model", "scenario", "region", "variables"]
    new = (
        pd.read_csv(current, float_precision="round_trip").set_index(keys).sort_index()
    )
    old = pd.read_csv(previous, float_precision="round_trip").set_index(keys)
    pd.testing.assert_frame_equal(new.loc[old.index], old, check_exact=True)
    assert new.index.is_unique and len(new) - len(old) == 6 * 40
    raw = pd.read_csv(raw_audit, float_precision="round_trip")
    raw = raw[raw.edition.eq("Latest") & raw.fuel.eq("Wood")]
    rows = []
    for r in raw.to_dict("records"):
        scenario, year = r["scenario"], r["year"]
        d = new.xs(("STEM", scenario, "CH"))
        value = lambda v: float(d.loc[v, str(year)])
        expected = {
            wood.ELECTRICITY + wood.CHP_ONLY: r["raw_electricity_PJ"],
            wood.HEAT + wood.CHP_ONLY: r["raw_heat_PJ"],
            wood.EFFICIENCY
            + wood.CHP_ONLY: r["raw_electricity_PJ"] / r["raw_total_fuel_PJ"],
            wood.BIOGAS_E: r["biogas_electricity_PJ"],
            wood.BIOGAS_H: r["biogas_heat_PJ"],
            wood.BIODIESEL_H: r["biodiesel_heat_PJ"],
        }
        for variable, amount in expected.items():
            assert np.isclose(value(variable), amount, rtol=1e-10, atol=1e-12), (
                scenario,
                year,
                variable,
            )
        for original, parts in (
            (wood.ELECTRICITY, [wood.ELECTRICITY + wood.CHP_ONLY, wood.BIOGAS_E]),
            (wood.HEAT, [wood.HEAT + wood.CHP_ONLY, wood.BIOGAS_H, wood.BIODIESEL_H]),
            ("Domestic biogas production", [wood.RESIDUAL_BIOGAS, wood.BIOGAS]),
        ):
            assert np.isclose(
                value(original), sum(value(v) for v in parts), rtol=1e-10, atol=1e-12
            )
        assert np.isclose(
            value(wood.FUEL_E + wood.CHP_ONLY) + value(wood.FUEL_H + wood.CHP_ONLY),
            r["raw_total_fuel_PJ"],
            rtol=1e-10,
        )
        rows.append(
            {
                "scenario": scenario,
                "year": year,
                "chp_electric_efficiency": value(wood.EFFICIENCY + wood.CHP_ONLY),
                "combined_chp_efficiency": r["raw_combined_output_over_input"],
                "below_1pct": r["raw_combined_output_over_input"] < 0.01,
                "above_110pct": r["raw_combined_output_over_input"] > 1.1,
            }
        )
    pd.DataFrame(rows).to_csv(output / "remaining_chp_source_flags.csv", index=False)
    result = {
        "original_rows_preserved_exactly": len(old),
        "derived_rows": len(new) - len(old),
        "scenario_year_cases": len(rows),
        "source_quantity_checks": len(rows) * 6,
        "energy_total_checks": len(rows) * 3,
        "low_chp_efficiency_cases_preserved": sum(r["below_1pct"] for r in rows),
        "high_chp_efficiency_cases_preserved": sum(r["above_110pct"] for r in rows),
    }
    (output / "input_validation.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def check_package(package):
    manifest = json.loads(package.with_suffix(".build.json").read_text())
    config = yaml.safe_load(Path(manifest["resources"]["config"]["path"]).read_text())
    values = wood.ScenarioValues(
        manifest["resources"]["scenario_data"]["path"], manifest["scenario"]
    )
    records = json.loads(package.with_suffix(".wood_coproducts.json").read_text())
    with ZipFile(package) as z:
        mapping = yaml.safe_load(z.read("mapping/mapping.yaml"))
    results = []
    for year in manifest["years"]:
        matrices = read_matrices(package, year)
        ai, a, b = (matrices[n] for n in ("A_matrix_index", "A_matrix", "B_matrix"))

        def index(meta):
            mask = np.ones(len(ai), dtype=bool)
            for column in ("name", "reference product", "unit", "location"):
                mask &= ai[column].eq(meta[column]).to_numpy()
            selected = ai.loc[mask, "index"].tolist()
            assert len(selected) == 1, (meta, selected)
            return selected[0]

        by_path = {r["pathway"]: r for r in records if r["year"] == year}
        assert set(by_path) == set(wood.PROVIDERS)
        for pathway, spec in wood.PROVIDERS.items():
            r = by_path[pathway]
            provider, basis = index(r["provider"]), index(r["basis"])
            inputs = a[a["index of activity"].eq(provider) & a.flip.eq(1)]
            assert len(inputs) == 1 and int(inputs.iloc[0]["index of product"]) == basis
            assert b[b["index of activity"].eq(provider)].empty
            if spec[3] == "FT":
                fuel, electricity, heat = (
                    values.value(wood.BIODIESEL, year),
                    0,
                    values.value(wood.BIODIESEL_H, year),
                )
                lhv = 43.3
            else:
                fuel, electricity, heat = (
                    values.value(v, year)
                    for v in (wood.BIOGAS, wood.BIOGAS_E, wood.BIOGAS_H)
                )
                lhv = 34.97
            alpha = (
                fuel / (fuel + electricity + heat) if fuel + electricity + heat else 1
            )
            unit_factor = (
                1
                if spec[4] == "fuel"
                else (3.6 if spec[2] == "kilowatt hour" else 1) / lhv
            )
            actual = float(inputs.iloc[0].value)
            assert np.isclose(actual, alpha * unit_factor, rtol=1e-10, atol=1e-15)
            results.append(
                {
                    "year": year,
                    "pathway": pathway,
                    "exported_source_amount": actual,
                    "expected_source_amount": alpha * unit_factor,
                }
            )
        for process in ("FT", "gas"):
            group = [(p, s) for p, s in wood.PROVIDERS.items() if s[3] == process]
            fuel_record = next(by_path[p] for p, s in group if s[4] == "fuel")
            outputs = fuel_record["outputs_PJ"]
            lhv = {"FT": 43.3, "gas": 34.97}[process]
            weighted = 0
            for path, spec in group:
                demand = outputs[spec[4]] / (
                    lhv
                    if spec[4] == "fuel"
                    else 3.6 if spec[2] == "kilowatt hour" else 1
                )
                provider = index(by_path[path]["provider"])
                amount = a.loc[
                    a["index of activity"].eq(provider) & a.flip.eq(1), "value"
                ].sum()
                weighted += demand * amount
            assert np.isclose(weighted, outputs["fuel"] / lhv, rtol=1e-10, atol=1e-15)
        for market_name in (
            "market for electricity, high voltage (SPS)",
            "market for district heating (SPS)",
            "market for methane (SPS)",
        ):
            market = next(m for m in config["markets"] if m["name"] == market_name)
            mi = int(
                ai.loc[ai.name.eq(market_name) & ai.location.eq("CH"), "index"].item()
            )
            denominator = sum(
                values.value(
                    config["production pathways"][p]["production volume"]["variable"],
                    year,
                )
                for p in market["includes"]
            )
            for pathway in (
                set(wood.PROVIDERS)
                | {"biomass (DH)", "DH_CHP_wood", "biomethane (domestic)"}
            ) & set(market["includes"]):
                alias = mapping[f"EXT - 0 - {pathway}"]["dataset"][0]
                ids = ai.loc[
                    ai.name.eq(alias["name"])
                    & ai["reference product"].eq(alias["reference product"]),
                    "index",
                ]
                amount = a.loc[
                    a["index of activity"].eq(mi)
                    & a["index of product"].isin(ids)
                    & a.flip.eq(1),
                    "value",
                ].sum()
                expected = (
                    values.value(
                        config["production pathways"][pathway]["production volume"][
                            "variable"
                        ],
                        year,
                    )
                    / denominator
                )
                assert np.isclose(amount, expected, rtol=1e-9, atol=1e-12), (
                    year,
                    market_name,
                    pathway,
                    amount,
                    expected,
                )
    result = {
        "scenario": manifest["scenario"],
        "years": manifest["years"],
        "allocation_provider_checks": len(results),
        "joint_burden_conservation_checks": 2 * len(manifest["years"]),
        "provider_checks": results,
        "market_shares_verified": True,
    }
    package.with_suffix(".wood_coproduct_validation.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    return result


def check_process_bases(package, reference):
    """Compare the unallocated Swiss process to its previously used inventory.

    Unlike checking the allocation coefficient alone, this catches accidental
    supplier changes during creation/regionalization of a dedicated basis.
    """
    records = json.loads(package.with_suffix(".wood_coproducts.json").read_text())
    checks = []

    def signature(matrices, name, product):
        ai, bi = matrices["A_matrix_index"], matrices["B_matrix_index"].fillna(
            "unspecified"
        )
        a, b = matrices["A_matrix"], matrices["B_matrix"]
        idx = ai.loc[
            ai.name.eq(name)
            & ai["reference product"].eq(product)
            & ai.location.eq("CH"),
            "index",
        ].item()
        amap = {
            r["index"]: tuple(
                r[k] for k in ("name", "reference product", "unit", "location")
            )
            for r in ai.to_dict("records")
        }
        bmap = {
            r["index"]: tuple(
                r[k] for k in ("name", "compartment", "subcompartment", "unit")
            )
            for r in bi.to_dict("records")
        }
        sig = defaultdict(float)
        for row in a[a["index of activity"].eq(idx) & a.flip.eq(1)].to_dict("records"):
            sig[("A", *amap[row["index of product"]])] += row["value"]
        for row in b[b["index of activity"].eq(idx)].to_dict("records"):
            sig[("B", *bmap[row["index of biosphere flow"]])] += row["value"]
        return sig

    for year in sorted({r["year"] for r in records}):
        previous, current = read_matrices(reference, year), read_matrices(package, year)
        seen = set()
        for record in (r for r in records if r["year"] == year):
            source, basis = record["source"], record["basis"]
            if basis["name"] in seen:
                continue
            seen.add(basis["name"])
            # A baseline made after the coproduct separation already has the
            # dedicated Swiss basis. Earlier baselines used the original alias.
            previous_index = previous["A_matrix_index"]
            has_basis = (
                previous_index.name.eq(basis["name"])
                & previous_index["reference product"].eq(basis["reference product"])
                & previous_index.location.eq("CH")
            ).any()
            reference_name = basis["name"] if has_basis else source["name"]
            old = signature(previous, reference_name, source["reference product"])
            new = signature(current, basis["name"], basis["reference product"])
            keys = old.keys() | new.keys()
            differences = [
                {"exchange": key, "previous": old.get(key, 0), "new": new.get(key, 0)}
                for key in keys
                if not np.isclose(
                    old.get(key, 0), new.get(key, 0), rtol=1e-10, atol=1e-15
                )
            ]
            assert not differences, (year, source["name"], differences)
            checks.append(
                {
                    "year": year,
                    "source": source["name"],
                    "basis": basis["name"],
                    "reference_activity": reference_name,
                    "exchanges_checked": len(keys),
                    "changed_exchanges": 0,
                }
            )
    package.with_suffix(".wood_process_basis_validation.json").write_text(
        json.dumps(checks, indent=2) + "\n"
    )
    return checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument(
        "--reference",
        type=Path,
        help="Prior package for checking unchanged physical process bases",
    )
    args = parser.parse_args()
    print(json.dumps(check_package(args.package), indent=2))
    if args.reference:
        print(json.dumps(check_process_bases(args.package, args.reference), indent=2))
