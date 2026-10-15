"""Resolve reviewed product definitions and audit every scenario-package index."""

import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
from zipfile import ZipFile

import yaml

if __package__:
    from .refined_metal_edges import edge_method
else:
    from refined_metal_edges import edge_method


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def content_fraction(formula, symbol, weights):
    tokens = re.findall(r"([A-Z][a-z]?)([0-9]*)", formula)
    if "".join(a + b for a, b in tokens) != formula:
        raise ValueError(f"Unsupported formula: {formula}")
    counts = {}
    for element, count in tokens:
        counts[element] = counts.get(element, 0) + int(count or 1)
    if symbol not in counts or any(count <= 0 for count in counts.values()):
        raise ValueError(f"Formula does not contain {symbol}: {formula}")
    return (
        weights[symbol]
        * counts[symbol]
        / sum(weights[e] * n for e, n in counts.items())
    )


def catalogue(packages):
    products, indices = {}, []
    for package in packages:
        with ZipFile(package) as archive:
            paths = sorted(
                n for n in archive.namelist() if n.endswith("/A_matrix_index.csv")
            )
            for path in paths:
                raw = archive.read(path)
                rows = list(csv.DictReader(io.StringIO(raw.decode()), delimiter=";"))
                record = {
                    "package": package.name,
                    "index": path,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "products": sorted({r["reference product"] for r in rows}),
                }
                indices.append(record)
                for r in rows:
                    p = products.setdefault(
                        r["reference product"],
                        {"units": set(), "suppliers": set(), "locations": set()},
                    )
                    p["units"].add(r["unit"])
                    p["suppliers"].add(r["name"])
                    p["locations"].add(r["location"])
    return {
        k: {f: sorted(v) for f, v in d.items()} for k, d in sorted(products.items())
    }, indices


def prepare(mapping_path, packages, output):
    config = yaml.load(Path(mapping_path).read_text(), Loader=UniqueLoader)
    products, indices = catalogue(packages)
    for element, reason in config.get("unavailable_elements", {}).items():
        if reason.startswith("No identified") and any(
            p.lower().startswith(element.lower()) for p in products
        ):
            raise ValueError(
                f"Claimed absent element has candidate products: {element}"
            )
    definitions, optional_absent = [], []
    for row in config["indicators"]:
        entries = []
        for product, item in row["products"].items():
            if not isinstance(item, dict):
                raise ValueError(f"Invalid product specification: {product}")
            if product not in products:
                if item.get("optional"):
                    optional_absent.append(product)
                    continue
                raise ValueError(f"Required product absent from packages: {product}")
            if products[product]["units"] != ["kilogram"]:
                raise ValueError(f"Unexpected reference units for {product}")
            factor = item.get("factor")
            if "formula" in item:
                if factor is not None:
                    raise ValueError(f"Ambiguous content specification: {product}")
                factor = content_fraction(
                    item["formula"], row["symbol"], config["atomic_weights"]
                )
            if factor is None or not math.isfinite(factor) or not 0 < factor <= 1:
                raise ValueError(f"Invalid factor for {product}: {factor}")
            entries.append(
                {
                    "product": product,
                    "factor": float(factor),
                    **item,
                    "basis": item.get(
                        "basis",
                        "Nominal chemical stoichiometry of the named substance; CIAAW 2024 weights",
                    ),
                }
            )
        if not entries:
            raise ValueError(f"Empty indicator: {row['name']}")
        definitions.append(
            {
                **row,
                "products": entries,
                "kind": "contained_metal",
                "internal_products": sorted(p["product"] for p in entries),
            }
        )
    for row in config["alloy_indicators"]:
        for p in row["products"]:
            if p not in products or products[p]["units"] != ["kilogram"]:
                raise ValueError(f"Missing or incompatible alloy product: {p}")
        definitions.append(
            {
                "name": row["name"],
                "symbol": "alloy",
                "kind": "alloy_mass",
                "products": [
                    {
                        "product": p,
                        "factor": 1.0,
                        "basis": "Whole alloy product mass; no elemental composition inferred",
                    }
                    for p in row["products"]
                ],
                "internal_products": sorted(row["products"]),
                "limitation": "Tonnes of alloy product, not tonnes of contained element. Overlaps with some upstream metal-input indicators; do not sum across indicators.",
            }
        )
    if len({d["name"] for d in definitions}) != len(definitions):
        raise ValueError("Duplicate indicator names")
    selected = {p["product"] for d in definitions for p in d["products"]}
    coverage = []
    for index in indices:
        present = set(index["products"])
        for d in definitions:
            matches = sorted(
                p["product"] for p in d["products"] if p["product"] in present
            )
            if not matches:
                raise ValueError(f"No product for {d['name']} in {index['index']}")
            coverage.append(
                {
                    "package": index["package"],
                    "index": index["index"],
                    "indicator": d["name"],
                    "present_products": matches,
                    "absent_products": sorted(set(d["internal_products"]) - present),
                }
            )
    methods = []
    method_dir = output / "methods"
    method_dir.mkdir(parents=True, exist_ok=True)
    for d in definitions:
        slug = d["name"].lower().replace(" ", "_")
        path = method_dir / f"{slug}.json"
        path.write_text(json.dumps(edge_method(d), indent=2) + "\n")
        family = "Alloy inputs" if d["kind"] == "alloy_mass" else "Refined-metal inputs"
        name = f"{family} - {d['name']}"
        methods.append(
            {
                "method": name,
                "method_tuple": [family, "selected product boundary", d["name"]],
                "indicator": name,
                "family": family,
                "label": d["name"],
                "unit": (
                    "kg alloy" if d["kind"] == "alloy_mass" else f"kg {d['symbol']}"
                ),
                "display_divisor": 1000,
                "display_unit": (
                    "tonnes alloy"
                    if d["kind"] == "alloy_mass"
                    else f"tonnes {d['symbol']}"
                ),
                "definition": d,
                "edges_method": str(path.relative_to(output)),
                "edges_method_sha256": digest(path),
                "limitation": d.get(
                    "limitation",
                    "Selected refined product forms; see the documented mapping and coverage inventory.",
                ),
                "interpretation": config["interpretation"],
            }
        )
    registry = {
        "name": config["name"],
        "version": config["version"],
        "mapping_sha256": digest(mapping_path),
        "methods": methods,
        "unavailable_elements": config["unavailable_elements"],
        "optional_products_absent": sorted(optional_absent),
        "boundary": config["boundary"],
        "interpretation": config["interpretation"],
        "sources": config["sources"],
        "coverage_limits": config.get("coverage_limits", []),
    }
    (output / "method_registry.json").write_text(json.dumps(registry, indent=2) + "\n")
    (output / "product_coverage.json").write_text(
        json.dumps(
            {
                "checks": coverage,
                "selected_products": {p: products[p] for p in sorted(selected)},
                "unselected_products": {
                    p: d for p, d in products.items() if p not in selected
                },
                "indices": [
                    {k: v for k, v in i.items() if k != "products"} for i in indices
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return registry
