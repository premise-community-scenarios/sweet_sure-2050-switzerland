"""Audit default inventory biosphere migration without building IAM scenarios.

Run in the premise environment; PYTHONPATH can select a frozen premise revision.
Records retained/dropped biosphere exchanges and import messages for comparison.
"""

import argparse
import ast
from collections import Counter
from contextlib import redirect_stdout
import inspect
import io
import json
from pathlib import Path
import textwrap

import premise.new_database as module
from premise.inventory_imports import DefaultInventory, migrate_import_db


def inventory_paths():
    source = textwrap.dedent(
        inspect.getsource(module.NewDatabase._NewDatabase__import_inventories)
    )
    tree = ast.parse(source)
    node = next(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "filepaths" for t in n.targets)
    )
    paths = [
        (getattr(module, item.elts[0].id), ast.literal_eval(item.elts[1]))
        for item in node.elts
    ]
    selected = []
    for path, version in paths:
        if path in (
            module.FILEPATH_OIL_GAS_INVENTORIES,
            module.FILEPATH_BATTERIES_NMC_NCA_LFP,
            module.FILEPATH_BATTERIES_NMC622_532,
            module.FILEPATH_GRAPHITE,
        ):
            continue  # NewDatabase excludes these workbooks for ecoinvent 3.12.
        if path == module.FILEPATH_PHOTOVOLTAICS:
            selected.extend(
                [
                    (module.FILEPATH_PHOTOVOLTAICS_2026, "3.12"),
                    (module.FILEPATH_PHOTOVOLTAICS_2026_ELECTRICITY, "3.12"),
                    (module.FILEPATH_PHOTOVOLTAICS_CIGS, "3.7"),
                ]
            )
        else:
            selected.append((path, version))
    selected.append((module.FILEPATH_AFFORESTATION_INVENTORIES, "3.12"))
    return selected


def audit(output, names):
    records = []
    for path, version in inventory_paths():
        if names and path.name not in names:
            continue
        capture = io.StringIO()
        with redirect_stdout(capture):
            importer = DefaultInventory([], version, "3.12", path, "cutoff", True)
            before = {}
            for dataset in importer.import_db.data:
                identity = [
                    dataset.get(k)
                    for k in ("name", "reference product", "location", "unit")
                ]
                for exc in dataset["exchanges"]:
                    if exc["type"] == "biosphere":
                        index = len(before)
                        exc["_audit_index"] = index
                        before[index] = {
                            "activity": identity,
                            "before": {
                                k: v for k, v in exc.items() if k != "_audit_index"
                            },
                        }
            migrate_import_db(importer.import_db, version, "3.12")
            importer.add_biosphere_links()
            for dataset in importer.import_db.data:
                for exc in dataset["exchanges"]:
                    if exc["type"] == "biosphere":
                        before[exc["_audit_index"]]["after"] = {
                            k: v for k, v in exc.items() if k != "_audit_index"
                        }
        missing = sum("after" not in r for r in before.values())
        records.append(
            {
                "file": path.name,
                "version": version,
                "messages": capture.getvalue(),
                "exchanges": list(before.values()),
            }
        )
        print(
            f"{path.name}: {len(before)} biosphere exchanges, {missing} removed",
            flush=True,
        )
    result = {"premise_source": module.__file__, "inventories": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(output)


def compare(original, fixed):
    previous, current = [
        json.loads(p.read_text())["inventories"] for p in (original, fixed)
    ]
    assert [i["file"] for i in previous] == [i["file"] for i in current]
    changes = []
    for old, new in zip(previous, current):
        assert len(old["exchanges"]) == len(new["exchanges"])
        for a, b in zip(old["exchanges"], new["exchanges"]):
            assert a["activity"] == b["activity"] and a["before"] == b["before"]
            before, after = a.get("after"), b.get("after")
            if after is not None:
                assert after["amount"] == b["before"]["amount"], b
            if (before or {}).get("input") != (after or {}).get("input"):
                changes.append(
                    {
                        "file": new["file"],
                        "activity": b["activity"],
                        "source": b["before"],
                        "previous": before,
                        "corrected": after,
                    }
                )
    messages = {
        label: [
            line
            for i in inventories
            for line in i["messages"].splitlines()
            if "Could not find" in line
        ]
        for label, inventories in [("previous", previous), ("corrected", current)]
    }
    summary = {
        "inventories": len(current),
        "source_biosphere_exchanges": sum(len(i["exchanges"]) for i in current),
        "changed_flow_links": len(changes),
        "restored_exchanges": sum(x["previous"] is None for x in changes),
        "newly_removed_exchanges": sum(x["corrected"] is None for x in changes),
        "retained_amounts_unchanged": True,
        "changes_by_source_flow": dict(Counter(x["source"]["name"] for x in changes)),
        "unresolved_flow_messages": messages,
    }
    report = fixed.with_suffix(".comparison.json")
    report.write_text(json.dumps({**summary, "changes": changes}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--inventories", nargs="*")
    parser.add_argument(
        "--compare", type=Path, help="Compare with a previous audit after running."
    )
    args = parser.parse_args()
    audit(args.output, args.inventories)
    if args.compare:
        compare(args.compare, args.output)
