#!/usr/bin/env python3
"""Check actual exported MJ wrappers against scenario equipment back-calculations."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
import yaml


def reachable_activities(matrix, roots):
    """Conservative reachability, including signed inputs and substitution."""
    links = (
        matrix[
            matrix["index of activity"].ne(matrix["index of product"])
            & matrix.value.ne(0)
        ]
        .groupby("index of activity")["index of product"]
        .agg(list)
        .to_dict()
    )
    reached, pending = set(), list(roots)
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(i for i in links.get(current, []) if i not in reached)
    return reached


def audit(package):
    evidence = json.loads(package.with_suffix(".vehicle_energy.json").read_text())
    records = []
    inactive_routes = []
    emissions_checks = routing_checks = transport_endpoint_checks = 0
    with ZipFile(package) as archive:
        mapping = yaml.safe_load(archive.read("mapping/mapping.yaml"))
        demand = pd.read_csv(archive.open("scenario_data/scenario_data.csv"))
        for year in sorted({r["year"] for r in evidence}):
            (member,) = [
                n for n in archive.namelist() if n.endswith(f"/{year}/A_matrix.csv")
            ]
            a = pd.read_csv(archive.open(member), sep=";", float_precision="round_trip")
            nodes = pd.read_csv(
                archive.open(member.replace("A_matrix.csv", "A_matrix_index.csv")),
                sep=";",
            )
            active_roots = []
            selected = demand[demand.year.eq(year) & demand.region.eq("CH")]
            for row in selected[
                selected.variables.str.startswith("EXT - 0 - FE_")
                & selected.value.gt(0)
            ].to_dict("records"):
                assert row["unit"] == "PJ/yr."
                for dataset in mapping[row["variables"]]["dataset"]:
                    matches = nodes[
                        nodes.name.eq(dataset["name"])
                        & nodes["reference product"].eq(dataset["reference product"])
                        & nodes.unit.eq(dataset["unit"])
                        & nodes.location.eq("CH")
                    ]
                    assert len(matches) == 1
                    active_roots.append(matches["index"].item())
            assert active_roots
            reached = None

            def check_route(inputs, root, wrapper, carrier):
                nonlocal reached
                if (
                    inputs.name.str.contains("SPS", regex=False).all()
                    and inputs.location.eq("CH").all()
                ):
                    return
                if reached is None:
                    reached = reachable_activities(a, active_roots)
                assert (
                    root not in reached
                ), f"Active energy use bypasses Swiss scenario supply: {wrapper}/{year}"
                inactive_routes.append(
                    {
                        "year": year,
                        "wrapper": wrapper,
                        "carrier": carrier,
                        "reason": "No path from any positive final-energy demand, including signed and substitution links",
                        "suppliers": inputs[
                            ["name", "reference product", "location", "unit"]
                        ].to_dict("records"),
                    }
                )

            for variable, spec in mapping.items():
                if not variable.endswith(("FE_tram", "FE_trolleybus")) and not any(
                    token in variable
                    for token in (
                        "_cars_",
                        "_bus_",
                        "_coach_",
                        "_trolleybus_",
                        "_motorcycle_",
                        "_passenger_rail_",
                        "_freight_rail_",
                        "_tram_",
                        "_truck_",
                        "_other_transport_",
                    )
                ):
                    continue
                for dataset in spec["dataset"]:
                    matches = nodes[
                        nodes.name.eq(dataset["name"])
                        & nodes["reference product"].eq(dataset["reference product"])
                        & nodes.unit.eq("megajoule")
                        & nodes.location.eq("CH")
                    ]
                    assert len(matches) == 1, (
                        variable,
                        "missing unique Swiss endpoint",
                    )
                    transport_endpoint_checks += 1
            b = pd.read_csv(
                archive.open(member.replace("A_matrix.csv", "B_matrix.csv")), sep=";"
            )
            flows = pd.read_csv(
                archive.open(member.replace("A_matrix.csv", "B_matrix_index.csv")),
                sep=";",
            ).fillna("unspecified")
            fuel = flows.compartment.eq("air") & (
                flows.name.isin(
                    ["Carbon dioxide, fossil", "Carbon dioxide, non-fossil"]
                )
                | (
                    flows.subcompartment.eq("unspecified")
                    & flows.name.isin(
                        ["Sulfur dioxide", "Methane, fossil", "Methane, non-fossil"]
                    )
                )
            )
            distance_flows = set(flows.loc[~fuel, "index"])
            for record in evidence:
                if record["year"] != year or record["location"] != "CH":
                    continue
                matches = nodes[
                    nodes.name.eq(record["wrapper"])
                    & nodes.location.eq("CH")
                    & nodes.unit.eq("megajoule")
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"Non-unique exported wrapper: {record['wrapper']}"
                    )
                idx = matches["index"].item()
                inputs = a[a["index of activity"].eq(idx) & a.flip.eq(1)].merge(
                    nodes, left_on="index of product", right_on="index"
                )
                production = a[a["index of activity"].eq(idx) & a.flip.eq(0)]
                assert len(production) == 1 and production.value.item() == 1
                expected = defaultdict(float)
                for component in record["retained equipment"]:
                    name, product, location, unit = component["supplier"]
                    expected[(name, product, unit)] += (
                        component["amount per vehicle"]
                        * component["vehicle per source service"]
                        * component.get("replacement correction factor", 1)
                        / record["purchased MJ per source service"]
                    )
                for (name, product, unit), amount in expected.items():
                    actual = inputs[
                        inputs.name.eq(name)
                        & inputs["reference product"].eq(product)
                        & inputs.unit.eq(unit)
                    ].value.sum()
                    records.append(
                        dict(
                            year=year,
                            wrapper=record["wrapper"],
                            supplier=name,
                            expected=amount,
                            actual=float(actual),
                            passed=bool(
                                np.isclose(amount, actual, rtol=2e-6, atol=1e-14)
                            ),
                        )
                    )
                if record["wrapper"].startswith("electricity"):
                    electricity = inputs[
                        inputs["reference product"].str.startswith("electricity,")
                        & inputs.unit.eq("kilowatt hour")
                    ]
                    assert np.isclose(electricity.value.sum() * 3.6, 1, rtol=2e-6)
                    check_route(electricity, idx, record["wrapper"], "electricity")
                elif record["wrapper"].startswith(("hydrogen", "diesel", "petrol")):
                    carrier = record["wrapper"].split(",", 1)[0]
                    fuel_inputs = inputs[
                        inputs["reference product"].str.startswith(carrier)
                        & inputs.unit.eq("kilogram")
                        & ~inputs["reference product"].str.contains("tank")
                    ]
                    assert not fuel_inputs.empty, record["wrapper"]
                    check_route(fuel_inputs, idx, record["wrapper"], carrier)
                    lhv = {"hydrogen": 120, "diesel": 43, "petrol": 42.6}[carrier]
                    assert np.isclose(
                        fuel_inputs.value.sum() * lhv, 1, rtol=2e-6
                    ), record["wrapper"]
                banned = inputs.name.str.lower().str.contains(
                    "glider|market for road|road construction|frame, blanks", regex=True
                )
                assert not banned.any(), record["wrapper"]
                auxiliary_diesel = inputs[
                    inputs["reference product"].eq("diesel")
                    & inputs.unit.eq("kilogram")
                ]
                check_route(
                    auxiliary_diesel, idx, record["wrapper"], "auxiliary diesel"
                )
                # Compare final, post-GAINS biosphere amounts, not an earlier
                # copied metadata record. Fuel-carbon accounting has its own
                # SPS market basis; all other source emissions retain distance.
                name, product, location, unit = record["source"]
                source = nodes[
                    nodes.name.eq(name)
                    & nodes["reference product"].eq(product)
                    & nodes.location.eq(location)
                    & nodes.unit.eq(unit)
                ]
                assert len(source) == 1, record["source"]

                def biosphere(index):
                    return (
                        b[
                            b["index of activity"].eq(index)
                            & b["index of biosphere flow"].isin(distance_flows)
                        ]
                        .groupby("index of biosphere flow")
                        .value.sum()
                    )

                expected_b = (
                    biosphere(source["index"].item())
                    / record["purchased MJ per source service"]
                )
                actual_b = biosphere(idx)
                ids = expected_b.index.union(actual_b.index)
                assert np.allclose(
                    expected_b.reindex(ids, fill_value=0),
                    actual_b.reindex(ids, fill_value=0),
                    rtol=3e-6,
                    atol=1e-14,
                ), f"Final vehicle emissions differ: {record['wrapper']} / {year}"
                emissions_checks += len(ids)
            # CNG must reach the physical compression recipe and Swiss inputs;
            # a renamed forwarding market does not establish this condition.
            cng = nodes[
                nodes.name.eq("natural gas, vehicle grade, at fuelling station (SPS)")
                & nodes.location.eq("CH")
            ]
            assert len(cng) == 1
            inputs = a[
                a["index of activity"].eq(cng["index"].item()) & a.flip.eq(1)
            ].merge(nodes, left_on="index of product", right_on="index")
            gas = inputs[
                inputs.name.eq("market for methane (SPS)")
                & inputs["reference product"].eq("gas")
                & inputs.unit.eq("cubic meter")
            ]
            grid = inputs[
                inputs["reference product"].str.startswith("electricity,")
                & inputs.unit.eq("kilowatt hour")
            ]
            assert not gas.empty and not grid.empty
            assert (
                gas.name.str.contains("SPS", regex=False).all()
                and gas.location.eq("CH").all()
            )
            assert (
                grid.name.str.contains("SPS", regex=False).all()
                and grid.location.eq("CH").all()
            )
            routing_checks += 2
    frame = pd.DataFrame(records)
    frame.to_csv(package.with_suffix(".vehicle_checks.csv"), index=False)
    if frame.empty or not frame.passed.all():
        raise ValueError(
            f"Exported vehicle equipment failed: {frame[~frame.passed].to_dict('records')[:5]}"
        )
    summary = {
        "passed": True,
        "equipment checks": len(frame),
        "final biosphere checks": emissions_checks,
        "CNG routing checks": routing_checks,
        "Swiss transport endpoints": transport_endpoint_checks,
        "wrappers": frame.wrapper.nunique(),
        "inactive non-SPS routes": inactive_routes,
        "years": sorted(frame.year.unique().tolist()),
    }
    package.with_suffix(".vehicle_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    print(json.dumps(audit(parser.parse_args().package)))
