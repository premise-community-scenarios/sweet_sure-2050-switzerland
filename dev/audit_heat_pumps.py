"""Verify exported heat-pump suppliers, COPs, equipment and final-energy units."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
import yaml

try:
    from dev.stem_heat_pumps import (
        COP_VARIABLES,
        GRID_NAME,
        GRID_PRODUCT,
        HEAT_NAME,
        HEAT_PRODUCT,
        heat_name,
    )
except ModuleNotFoundError:
    from stem_heat_pumps import (
        COP_VARIABLES,
        GRID_NAME,
        GRID_PRODUCT,
        HEAT_NAME,
        HEAT_PRODUCT,
        heat_name,
    )


def validate_heat_pumps(package):
    manifest = json.loads(package.with_suffix(".build.json").read_text())
    metadata = json.loads(package.with_suffix(".heat_pumps.json").read_text())
    frame = pd.read_csv(
        manifest["resources"]["scenario_data"]["path"], float_precision="round_trip"
    )
    frame = frame[
        frame.scenario.eq(manifest["scenario"]) & frame.region.eq("CH")
    ].set_index("variables")
    input_years = sorted(int(c) for c in frame.columns if c.isdigit())
    records = []
    with ZipFile(package) as archive:
        (mapping_file,) = [n for n in archive.namelist() if n.endswith("mapping.yaml")]
        mapping = yaml.safe_load(archive.read(mapping_file))
        for year in manifest["years"]:
            (directory,) = [
                str(Path(n).parent)
                for n in archive.namelist()
                if n.endswith(f"/{year}/A_matrix.csv")
            ]

            def read(name):
                return pd.read_csv(
                    archive.open(f"{directory}/{name}.csv"),
                    sep=";",
                    float_precision="round_trip",
                )

            ai, bi, a, b = [
                read(n)
                for n in ["A_matrix_index", "B_matrix_index", "A_matrix", "B_matrix"]
            ]

            def find(name, product, location="CH", unit="megajoule"):
                return int(
                    ai.loc[
                        ai.name.eq(name)
                        & ai["reference product"].eq(product)
                        & ai.location.eq(location)
                        & ai.unit.eq(unit),
                        "index",
                    ].item()
                )

            def technology(index):
                return a[a["index of activity"].eq(index) & a.flip.eq(1)].merge(
                    ai, left_on="index of product", right_on="index"
                )

            def non_electric_signature(index):
                result = defaultdict(float)
                for row in technology(index).to_dict("records"):
                    if row["reference product"] != GRID_PRODUCT:
                        result[
                            (
                                "technosphere",
                                row["name"],
                                row["reference product"],
                                row["location"],
                                row["unit"],
                            )
                        ] += row["value"]
                flows = b[b["index of activity"].eq(index)].merge(
                    bi, left_on="index of biosphere flow", right_on="index"
                )
                for row in flows.to_dict("records"):
                    result[
                        (
                            "biosphere",
                            row["name"],
                            row["compartment"],
                            row["subcompartment"],
                            row["unit"],
                        )
                    ] += row["value"]
                return dict(result)

            grid = find(GRID_NAME, GRID_PRODUCT, unit="kilowatt hour")
            for pathway, variable in COP_VARIABLES.items():
                (record,) = [
                    r for r in metadata if r["year"] == year and r["pathway"] == pathway
                ]
                source_name, source_product, source_location, source_unit = record[
                    "source"
                ]
                assert (
                    source_name == HEAT_NAME
                    and source_product == HEAT_PRODUCT
                    and source_unit == "megajoule"
                )
                source = find(source_name, source_product, source_location, source_unit)
                target = find(heat_name(pathway), HEAT_PRODUCT)
                expected_cop = float(
                    np.interp(
                        year,
                        input_years,
                        frame.loc[variable, [str(y) for y in input_years]].to_numpy(
                            float
                        ),
                    )
                )
                assert np.isfinite(expected_cop) and expected_cop > 0
                inputs = technology(target)
                electricity = inputs[inputs["reference product"].eq(GRID_PRODUCT)]
                assert (
                    len(electricity) == 1
                    and electricity["index of product"].item() == grid
                )
                kwh = float(electricity.value.item())
                actual_cop = 1 / (kwh * 3.6)
                assert np.isclose(actual_cop, expected_cop, rtol=1e-12, atol=0)
                old, new = non_electric_signature(source), non_electric_signature(
                    target
                )
                assert old.keys() == new.keys(), (pathway, old.keys(), new.keys())
                for flow in old:
                    assert np.isclose(old[flow], new[flow], rtol=1e-12, atol=0), (
                        year,
                        pathway,
                        flow,
                    )
                production = a[a["index of activity"].eq(target) & a.flip.eq(0)]
                assert len(production) == 1 and production.value.item() == 1
                (exported,) = mapping["EXT - 0 - " + pathway]["dataset"]
                final_electricity = None
                if pathway == "DH_heat_pump":
                    assert exported["name"] == heat_name(pathway)
                    assert exported["reference product"] == HEAT_PRODUCT
                    dh_market = find(
                        "market for district heating (SPS)",
                        "heat, district or industrial, other than natural gas",
                    )
                    # A zero-production heat-pump pathway can be omitted from its
                    # market, but any exported exchange must target the dedicated process.
                    links = technology(dh_market)
                    hp = links[links["reference product"].eq(HEAT_PRODUCT)]
                    assert hp["index of product"].isin([target]).all()
                else:
                    wrapper = find(exported["name"], exported["reference product"])
                    links = technology(wrapper)
                    assert (
                        len(links) == 1 and links["index of product"].item() == target
                    )
                    heat = float(links.value.item())
                    assert np.isclose(heat, expected_cop, rtol=1e-12, atol=0)
                    final_electricity = heat * kwh * 3.6
                    assert np.isclose(final_electricity, 1, rtol=1e-12, atol=0)
                    direct = b[b["index of activity"].eq(wrapper)].merge(
                        bi, left_on="index of biosphere flow", right_on="index"
                    )
                    expected_direct = record["preserved_wrapper_biosphere"]
                    assert len(direct) == len(expected_direct)
                    for flow in expected_direct:
                        categories = flow["categories"]
                        matches = direct[
                            direct.name.eq(flow["name"])
                            & direct.compartment.eq(categories[0])
                            & direct.subcompartment.eq(
                                categories[1] if len(categories) > 1 else "unspecified"
                            )
                        ]
                        assert len(matches) == 1 and np.isclose(
                            matches.value.item(), flow["amount"], rtol=1e-12, atol=0
                        )
                records.append(
                    {
                        "year": year,
                        "pathway": pathway,
                        "expected_cop": expected_cop,
                        "exported_cop": actual_cop,
                        "operating_grid": GRID_NAME,
                        "preserved_non_electric_exchanges": len(old),
                        "operating_MJ_per_MJ_final_electricity": final_electricity,
                    }
                )
    result = {
        "passed": True,
        "checks": records,
        "heat_pump_checks": len(records),
        "final_electricity_conservation_checks": sum(
            r["operating_MJ_per_MJ_final_electricity"] is not None for r in records
        ),
    }
    package.with_suffix(".heat_pump_validation.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    result = validate_heat_pumps(parser.parse_args().package)
    print(json.dumps({k: v for k, v in result.items() if k != "checks"}, indent=2))
