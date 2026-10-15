"""Check premise's integrated vehicle-energy wrappers through both cache routes."""

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

from update_vehicle_energy_inventories import read_workbook, vehicle_sources

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--premise", type=Path, default=ROOT.parent / "premise")
    parser.add_argument("--base", default="b844a9d")
    args = parser.parse_args()
    sys.path.insert(0, str(args.premise.resolve()))
    from premise.vehicle_energy_use import (
        normalize,
        select_source,
        source_name,
        carrier,
        energy_per_unit,
        SOURCE_KEY,
        VehicleLookup,
    )
    from premise.utils import create_cache, load_cached_database
    from premise.inventory_store import CompactInventoryStore

    folder = args.premise / "premise/data/additional_inventories"
    workbook = folder / "lci-final-energy.xlsx"
    data = vehicle_sources(folder) + read_workbook(workbook)
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "vehicles.pickle"
        create_cache(deepcopy(data), path)
        for route in ["pickle", "compact"]:
            cached = load_cached_database(path)
            if route == "compact":
                cached = CompactInventoryStore(cached).materialize()
            lookup = VehicleLookup(cached)
            targets = [d for d in cached if SOURCE_KEY in d]
            assert len(targets) == 55
            for wrapper in targets:
                source = select_source(
                    lookup, source_name(wrapper["name"]), wrapper["location"]
                )
                record = normalize(wrapper, source, lookup)
                name = wrapper["name"]
                fuel = (
                    "electricity"
                    if name.startswith("electricity")
                    else (
                        "hydrogen"
                        if name.startswith("hydrogen")
                        else (
                            "gas"
                            if name.startswith(("compressed gas", "biomethane"))
                            else "diesel" if "diesel" in name else "gasoline"
                        )
                    )
                )
                energy = sum(
                    e["amount"] * energy_per_unit(e)
                    for e in wrapper["exchanges"]
                    if carrier(e) == fuel
                )
                assert math.isclose(energy, 1, rel_tol=1e-7), (route, name, energy)
                assert all(math.isfinite(e["amount"]) for e in wrapper["exchanges"])
                assert record["retained equipment"]
                for e in wrapper["exchanges"]:
                    if e["type"] == "technosphere":
                        assert not any(
                            t in e["name"].lower()
                            for t in [
                                "glider",
                                "chassis",
                                "market for road",
                                "maintenance,",
                                "maintenance and repair",
                            ]
                        ), e["name"]
                records.append(
                    {"route": route, "wrapper": name, "purchased_MJ": energy}
                )

        original = Path(temporary) / "baseline.xlsx"
        original.write_bytes(
            subprocess.check_output(
                [
                    "git",
                    "show",
                    args.base
                    + ":premise/data/additional_inventories/lci-final-energy.xlsx",
                ],
                cwd=args.premise,
            )
        )

        def semantic(ds):
            return {k: v for k, v in ds.items() if k not in ("sheet", "start", "end")}

        before = {d["name"]: semantic(d) for d in read_workbook(original)}
        after = {d["name"]: semantic(d) for d in read_workbook(workbook)}
        assert before.keys() == after.keys()
        changed = [name for name in before if before[name] != after[name]]
        assert len(changed) == 51
        existing = [n for n in changed if SOURCE_KEY in before[n]]
        assert existing == ['electricity, used in battery electric motorcycle']
        assert all(SOURCE_KEY in after[n] for n in changed)
    out = ROOT / "results/sps_updated/energy_equipment_integration"
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "base": args.base,
        "passed": len(records),
        "updated_datasets": len(changed),
        "unchanged_datasets": len(before) - len(changed),
        "changed_datasets": changed,
        "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
        "records": records,
    }
    (out / "runtime_checks.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ("records", "changed_datasets")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
