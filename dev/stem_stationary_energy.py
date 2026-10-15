"""Apply documented Swiss heat links after external providers exist."""

from contextlib import contextmanager
import json


def route_swiss_heat(database):
    providers = {}
    for ds in database:
        key = (ds["name"], ds["reference product"], ds["location"], ds["unit"])
        providers.setdefault(key, []).append(ds)
    for ds in database:
        if ds.get("location") != "CH" or ds.get("unit") != "megajoule":
            continue
        for record in json.loads(ds.get("Swiss heat provider routing", "[]")):
            key = (record["supplier"], record["product"], "CH", record["unit"])
            if len(providers.get(key, [])) != 1:
                raise ValueError(f"Swiss heat provider is not unique: {key}")
            inputs = [
                e
                for e in ds["exchanges"]
                if e.get("type") == "technosphere"
                and (e.get("name"), e.get("product"), e.get("unit"))
                == (key[0], key[1], key[3])
            ]
            if len(inputs) != 1:
                raise ValueError(f"Heat input is not unique: {ds['name']} / {key}")
            inputs[0]["location"] = "CH"
            inputs[0].pop("input", None)


@contextmanager
def swiss_stationary_energy():
    from premise.external import ExternalScenario

    original = ExternalScenario.relink_datasets

    def relinked(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        route_swiss_heat(self.database)
        return result

    ExternalScenario.relink_datasets = relinked
    try:
        yield
    finally:
        ExternalScenario.relink_datasets = original
