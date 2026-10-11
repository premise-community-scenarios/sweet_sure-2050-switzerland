"""Read exported matrices directly for independent STEM workflow diagnostics."""

import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class MatrixAudit:
    def __init__(self, package, year, cf_path, methods, solver="umfpack"):
        with zipfile.ZipFile(package) as archive:
            candidates = [
                n for n in archive.namelist() if n.endswith(f"/{year}/A_matrix.csv")
            ]
            if len(candidates) != 1:
                raise ValueError("Ambiguous matrix year")
            base = str(Path(candidates[0]).parent)

            def read(name):
                return pd.read_csv(
                    archive.open(f"{base}/{name}.csv"),
                    sep=";",
                    float_precision="round_trip",
                )

            self.ai, self.bi = read("A_matrix_index"), read("B_matrix_index")
            self.a, self.b = read("A_matrix"), read("B_matrix")
        for index in [self.ai, self.bi]:
            assert index["index"].is_unique
            assert sorted(index["index"].tolist()) == list(range(len(index)))
        self.ai = self.ai.set_index("index").sort_index()
        self.bi = self.bi.set_index("index").sort_index()
        assert not self.ai.duplicated(
            ["name", "reference product", "unit", "location"]
        ).any()
        a, b = self.a, self.b
        for frame, supplier, size in [
            (a, "index of product", len(self.ai)),
            (b, "index of biosphere flow", len(self.bi)),
        ]:
            assert np.isfinite(frame.value).all()
            assert frame["index of activity"].between(0, len(self.ai) - 1).all()
            assert frame[supplier].between(0, size - 1).all()
        prod = a[a.flip.eq(0)]
        assert len(prod) == len(self.ai)
        assert prod["index of product"].eq(prod["index of activity"]).all()
        # Waste-treatment functional units can have negative production.
        assert prod["index of activity"].is_unique and prod.value.ne(0).all()
        self.A = coo_matrix(
            (
                a.value * np.where(a.flip.eq(1), -1.0, 1.0),
                (a["index of product"], a["index of activity"]),
            ),
            shape=(len(self.ai), len(self.ai)),
        ).tocsr()
        self.B = coo_matrix(
            (b.value, (b["index of biosphere flow"], b["index of activity"])),
            shape=(len(self.bi), len(self.ai)),
        ).tocsr()
        registry = {
            " - ".join(m["name"]): m for m in json.loads(Path(cf_path).read_text())
        }
        flows = {
            (r["name"], r["compartment"], r["subcompartment"]): i
            for i, r in self.bi.iterrows()
        }
        assert len(flows) == len(self.bi)
        self.C = np.zeros((len(methods), len(self.bi)))
        self.cf_audit = []
        for j, method in enumerate(methods):
            seen = set()
            for exchange in registry[method]["exchanges"]:
                key = (
                    exchange["name"],
                    exchange["categories"][0],
                    (
                        exchange["categories"][1]
                        if len(exchange["categories"]) > 1
                        else "unspecified"
                    ),
                )
                assert key not in seen
                seen.add(key)
                if key in flows:
                    self.C[j, flows[key]] = exchange["amount"]
            self.cf_audit.append(
                {
                    "method": method,
                    "matched_nonzero_cfs": int(np.count_nonzero(self.C[j])),
                }
            )
        self.direct = np.asarray(self.C @ self.B)
        self.solver = solver
        if solver == "umfpack":
            from scikits.umfpack import splu as umf_splu

            self.factor = umf_splu(self.A)
        else:
            self.factor = splu(self.A.tocsc())
        self.intensities = None

    def find(self, name, product=None, unit=None, location=None):
        mask = self.ai.name.eq(name)
        for column, value in [
            ("reference product", product),
            ("unit", unit),
            ("location", location),
        ]:
            if value is not None:
                mask &= self.ai[column].eq(value)
        matches = self.ai.index[mask].tolist()
        if len(matches) != 1:
            raise ValueError(
                f"Non-unique supplier: {name}, {product}, {unit}, {location}: {matches}"
            )
        return matches[0]

    def solve(self, demands):
        supply = self.factor.solve(demands)
        assert np.isfinite(supply).all()
        error = np.max(np.abs(self.A @ supply - demands), axis=0)
        denominator = float(abs(self.A).sum(axis=1).max()) * np.max(
            np.abs(supply), axis=0
        ) + np.max(np.abs(demands), axis=0)
        backward = np.divide(
            error, denominator, out=np.zeros_like(error), where=denominator != 0
        )
        assert np.max(backward) < 1e-12
        return supply, self.C @ (self.B @ supply), backward

    def upstream_intensities(self):
        if self.intensities is None:
            if self.solver == "umfpack":
                from scikits.umfpack import UMFPACK_At

                self.intensities = np.array(
                    [
                        self.factor.umf.solve(
                            UMFPACK_At, self.factor._A, row, autoTranspose=True
                        )
                        for row in self.direct
                    ]
                )
            else:
                self.intensities = self.factor.solve(self.direct.T, trans="T").T
        return self.intensities

    def exchanges(self, activity):
        ex = self.a[self.a["index of activity"].eq(activity)].merge(
            self.ai, left_on="index of product", right_index=True
        )
        return ex[
            [
                "index of product",
                "name",
                "reference product",
                "unit",
                "location",
                "value",
                "flip",
            ]
        ].copy()

    def contributions(self, supply, method):
        scores = self.direct[method] * supply
        frame = self.ai.copy()
        frame["supply"] = supply
        frame["direct_score_per_unit"] = self.direct[method]
        frame["score"] = scores
        return frame.iloc[np.argsort(-np.abs(scores))].reset_index()

    def supplier_contributions(self, activity, method):
        ex = self.exchanges(activity)
        ex = ex[ex.flip.eq(1)].copy()
        intensity = self.upstream_intensities()[method]
        ex["supplier_intensity"] = intensity[ex["index of product"]]
        production = float(
            self.a.loc[
                self.a["index of activity"].eq(activity) & self.a.flip.eq(0), "value"
            ].sum()
        )
        ex["contribution_per_output"] = ex.value * ex.supplier_intensity / production
        return ex.sort_values("contribution_per_output", key=abs, ascending=False)
