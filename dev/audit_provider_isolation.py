"""Audit that external efficiency scaling leaves shared providers unchanged.

The context captures exchanges at three boundaries: before inventory checking,
before regionalization/efficiency changes, and before market creation. It writes
source provenance and snapshots for independent checks of exported matrices.
Explicit market replacements are applied after the preservation checks.
"""

from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path


def identity(dataset):
    return (
        dataset["name"],
        dataset.get("reference product", dataset.get("product")),
        dataset["unit"],
        dataset["location"],
    )


def exchanges(dataset):
    return [deepcopy(dict(e)) for e in dataset["exchanges"]]


@contextmanager
def audit_provider_isolation(output):
    # Match the normal builder's import order before installing hooks. Wurst and
    # Brightway both initialize structlog; importing external first breaks the
    # installed Wurst geographic fallback's logger.
    import bw2data  # noqa: F401
    from premise import PathwaysDataPackage  # noqa: F401
    import premise.external as external

    original_check = external.check_inventories
    original_regionalize = external.ExternalScenario.regionalize_inventories
    original_markets = external.ExternalScenario.create_markets
    records = []
    by_year = defaultdict(list)

    def check(*args, **kwargs):
        configuration = kwargs["configuration"]
        aliases = {
            (p["ecoinvent alias"]["name"], p["ecoinvent alias"]["reference product"])
            for p in configuration["production pathways"].values()
            if p.get("efficiency")
            and p["ecoinvent alias"].get("exists in original database", True)
        }
        originals = {
            identity(d): exchanges(d)
            for d in kwargs["database"] + kwargs["inventory_data"]
            if (d["name"], d["reference product"]) in aliases
        }
        result = original_check(*args, **kwargs)
        inventory, database, config, _ = result
        lookup = {identity(d): d for d in database + inventory}
        for key, before in originals.items():
            assert exchanges(lookup[key]) == before, (
                "source changed in check_inventories",
                key,
            )
        for ds in database:
            source = ds.get("external scenario source")
            if not source or identity(source) not in originals:
                continue
            record = {
                "year": kwargs["year"],
                "copy": identity(ds),
                "source": identity(source),
                "before": originals[identity(source)],
                "preserved_during_check": True,
            }
            by_year[kwargs["year"]].append(record)
            records.append(record)
        return result

    def regionalize(self, *args, **kwargs):
        before = {
            identity(d): exchanges(d)
            for d in self.database
            if identity(d) in {tuple(r["source"]) for r in by_year[self.year]}
        }
        result = original_regionalize(self, *args, **kwargs)
        lookup = {identity(d): d for d in self.database}
        for key, excs in before.items():
            assert exchanges(lookup[key]) == excs, (
                "source changed during efficiency adjustment",
                key,
            )
        for record in by_year[self.year]:
            record["preserved_during_efficiency_adjustment"] = True
        return result

    def markets(self, *args, **kwargs):
        result = original_markets(self, *args, **kwargs)
        lookup = {identity(d): d for d in self.database}
        for record in by_year[self.year]:
            record["after_markets"] = exchanges(lookup[tuple(record["source"])])
            record["copies_after_markets"] = [
                {"identity": identity(d), "exchanges": exchanges(d)}
                for d in self.database
                if d.get("external scenario source")
                and identity(d["external scenario source"]) == tuple(record["source"])
                and d["name"] == record["copy"][0]
            ]
        Path(output).write_text(json.dumps(records, indent=2) + "\n")
        return result

    external.check_inventories = check
    external.ExternalScenario.regionalize_inventories = regionalize
    external.ExternalScenario.create_markets = markets
    try:
        yield records
    finally:
        external.check_inventories = original_check
        external.ExternalScenario.regionalize_inventories = original_regionalize
        external.ExternalScenario.create_markets = original_markets
