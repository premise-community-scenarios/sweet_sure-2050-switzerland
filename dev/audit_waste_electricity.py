"""Reconcile waste generation with raw STEM inputs and exported market matrices."""

import argparse
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
import yaml

from add_waste_electricity_inventories import VARIANTS, blocks
from audit_stem_input_chain import scenario_code, source_frame


def check_inputs(inputs, archive, before, output):
    config = yaml.safe_load((inputs / "configuration_file/config.yaml").read_text())
    frame = pd.read_csv(
        inputs / "scenario_data/scenario_data.csv", float_precision="round_trip"
    )
    keys = ["model", "scenario", "region", "variables"]
    previous = (
        pd.read_csv(
            before / "scenario_data/scenario_data.csv", float_precision="round_trip"
        )
        .set_index(keys)
        .sort_index()
    )
    current = frame.set_index(keys).sort_index()
    pd.testing.assert_frame_equal(
        previous, current.loc[previous.index], check_exact=True
    )
    assert len(current) - len(previous) == 160
    market = next(
        m
        for m in config["markets"]
        if m["name"] == "market for electricity, high voltage (SPS)"
    )
    variables = [
        config["production pathways"][p]["production volume"]["variable"]
        for p in market["includes"]
    ]
    assert len(variables) == len(set(variables))
    wastes = [v for v in variables if v.startswith("Electricity generation|Wastes|")]
    assert len(wastes) == 12
    records = []
    with ZipFile(archive) as bundle:
        members = sorted(
            n
            for n in bundle.namelist()
            if n.endswith(".xlsx") and not n.startswith("__MACOSX/")
        )
        assert len(members) == 40
        for n, member in enumerate(members, 1):
            scenario = scenario_code(member)
            raw, years, _ = source_frame(bundle.read(member))
            scenario_data = frame[
                frame.scenario.eq(scenario) & frame.region.eq("CH")
            ].set_index("variables")
            for year in years:
                for variable in wastes:
                    actual = float(scenario_data.at[variable, str(year)])
                    expected = float(raw.at[variable, year])
                    assert np.isclose(actual, expected, rtol=1e-12, atol=1e-14), (
                        scenario,
                        year,
                        variable,
                    )
                restored = sum(float(raw.at[v["variable"], year]) for v in VARIANTS)
                records.append(
                    {
                        "scenario": scenario,
                        "year": year,
                        "restored_PJ": restored,
                        "restored_TWh": restored / 3.6,
                        "waste_generation_PJ": sum(
                            float(raw.at[v, year]) for v in wastes
                        ),
                        "checked_waste_series": len(wastes),
                    }
                )
            print(f"Verified raw workbook {n}/40: {scenario}", flush=True)
    pd.DataFrame(records).to_csv(output / "raw_waste_conservation.csv", index=False)
    result = {
        "scenarios": 40,
        "years": 7,
        "raw_waste_value_checks": len(records) * len(wastes),
        "preexisting_rows_unchanged": len(previous),
        "restored_rows": 160,
        "duplicate_market_variables": 0,
    }
    (output / "input_validation.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def check_package(package, output):
    manifest = json.loads(package.with_suffix(".build.json").read_text())
    config = yaml.safe_load(Path(manifest["resources"]["config"]["path"]).read_text())
    frame = pd.read_csv(
        manifest["resources"]["scenario_data"]["path"], float_precision="round_trip"
    )
    frame = frame[
        frame.scenario.eq(manifest["scenario"]) & frame.region.eq("CH")
    ].set_index("variables")
    inventory = blocks(Path(manifest["resources"]["inventories"]["path"]))
    market = next(
        m
        for m in config["markets"]
        if m["name"] == "market for electricity, high voltage (SPS)"
    )
    # Read Premise's actual bindings: provider copies can have arbitrary names.
    with ZipFile(package) as archive:
        mapping = yaml.safe_load(archive.read("mapping/mapping.yaml"))
    aliases = {
        pathway: mapping[f"EXT - 0 - {pathway}"]["dataset"][0]["name"].lower()
        for pathway in config["production pathways"]
    }

    records, carbon = [], []
    with ZipFile(package) as bundle:
        for year in manifest["years"]:
            (directory,) = [
                str(Path(n).parent)
                for n in bundle.namelist()
                if n.endswith(f"/{year}/A_matrix.csv")
            ]

            def read(name):
                return pd.read_csv(
                    bundle.open(f"{directory}/{name}.csv"),
                    sep=";",
                    float_precision="round_trip",
                )

            ai, a, bi, b = (
                read(n)
                for n in ("A_matrix_index", "A_matrix", "B_matrix_index", "B_matrix")
            )
            market_id = ai.loc[
                ai.name.eq(market["name"]) & ai.location.eq("CH"), "index"
            ].item()
            suppliers = a[a["index of activity"].eq(market_id) & a.flip.eq(1)].merge(
                ai, left_on="index of product", right_on="index"
            )
            total = sum(
                float(
                    frame.at[
                        config["production pathways"][p]["production volume"][
                            "variable"
                        ],
                        str(year),
                    ]
                )
                for p in market["includes"]
            )
            for pathway in market["includes"]:
                settings = config["production pathways"][pathway]
                expected = (
                    float(
                        frame.at[settings["production volume"]["variable"], str(year)]
                    )
                    / total
                )
                matched = suppliers[
                    suppliers.name.str.lower().eq(aliases[pathway])
                    & suppliers.location.eq("CH")
                    & suppliers["reference product"].eq(
                        settings["ecoinvent alias"]["reference product"]
                    )
                ]
                assert len(matched) == (1 if expected > 0 else 0), (
                    year,
                    pathway,
                    matched,
                )
                actual = float(matched.value.sum())
                assert np.isclose(actual, expected, rtol=1e-9, atol=1e-12), (
                    year,
                    pathway,
                    expected,
                    actual,
                )
                records.append(
                    {
                        "year": year,
                        "pathway": pathway,
                        "expected_share": expected,
                        "exported_share": actual,
                    }
                )
            for spec in VARIANTS:
                (pathway,) = [
                    p
                    for p, v in config["production pathways"].items()
                    if v["ecoinvent alias"]["name"] == spec["name"]
                    and v["ecoinvent alias"]["reference product"] == spec["product"]
                ]
                activity = ai.loc[
                    ai.name.str.lower().eq(aliases[pathway])
                    & ai.location.eq("CH")
                    & ai["reference product"].eq(spec["product"]),
                    "index",
                ].item()
                source = inventory[spec["source"]]
                baseline = next(r[1] for r in source if r[0] == "current efficiency")
                target = float(frame.at[spec["efficiency"], str(year)])
                assert target > 0
                factor = baseline / (spec["fraction"] * target)
                flows = b[b["index of activity"].eq(activity)].merge(
                    bi, left_on="index of biosphere flow", right_on="index"
                )
                inputs = a[a["index of activity"].eq(activity) & a.flip.eq(1)].merge(
                    ai, left_on="index of product", right_on="index"
                )
                for row in source:
                    if row[5] in ("biosphere", "technosphere") and str(
                        row[0]
                    ).lower().startswith("carbon dioxide"):
                        expected = row[1] * factor
                        exchanges = flows if row[5] == "biosphere" else inputs
                        actual = float(
                            exchanges.loc[
                                exchanges.name.str.lower().eq(row[0].lower()), "value"
                            ].sum()
                        )
                        assert np.isclose(expected, actual, rtol=1e-9, atol=1e-12), (
                            year,
                            spec["name"],
                            row[0],
                            expected,
                            actual,
                        )
                        carbon.append(
                            {
                                "year": year,
                                "activity": spec["name"],
                                "flow": row[0],
                                "expected_kg_per_kWh": expected,
                                "exported_kg_per_kWh": actual,
                            }
                        )
    pd.DataFrame(records).to_csv(
        output / "exported_electricity_market_shares.csv", index=False
    )
    pd.DataFrame(carbon).to_csv(
        output / "exported_waste_carbon_scaling.csv", index=False
    )
    result = {
        "market_share_checks": len(records),
        "waste_carbon_scaling_checks": len(carbon),
        "years": manifest["years"],
    }
    (output / "package_waste_validation.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--package", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.inputs:
        print(
            json.dumps(
                check_inputs(args.inputs, args.archive, args.before, args.output),
                indent=2,
            )
        )
    if args.package:
        print(json.dumps(check_package(args.package, args.output), indent=2))
