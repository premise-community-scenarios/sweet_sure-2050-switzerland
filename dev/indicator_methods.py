"""Freeze the 16 EF 3.1 headline categories and the complete RELICS method set.

RELICS flow selection follows relics.utils.add_relics: case-insensitive name
prefix and exact compartment. Matching uses each package's actual biosphere
index, rather than carrying the ecoinvent 3.11 flow list into a 3.12 calculation.
"""

import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

import yaml

EF_HEADLINES = {
    "acidification",
    "climate change",
    "ecotoxicity: freshwater",
    "energy resources: non-renewable",
    "eutrophication: freshwater",
    "eutrophication: marine",
    "eutrophication: terrestrial",
    "human toxicity: carcinogenic",
    "human toxicity: non-carcinogenic",
    "ionising radiation: human health",
    "land use",
    "material resources: metals/minerals",
    "ozone depletion",
    "particulate matter formation",
    "photochemical oxidant formation: human health",
    "water use",
}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relics_methods(mappings, flows):
    """Return native Pathways methods and a record of every matching decision."""
    methods, audit = [], []
    names = [m["name"] for m in mappings]
    if not names or len(set(names)) != len(names):
        raise ValueError("Empty or duplicate RELICS indicators")
    for mapping in mappings:
        coefficients = {}
        patterns = []
        for specification in mapping["environmental flow"]:
            categories = tuple(specification["categories"].split("::"))
            matches = sorted(
                f
                for f in flows
                if f[0].lower().startswith(specification["name"].lower())
                and f[1:3] == categories
            )
            patterns.append(
                {"prefix": specification["name"], "matches": [f[0] for f in matches]}
            )
            for flow in matches:
                if flow[3] != specification["unit"] or flow[3] != "kilogram":
                    raise ValueError(f"Unexpected RELICS flow unit: {flow}")
                key = flow[:3]
                amount = float(specification["amount"])
                if key in coefficients and coefficients[key] != amount:
                    raise ValueError(f"Conflicting RELICS coefficients: {key}")
                coefficients[key] = amount
        if not coefficients:
            raise ValueError(f"No biosphere flow for RELICS {mapping['name']}")
        methods.append(
            {
                "name": ["RELICS", "metals extraction", mapping["name"]],
                "unit": "kilogram",
                "exchanges": [
                    {"name": key[0], "categories": list(key[1:]), "amount": amount}
                    for key, amount in sorted(coefficients.items())
                ],
            }
        )
        audit.append(
            {
                "indicator": mapping["name"],
                "patterns": patterns,
                "flow_count": len(coefficients),
            }
        )
    return methods, audit


def metadata(method):
    family, category = method["name"][:2]
    title = method["name"][2] if family == "RELICS" else category.capitalize()
    indicator = f"{family} - {method['name'][2] if family == 'RELICS' else category}"
    unit = method["unit"]
    divisor, display_unit = 1, unit
    if family == "RELICS":
        divisor, display_unit = 1000, "tonnes"
    elif category == "climate change":
        divisor, display_unit = 1e9, "Mt CO₂-eq."
    return {
        "method": " - ".join(method["name"]),
        "method_tuple": method["name"],
        "indicator": indicator,
        "family": family,
        "label": title,
        "unit": unit,
        "display_divisor": divisor,
        "display_unit": display_unit,
    }


def freeze_methods(cf_path, mapping_path, packages, output):
    """Extend only the batch's private Pathways CF file; return its registry."""
    original_hash = digest(cf_path)
    bundled = json.loads(cf_path.read_text())
    ef = [
        m for m in bundled if m["name"][0] == "EF v3.1" and m["name"][1] in EF_HEADLINES
    ]
    if len(ef) != 16 or {m["name"][1] for m in ef} != EF_HEADLINES:
        raise ValueError("Exactly 16 EF v3.1 headline categories are required")
    ef_flows = {
        m["name"][1]: {
            (
                e["name"],
                e["categories"][0],
                e["categories"][1] if len(e["categories"]) > 1 else "unspecified",
            )
            for e in m["exchanges"]
        }
        for m in ef
    }
    if any(not flows for flows in ef_flows.values()):
        raise ValueError("An EF headline method has no characterization factors")
    mappings = yaml.safe_load(mapping_path.read_text())
    reference, reference_audit = None, None
    checks = []
    for entry in packages:
        with zipfile.ZipFile(entry["package"]) as archive:
            paths = sorted(
                n for n in archive.namelist() if n.endswith("/B_matrix_index.csv")
            )
            if not paths:
                raise ValueError("Package has no biosphere indices")
            for path in paths:
                rows = csv.DictReader(
                    io.StringIO(archive.read(path).decode("utf-8")), delimiter=";"
                )
                flows = {
                    (r["name"], r["compartment"], r["subcompartment"], r["unit"])
                    for r in rows
                }
                flow_identities = {f[:3] for f in flows}
                for category, expected in ef_flows.items():
                    if missing := expected - flow_identities:
                        raise ValueError(
                            f"Unmatched EF factors for {category}: {sorted(missing)[:5]}"
                        )
                methods, audit = relics_methods(mappings, flows)
                if reference is None:
                    reference, reference_audit = methods, audit
                elif methods != reference:
                    raise ValueError(
                        f"RELICS flow coverage differs: {entry['scenario']} / {path}"
                    )
                checks.append(
                    {
                        "scenario": entry["scenario"],
                        "index": path,
                        "methods": len(methods),
                        "ef_matched_flow_counts": {
                            category: len(expected)
                            for category, expected in ef_flows.items()
                        },
                    }
                )
    if len(reference or []) != 65:
        raise ValueError("Expected the complete 65-indicator RELICS mapping")
    combined = [m for m in bundled if m["name"][0] != "RELICS"] + reference
    cf_path.write_text(json.dumps(combined, separators=(",", ":")) + "\n")
    registry = {
        "name": "ef31-relics",
        "ecoinvent_version": "3.12",
        "ef_scope": "16 headline categories, including long-term emissions",
        "relics_mapping_sha256": digest(mapping_path),
        "original_pathways_cf_sha256": original_hash,
        "cf_sha256": digest(cf_path),
        "methods": [metadata(m) for m in ef + reference],
        "relics_flow_matching": reference_audit,
        "package_coverage_checks": checks,
        "notes": [
            "RELICS measures resource extraction in kilograms using the upstream mapping factors.",
            "EF parent categories only; no subcategory double counting and no combined score.",
            "RELICS matching follows the upstream name-prefix and exact-compartment rules.",
        ],
    }
    (output / "method_registry.json").write_text(json.dumps(registry, indent=2) + "\n")
    return registry


def load_registry(path):
    data = json.loads(Path(path).read_text())
    rows = data["methods"]
    if len({r["method"] for r in rows}) != len(rows) or len(
        {r["indicator"] for r in rows}
    ) != len(rows):
        raise ValueError("Duplicate methods or indicator labels in registry")
    return data, {r["method"]: (r["indicator"], r["unit"]) for r in rows}
