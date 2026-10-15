"""Apply real Edges rules to Pathways matrices without importing a BW database.

The metadata adapter follows Pathways' edges_matrix module. Only the selected
supplier rows are sent to CLIPS; matrix positions and the solved LCI are intact.
"""

from types import SimpleNamespace

import numpy as np
from scipy import sparse


def edge_method(definition):
    """Exact product/unit rules; specific zero rules define internal transfers."""
    exchanges = []
    internal = set(definition["internal_products"])
    for product in definition["products"]:
        supplier = {
            "matrix": "technosphere",
            "reference product": product["product"],
            "unit": "kilogram",
            "operator": "equals",
        }
        for consumer in sorted(internal):
            exchanges.append(
                {
                    "supplier": dict(supplier),
                    "consumer": {
                        "matrix": "technosphere",
                        "reference product": consumer,
                        "operator": "equals",
                    },
                    "value": 0.0,
                }
            )
        exchanges.append(
            {
                "supplier": supplier,
                "consumer": {"matrix": "technosphere"},
                "value": product["factor"],
            }
        )
    family = (
        "Alloy inputs"
        if definition.get("kind") == "alloy_mass"
        else "Refined-metal inputs"
    )
    return {
        "name": f"{family} - {definition['name']}",
        "version": "1.0",
        "unit": f"kg {definition['symbol']}",
        "description": (
            "Alloy product mass delivered outside its selected production and distribution boundary."
            if definition.get("kind") == "alloy_mass"
            else "Contained metal delivered outside its selected refining and distribution boundary."
        ),
        "strategies": ["map_exchanges"],
        "exchanges": exchanges,
    }


def independent_coefficients(matrix, nodes, definition):
    """Separate direct implementation for verifying the CLIPS result."""
    factors = {p["product"]: p["factor"] for p in definition["products"]}
    internal = set(definition["internal_products"])
    row, column, values = [], [], []
    matrix = matrix.tocsr()
    for supplier, node in nodes.items():
        if node["reference product"] not in factors or node["unit"] != "kilogram":
            continue
        start, end = matrix.indptr[supplier : supplier + 2]
        for consumer, amount in zip(matrix.indices[start:end], matrix.data[start:end]):
            if (
                supplier != consumer
                and amount < 0
                and nodes[int(consumer)]["reference product"] not in internal
            ):
                row.append(supplier)
                column.append(consumer)
                values.append(factors[node["reference product"]])
    return sparse.csr_matrix((values, (row, column)), shape=matrix.shape)


def characterize(matrix, nodes, supply, definition, *, all_edges=False, inventory=None):
    """Run CLIPS matching, CF evaluation and EdgeLCIA.lcia on solved supplies.

    ``nodes`` uses the actual matrix positions, not the original package IDs.
    Supply may be signed: credits stay signed and are reported separately.
    Positive off-diagonal A entries are not consumption edges in Edges.
    """
    from edges import EdgeLCIA
    from edges.matrix_builders import build_technosphere_edges_matrix

    products = {p["product"] for p in definition["products"]}
    selected = {i for i, n in nodes.items() if n["reference product"] in products}
    if any(nodes[i]["unit"] != "kilogram" for i in selected):
        raise ValueError("Selected metal product has a non-kilogram reference unit")
    nonpositive_reference = [
        dict(nodes[i], position=i, diagonal=float(matrix[i, i]))
        for i in sorted(selected)
        if matrix[i, i] <= 0
    ]
    if inventory is None:
        inventory = build_technosphere_edges_matrix(matrix, np.asarray(supply))
    # Build the mapping domain from A, including inactive but eligible edges.
    positions = set()
    for i in (nodes if all_edges else selected):
        start, end = matrix.indptr[i : i + 2]
        positions.update(
            (int(i), int(j))
            for j, v in zip(matrix.indices[start:end], matrix.data[start:end])
            if i != j and v < 0
        )
    state = SimpleNamespace(
        technosphere_matrix=matrix,
        inventory=sparse.csr_matrix((1, matrix.shape[1])),
    )
    engine = EdgeLCIA(demand={}, method=edge_method(definition), lca=state)
    engine.technosphere_edges = positions
    engine.technosphere_flow_matrix = inventory
    engine.biosphere_edges = set()
    engine.biosphere_flows = []
    engine.technosphere_flows = [dict(node, position=i) for i, node in nodes.items()]
    engine.position_to_technosphere_flows_lookup = nodes
    engine.reversed_activity = {i: i for i in nodes}
    engine.reversed_biosphere = {}
    engine._preprocess_lookups(
        restrict_supplier_positions_bio=set(),
        restrict_supplier_positions_tech={i for i, _ in positions},
        restrict_consumer_positions={j for _, j in positions},
    )
    if positions:
        engine.apply_strategies()
    else:
        # The public wrapper interprets an empty domain as an uninitialized
        # Brightway LCI. This domain has already been checked against A; use
        # its actual CLIPS adapter directly, retaining a valid explicit zero.
        from edges.rete.adapter import map_exchanges_clips

        map_exchanges_clips(engine)
    engine.evaluate_cfs()
    engine.lcia()
    if engine.duplicate_method_signature_groups:
        raise ValueError("Duplicate Edges rule signatures")
    unmatched = {
        (i, j) for i, j in engine.unprocessed_technosphere_edges if i in selected
    }
    if unmatched:
        raise ValueError(f"Edges failed to match {len(unmatched)} eligible exchanges")
    coefficients = engine.characterization_matrices["technosphere"]
    if coefficients is None:
        coefficients = sparse.csr_matrix(matrix.shape)
    coefficients = coefficients.tocsr()
    expected = independent_coefficients(matrix, nodes, definition)
    difference = coefficients - expected
    if difference.nnz and np.max(np.abs(difference.data)) > 1e-14:
        raise ValueError(
            "Edges characterization differs from the independent product-boundary check"
        )
    for record in nonpositive_reference:
        position = record["position"]
        record["signed_activity_scale"] = float(supply[position])
        record["characterized_deliveries"] = float(
            coefficients.getrow(position).multiply(inventory.getrow(position)).sum()
        )
    return (
        coefficients,
        float(engine.score),
        {
            "eligible_edges": sum(i in selected for i, _ in positions),
            "counted_edges": expected.nnz,
            "internal_edges": sum(i in selected for i, _ in positions) - expected.nnz,
            "supplier_activities": len(selected),
            "unmatched_eligible_edges": len(unmatched),
            "duplicate_rules": len(engine.duplicate_method_signature_groups),
            "nonpositive_reference_suppliers": nonpositive_reference,
        },
    )
