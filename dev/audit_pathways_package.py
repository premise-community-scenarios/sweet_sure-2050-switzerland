"""Check exported matrix links and solve one Swiss electricity demand per year.

Usage: python dev/audit_pathways_package.py path/to/package.zip
"""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path, PurePosixPath
import zipfile

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
try:
    from scikits.umfpack import splu
except ImportError:
    from scipy.sparse.linalg import splu


class MatrixDirectory:
    def __init__(self, path):
        self.path = path

    def namelist(self):
        return [p.relative_to(self.path).as_posix() for p in self.path.rglob("*.csv")]

    def open(self, name):
        return (self.path / name).open("rb")


def audit(path):
    records = []
    context = (
        nullcontext(MatrixDirectory(path)) if path.is_dir() else zipfile.ZipFile(path)
    )
    with context as archive:
        if not path.is_dir():
            descriptor = json.loads(archive.read("datapackage.json"))
            for resource in descriptor["resources"]:
                assert resource["path"] in archive.namelist(), resource["path"]
        members = sorted(n for n in archive.namelist() if n.endswith("/A_matrix.csv"))
        assert members, "No exported matrices"
        for member in members:
            directory = str(PurePosixPath(member).parent)

            def read(name):
                with archive.open(f"{directory}/{name}.csv") as stream:
                    # premise can emit an empty row for an unresolved biosphere
                    # exchange. Preserve it so the index/finite checks reject it.
                    return pd.read_csv(stream, sep=";", skip_blank_lines=False)

            ai, bi = read("A_matrix_index"), read("B_matrix_index")
            a, b = read("A_matrix"), read("B_matrix")
            assert ai["index"].is_unique and bi["index"].is_unique
            assert set(ai["index"]) == set(range(len(ai)))
            assert set(bi["index"]) == set(range(len(bi)))
            assert not ai.duplicated(
                ["name", "reference product", "unit", "location"]
            ).any()
            for data, supplier_col, suppliers in [
                (a, "index of product", ai),
                (b, "index of biosphere flow", bi),
            ]:
                assert data["index of activity"].isin(ai["index"]).all()
                assert data[supplier_col].isin(suppliers["index"]).all()
                assert np.isfinite(data["value"]).all()

            production = a.loc[a["flip"].eq(0)]
            assert set(production["index of activity"]) == set(ai["index"])
            assert production["index of activity"].is_unique
            assert (
                production["index of activity"].eq(production["index of product"]).all()
            )
            assert production["value"].ne(0).all()
            values = a["value"].to_numpy() * np.where(a["flip"].eq(1), -1, 1)
            matrix = coo_matrix(
                (values, (a["index of product"], a["index of activity"])),
                shape=(len(ai), len(ai)),
            ).tocsc()
            demand_candidates = ai.loc[
                ai["name"].eq("market for electricity, high voltage (SPS)")
                & ai["location"].eq("CH")
                & ai["unit"].eq("kilowatt hour")
            ]
            assert len(demand_candidates) == 1, demand_candidates.to_dict("records")
            demand = np.zeros(len(ai))
            demand[int(demand_candidates.iloc[0]["index"])] = 1
            factorization = splu(matrix)
            supply = factorization.solve(demand)
            assert np.isfinite(supply).all()
            residual = float(np.max(np.abs(matrix @ supply - demand)))
            assert residual < 1e-7, residual
            biosphere = coo_matrix(
                (b["value"], (b["index of biosphere flow"], b["index of activity"])),
                shape=(len(bi), len(ai)),
            ).tocsr()
            inventory = biosphere @ supply
            assert np.isfinite(inventory).all()
            record = {
                "matrix_directory": directory,
                "year": int(PurePosixPath(directory).name),
                "activities": len(ai),
                "biosphere_flows": len(bi),
                "technosphere_entries": len(a),
                "biosphere_entries": len(b),
                "invalid_supplier_indices": 0,
                "nonfinite_exchange_amounts": 0,
                "missing_production_activities": 0,
                "demand": "1 kWh, Swiss SPS high-voltage electricity",
                "solve_max_absolute_residual": residual,
                "nonzero_inventory_flows": int(np.count_nonzero(inventory)),
            }
            records.append(record)
            print(json.dumps(record), flush=True)
            del factorization, matrix, biosphere, a, b
    return {"package": str(path.resolve()), "years": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    result = audit(args.package)
    output = args.package.with_suffix(".audit.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Audit passed: {output}")
