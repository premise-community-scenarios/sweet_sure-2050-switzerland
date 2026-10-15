#!/usr/bin/env python3
"""Independent workbook round trips and equipment-coverage register.

Does not import the normalization helper. Checks saved amounts against source
service/production exchanges and compares the two final-energy workbooks.
"""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

import pandas as pd
import yaml

from update_vehicle_energy_inventories import (
    ROOT,
    read_workbook,
    vehicle_sources,
)


def key(e):
    return (
        e["name"],
        e.get("product", e.get("reference product")),
        e.get("location"),
        e["unit"],
    )


def quantities(ds):
    result = defaultdict(float)
    for e in ds["exchanges"]:
        if e["type"] == "technosphere":
            result[key(e)] += e["amount"]
    return result


def purchased_energy(ds):
    """Independent delivered-energy check using the project's fuel conventions."""
    n = ds["name"]
    mode = (
        "electricity"
        if n.startswith("electricity")
        else (
            "hydrogen"
            if n.startswith("hydrogen")
            else (
                "gas"
                if n.startswith(("compressed gas", "biomethane"))
                else "diesel" if "diesel" in n else "gasoline"
            )
        )
    )
    energy = 0.0
    for e in ds["exchanges"]:
        if e["type"] != "technosphere":
            continue
        p, unit = e.get("product", ""), e["unit"]
        lhv = None
        if (
            mode == "electricity"
            and p.startswith("electricity,")
            and unit == "kilowatt hour"
        ):
            lhv = 3.6
        elif (
            mode == "hydrogen"
            and (p == "hydrogen" or p.startswith("hydrogen,"))
            and unit == "kilogram"
        ):
            lhv = 120
        elif mode == "gas" and p.startswith(
            ("natural gas", "biomethane", "biogas", "methane")
        ):
            lhv = (
                36
                if unit == "cubic meter"
                else 46.5 if p.startswith("natural gas") else 47.5
            )
        elif mode == "gasoline" and unit == "kilogram":
            if p.startswith("ethanol"):
                lhv = 26.5
            elif (
                p.startswith(("petrol", "gasoline"))
                or e["name"] == "fuel supply for gasoline vehicles"
            ):
                lhv = 42.6
        elif (
            mode == "diesel"
            and p.startswith(("biodiesel", "diesel"))
            and unit == "kilogram"
        ):
            lhv = (
                38
                if p.startswith("biodiesel")
                and "Fischer" not in e["name"]
                and "synthetic" not in e["name"]
                else 43
            )
        if lhv is not None:
            energy += float(e["amount"]) * lhv
    return energy


def audit(root=ROOT, premise=None):
    folder = (
        premise or root.parent / "premise"
    ) / "premise/data/additional_inventories"
    sources = vehicle_sources(folder)
    index = {key(d): d for d in sources}
    checks, comparisons, coverage = [], [], []
    normalized = defaultdict(list)
    sweet = read_workbook(root / "inventories/lci-sweet_sure.xlsx")
    for path, datasets in [
        (root / "inventories/lci-sweet_sure.xlsx", sweet),
        (
            folder / "lci-final-energy.xlsx",
            read_workbook(folder / "lci-final-energy.xlsx"),
        ),
    ]:
        for ds in datasets:
            if "energy use source" not in ds:
                continue
            record = json.loads(ds["energy use source"])
            source = index[tuple(record["source"])]
            q = record["purchased MJ per source service"]
            actual = quantities(ds)
            expected = defaultdict(float)
            for component in record["retained equipment"]:
                supplier = tuple(component["supplier"])
                # Obtain the amount independently from the actual production
                # supplier, rather than accepting the copied audit coefficient.
                matching = []
                for link in source["exchanges"]:
                    if link["type"] != "technosphere" or key(link) not in index:
                        continue
                    production = index[key(link)]
                    for e in production["exchanges"]:
                        if e["type"] == "technosphere" and key(e) == supplier:
                            matching.append(e["amount"] * link["amount"])
                if len(matching) != 1:
                    raise ValueError(
                        f"Non-unique equipment source: {source['name']}, {supplier}"
                    )
                expected[supplier] += (
                    matching[0] * component.get("replacement correction factor", 1) / q
                )
            for supplier, expected_amount in expected.items():
                amount = actual.get(supplier, 0)
                checks.append(
                    {
                        "workbook": path.name,
                        "wrapper": ds["name"],
                        "source": source["name"],
                        "supplier": supplier[0],
                        "unit": supplier[3],
                        "expected per MJ": expected_amount,
                        "actual per MJ": amount,
                        "recovered per service": amount * q,
                        "passed": math.isclose(
                            amount, expected_amount, rel_tol=1e-11, abs_tol=1e-15
                        ),
                    }
                )
            banned = [
                e["name"]
                for e in ds["exchanges"]
                if e["type"] == "technosphere"
                and any(
                    t in e["name"].lower()
                    for t in (
                        "glider",
                        "market for road",
                        "road construction",
                        "maintenance, passenger car",
                        "maintenance, lorry",
                        "frame, blanks",
                    )
                )
            ]
            if banned:
                raise ValueError(
                    f"Common end-use equipment leaked into {ds['name']}: {banned}"
                )
            if ds["name"].startswith("electricity"):
                operating = sum(
                    e["amount"] * 3.6
                    for e in ds["exchanges"]
                    if e["type"] == "technosphere"
                    and e.get("product", "").startswith("electricity,")
                    and e["unit"] == "kilowatt hour"
                )
                if not math.isclose(operating, 1, rel_tol=1e-12):
                    raise ValueError(
                        f"Not one MJ operating electricity: {ds['name']} / {operating}"
                    )
            signature = {k: actual[k] for k in expected}
            energy = purchased_energy(ds)
            checks.append(
                {
                    "workbook": path.name,
                    "wrapper": ds["name"],
                    "source": source["name"],
                    "supplier": "__purchased_energy__",
                    "unit": "megajoule",
                    "expected per MJ": 1,
                    "actual per MJ": energy,
                    "recovered per service": energy * q,
                    "passed": math.isclose(energy, 1, rel_tol=1e-11, abs_tol=1e-14),
                }
            )
            normalized[(source["name"], ds["name"].split(",")[0])].append(
                (path.name, ds["name"], signature)
            )
    for entries in normalized.values():
        for left in entries:
            for right in entries:
                if left[0] >= right[0]:
                    continue
                same = left[2].keys() == right[2].keys() and all(
                    math.isclose(v, right[2][k], rel_tol=1e-11, abs_tol=1e-15)
                    for k, v in left[2].items()
                )
                comparisons.append({"left": left[1], "right": right[1], "passed": same})
    config = yaml.safe_load((root / "configuration_file/config.yaml").read_text())
    byname = {
        d["name"]: d for d in read_workbook(folder / "lci-final-energy.xlsx") + sweet
    }
    for pathway, value in config["production pathways"].items():
        if not pathway.startswith("FE_"):
            continue
        name = value["ecoinvent alias"]["name"]
        ds = byname.get(name)
        source = (
            json.loads(ds["energy use source"])
            if ds and "energy use source" in ds
            else None
        )
        if source:
            status = "scoped equipment included"
            detail = source["source"][0]
            limitations = sorted(
                {
                    r["coefficient limitation"]
                    for r in source["retained equipment"]
                    if "coefficient limitation" in r
                }
            )
            if limitations:
                detail += "; " + " ".join(limitations)
            if pathway.startswith("FE_other_transport"):
                status = "representative proxy; aggregate composition unresolved"
            elif pathway == "FE_coach_electricity":
                status = "urban electric bus proxy; native electric coach absent"
            elif pathway == "FE_light_duty_truck_gas":
                status = (
                    "Van compressed-gas proxy; native 3.5-tonne gas inventory absent"
                )
            elif pathway.startswith("FE_passenger_rail"):
                status = "freight-locomotive equipment proxy for passenger rail"
                detail += "; native freight shunting fuel is retained as a separately identifiable auxiliary; passenger relevance is unresolved"
            elif "tram" in pathway:
                status = "freight-locomotive electric equipment proxy for tram"
                detail += "; native component-separated tram inventory absent; whole tram and track intentionally excluded"
                detail += (
                    "; inherited freight shunting auxiliary remains a proxy limitation"
                )
            elif "motorcycle_hydrogen" in pathway:
                status = "fuel-cell car equipment proxy for hydrogen motorcycle"
                detail += "; native hydrogen motorcycle inventory absent; source per-MJ components used without an undocumented size divisor"
        elif ds and "aggregate electric equipment" in ds:
            aggregate = json.loads(ds["aggregate electric equipment"])
            status = "partial energy-component mix; residual and technology gaps"
            detail = (
                f"{aggregate['fraction with some identified components']:.1%} of energy has some identified components; "
                f"{aggregate['fraction without equipment']:.1%} has no added equipment. "
                + " ".join(aggregate["limitations"])
            )
        elif ds and "electric boiler equipment" in ds:
            boiler = json.loads(ds["electric boiler equipment"])
            status = (
                "dedicated electric-boiler hardware; material and technology proxies"
            )
            detail = boiler["model"] + "; " + " ".join(boiler["limitations"])
        elif "heat_pump_gas" in pathway:
            status = "heat-pump gas proxy; native equipment unresolved"
            detail = "Legacy natural-gas-boiler provider represents a gas-driven heat pump. Gas supply is regionalized to CH, but absorber/engine, heat-pump hardware and technology-specific allocation are unresolved."
        elif "heat_pump" in pathway:
            status = "heat-pump hardware included; technology proxy retained"
            detail = "Physical heat-provider equipment allocation; air-water process uses source brine-water hardware proxy"
        elif "CHP_fuel_cell" in pathway:
            status = "gas-engine CHP proxy; fuel-cell components unresolved"
            detail = "Existing heat provider uses natural-gas engine CHP hardware. This does not establish fuel-cell stack or reformer material quantities, even when scenario fuel efficiency is corrected."
        elif "hydrogen" in pathway and "transport" not in pathway:
            status = "hydrogen heat technology proxy; equipment requires review"
            detail = "Hydrogen boiler uses conventional gas-boiler hardware; hydrogen CHP uses the existing residential hydrogen-CHP provider. Specific burner, materials and scale suitability remain unresolved."
        elif (
            ds
            and len([e for e in ds["exchanges"] if e["type"] == "technosphere"]) == 1
            and next(e for e in ds["exchanges"] if e["type"] == "technosphere")
            .get("product", "")
            .startswith("electricity,")
        ):
            status = "equipment coverage gap"
            detail = "Operating electricity only; requires end-use equipment composition and lifetime throughput"
        elif "rail" in pathway or "tram" in pathway or "motorcycle_hydrogen" in pathway:
            status = "legacy transport proxy; equipment boundary unresolved"
            detail = "Requires component-separated energy equipment, not complete rolling stock or track"
        else:
            status = "linked energy-process equipment; source technology retained"
            detail = "Existing heat/conversion-provider equipment and allocation retained. The exported heat-path screen records identifiable hardware; presence alone does not certify technology suitability or complete coverage."
        coverage.append(
            {
                "pathway": pathway,
                "wrapper": name,
                "status": status,
                "basis": detail,
                "included boundary": "energy-specific equipment and upstream manufacture",
                "excluded boundary": "vehicle chassis/body, roads, general buildings",
            }
        )
    if not all(r["passed"] for r in checks + comparisons):
        raise ValueError("Vehicle inventory consistency checks failed")
    return checks, comparisons, coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/sps_updated/vehicle_audit"
    )
    parser.add_argument("--premise", type=Path, default=ROOT.parent / "premise")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks, comparisons, coverage = audit(premise=args.premise)
    for name, records in [
        ("equipment_round_trips", checks),
        ("cross_workbook_checks", comparisons),
        ("equipment_coverage", coverage),
    ]:
        pd.DataFrame(records).to_csv(args.output / f"{name}.csv", index=False)
    summary = {
        "equipment checks": sum(
            r["supplier"] != "__purchased_energy__" for r in checks
        ),
        "purchased-energy checks": sum(
            r["supplier"] == "__purchased_energy__" for r in checks
        ),
        "cross-workbook checks": len(comparisons),
        "final-energy pathways": len(coverage),
        "checks passed": True,
        "coverage status": pd.Series([r["status"] for r in coverage])
        .value_counts()
        .to_dict(),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
