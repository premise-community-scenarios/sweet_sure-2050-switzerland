"""Document Swiss heat routing to apply after providers are regionalized.

Only provider locations change. Equipment, allocation and heat coefficients are
retained; this does not resolve the legacy technology proxies.
"""

import argparse
import json
import yaml
from update_vehicle_energy_inventories import ROOT, read_workbook, write_blocks

KEY = "Swiss heat provider routing"


def build(write=False):
    config = yaml.safe_load((ROOT / "configuration_file/config.yaml").read_text())
    declared = {
        (d["name"], d["reference product"]) for d in config["regionalize"]["datasets"]
    }
    path = ROOT / "inventories/lci-sweet_sure.xlsx"
    datasets = read_workbook(path)
    changed, records = [], []
    for ds in datasets:
        if ds["location"] != "CH" or ds["unit"] != "megajoule":
            continue
        proof = json.loads(ds.get(KEY, "[]"))
        new = []
        restored = False
        for e in ds["exchanges"]:
            existing = [
                p
                for p in proof
                if (p["supplier"], p["product"], p["unit"])
                == (e.get("name"), e.get("product"), e.get("unit"))
            ]
            if existing:
                assert len(existing) == 1
                previous = existing[0]["previous_location"]
                restored |= e["location"] != previous
                e["location"] = previous
                continue
            if (
                e["type"] == "technosphere"
                and e["unit"] == "megajoule"
                and (e["name"], e["product"]) in declared
                and e["location"] != "CH"
            ):
                new.append(
                    {
                        "wrapper": ds["name"],
                        "wrapper_product": ds["reference product"],
                        "supplier": e["name"],
                        "product": e["product"],
                        "previous_location": e["location"],
                        "location": "CH",
                        "amount": e["amount"],
                        "unit": e["unit"],
                    }
                )
        if new or restored:
            proof.extend(new)
            ds[KEY] = json.dumps(proof)
            changed.append(ds)
        records.extend(proof)
    if write and changed:
        write_blocks(path, changed)
    output = ROOT / "results/sps_updated/stationary_routing.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, indent=2) + "\n")
    print(
        json.dumps(
            {
                "changed_wrappers": len(changed),
                "documented_links": len(records),
                "written": write,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    build(parser.parse_args().write)
