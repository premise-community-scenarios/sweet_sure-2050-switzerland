"""Independently match provider-isolation snapshots to exported A/B matrices."""

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

try:
    from dev.compare_heat_pump_inventories import read_matrices
except ModuleNotFoundError:
    from compare_heat_pump_inventories import read_matrices


def expected_signature(exchanges):
    signature = defaultdict(float)
    for e in exchanges:
        if e["type"] == "technosphere":
            key = ("A", e["name"], e["product"], e["unit"], e["location"])
        elif e["type"] == "biosphere":
            categories = e["categories"]
            key = (
                "B",
                e["name"],
                categories[0],
                categories[1] if len(categories) > 1 else "unspecified",
                e["unit"],
            )
        else:
            continue
        signature[key] += e["amount"]
    return dict(signature)


def validate(package):
    manifest = json.loads(package.with_suffix(".build.json").read_text())
    records = json.loads((package.parent / "provider_isolation.json").read_text())
    config = yaml.safe_load(Path(manifest["resources"]["config"]["path"]).read_text())
    custom_markets = {m["name"] for m in config["markets"]}
    # Premise applies GAINS after external scenarios. Reconstruct those expected
    # pollutant reductions from the published mapping/data, rather than treating
    # them as changes caused by external efficiency scaling.
    import bw2data  # noqa: F401
    from premise.activity_maps import InventorySet
    from premise.data_collection import get_gains_IAM_data
    from premise.emissions import Emissions, EI_POLLUTANTS, fetch_mapping
    from premise.geomap import Geomap

    gains = get_gains_IAM_data(manifest["model"], "CLE")
    pollutants = fetch_mapping(EI_POLLUTANTS)
    geo = Geomap(model=manifest["model"])
    results = []
    for year in manifest["years"]:
        data = read_matrices(package, year)
        ai, bi = data["A_matrix_index"], data["B_matrix_index"].fillna("unspecified")
        a, b = data["A_matrix"], data["B_matrix"]
        sectors = {
            ds["name"]: sector
            for sector, datasets in InventorySet(ai.to_dict("records"))
            .generate_gains_mapping()
            .items()
            for ds in datasets
        }
        updater = object.__new__(Emissions)
        updater.year = year
        factors = updater.prepare_data(gains)
        gains_checks = 0
        amap = {
            tuple(r[k] for k in ("name", "reference product", "unit", "location")): r[
                "index"
            ]
            for r in ai.to_dict("records")
        }
        akeys = {v: ("A", *k) for k, v in amap.items()}
        bkeys = {
            r["index"]: (
                "B",
                r["name"],
                r["compartment"],
                r["subcompartment"],
                r["unit"],
            )
            for r in bi.to_dict("records")
        }
        exported = defaultdict(lambda: defaultdict(float))
        for row in a.loc[a.flip.eq(1)].to_dict("records"):
            exported[row["index of activity"]][akeys[row["index of product"]]] += row[
                "value"
            ]
        for row in b.to_dict("records"):
            exported[row["index of activity"]][
                bkeys[row["index of biosphere flow"]]
            ] += row["value"]
        selected = [r for r in records if r["year"] == year]
        assert selected
        checked_sources = set()
        checked_copies = set()
        for record in selected:
            assert (
                record["preserved_during_check"]
                and record["preserved_during_efficiency_adjustment"]
            )
            entries = [
                {"identity": record["source"], "exchanges": record["after_markets"]}
            ] + record["copies_after_markets"]
            for entry in entries:
                key = tuple(entry["identity"])
                expected = expected_signature(entry["exchanges"])
                sector = sectors.get(key[0])
                assert sector == sectors.get(record["source"][0]), (
                    "Isolated provider changed GAINS sector",
                    key,
                    sector,
                    sectors.get(record["source"][0]),
                )
                region = (
                    key[-1]
                    if key[-1] in factors.region.values
                    else geo.ecoinvent_to_iam_location(key[-1])
                )
                if sector and region in factors.region.values:
                    for flow in expected:
                        if flow[0] == "B" and flow[1] in pollutants:
                            factor = float(
                                factors.sel(
                                    region=region,
                                    pollutant=pollutants[flow[1]],
                                    sector=sector,
                                )
                            )
                            if 0 < factor < 1:
                                expected[flow] *= factor
                                gains_checks += 1
                actual = exported[amap[key]]
                for flow in expected.keys() | actual.keys():
                    assert np.isclose(
                        expected.get(flow, 0),
                        actual.get(flow, 0),
                        rtol=1e-9,
                        atol=1e-14,
                    ), (year, key, flow, expected.get(flow), actual.get(flow))
                (
                    checked_sources
                    if key == tuple(record["source"])
                    else checked_copies
                ).add(key)
        copy_ids = {amap[k] for k in checked_copies}
        consumers = a[a.flip.eq(1) & a["index of product"].isin(copy_ids)].merge(
            ai, left_on="index of activity", right_on="index"
        )
        background_markets = consumers[
            consumers.name.str.startswith(("market for", "market group for"))
            & ~consumers.name.isin(custom_markets)
        ]
        assert background_markets.empty, background_markets[
            ["name", "location", "index of product"]
        ].to_dict("records")
        results.append(
            {
                "year": year,
                "preserved_sources": len(checked_sources),
                "verified_copies": len(checked_copies),
                "post_external_gains_flow_checks": gains_checks,
                "background_market_links_to_copies": len(background_markets),
                "status": "passed",
            }
        )
    target = package.with_suffix(".provider_validation.json")
    target.write_text(json.dumps(results, indent=2) + "\n")
    return results


def compare_background_markets(reference, package):
    """Check that all background electricity markets keep the same supplier shares."""
    from zipfile import ZipFile

    manifest = json.loads(package.with_suffix(".build.json").read_text())
    config = yaml.safe_load(Path(manifest["resources"]["config"]["path"]).read_text())
    custom = {m["name"] for m in config["markets"]}
    records = []
    with ZipFile(reference) as before, ZipFile(package) as after:
        for year in manifest["years"]:
            signatures = []
            for archive in (before, after):
                (directory,) = [
                    str(Path(n).parent)
                    for n in archive.namelist()
                    if n.endswith(f"/{year}/A_matrix.csv")
                ]
                ai = pd.read_csv(
                    archive.open(directory + "/A_matrix_index.csv"), sep=";"
                )
                a = pd.read_csv(
                    archive.open(directory + "/A_matrix.csv"),
                    sep=";",
                    float_precision="round_trip",
                )
                keys = {
                    r["index"]: tuple(
                        r[k] for k in ("name", "reference product", "unit", "location")
                    )
                    for r in ai.to_dict("records")
                }
                markets = ai[
                    ai.name.str.startswith(("market for", "market group for"))
                    & ai["reference product"].str.startswith("electricity")
                    & ~ai.name.isin(custom)
                ]
                signature = {keys[i]: defaultdict(float) for i in markets["index"]}
                for r in a[a["index of activity"].isin(markets["index"])].to_dict(
                    "records"
                ):
                    signature[keys[r["index of activity"]]][
                        (keys[r["index of product"]], r["flip"])
                    ] += r["value"]
                signatures.append(signature)
            left, right = signatures
            assert left.keys() == right.keys(), (
                "Background electricity market set changed",
                year,
                left.keys() ^ right.keys(),
            )
            differences = []
            for key in left:
                for supplier in left[key].keys() | right[key].keys():
                    old, new = left[key].get(supplier, 0), right[key].get(supplier, 0)
                    if not np.isclose(old, new, rtol=1e-10, atol=1e-15):
                        differences.append(
                            {
                                "market": key,
                                "supplier": supplier,
                                "before": old,
                                "after": new,
                            }
                        )
            assert not differences, (year, differences[:10])
            records.append(
                {
                    "year": year,
                    "background_electricity_markets": len(left),
                    "changed_supplier_shares": 0,
                }
            )
    package.with_suffix(".background_validation.json").write_text(
        json.dumps(records, indent=2) + "\n"
    )
    return records


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("package", type=Path)
    p.add_argument(
        "--reference",
        type=Path,
        help="Prior package for checking unchanged background electricity shares",
    )
    args = p.parse_args()
    print(json.dumps(validate(args.package), indent=2))
    if args.reference:
        print(
            json.dumps(
                compare_background_markets(args.reference, args.package), indent=2
            )
        )
