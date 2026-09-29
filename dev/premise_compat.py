"""Export activity mappings while preserving premise's non-activity metadata."""

from contextlib import contextmanager

# Added by the metals transformation for validation/provenance, not suppliers.
NON_ACTIVITY_MAPPING_FIELDS = {
    ("metals", "material decisions"),
    ("metals", "material update metrics"),
}


def snapshot_mappings(mapping):
    """Select dataset descriptors from the mixed scenario-mapping structure."""
    result = {}
    for sector, variables in mapping.items():
        result[sector] = {}
        for variable, datasets in variables.items():
            if (sector, variable) in NON_ACTIVITY_MAPPING_FIELDS:
                continue
            records = []
            for dataset in datasets:
                try:
                    record = {
                        key: dataset[key]
                        for key in ("name", "reference product", "unit")
                    }
                except KeyError as error:
                    raise ValueError(
                        f"Incomplete Pathways mapping: {sector}/{variable}"
                    ) from error
                if "lhv" in dataset:
                    record["lhv"] = dataset["lhv"]
                if record not in records:
                    records.append(record)
            result[sector][variable] = records
    return result


@contextmanager
def preserve_pathways_mappings():
    from premise.pathways import PathwaysDataPackage

    original = PathwaysDataPackage._add_variables_mapping

    def add_variables_mapping(self):
        scenarios = self.datapackage.scenarios
        originals = [scenario["mapping"] for scenario in scenarios]
        try:
            for scenario, mapping in zip(scenarios, originals):
                scenario["mapping"] = snapshot_mappings(mapping)
            return original(self)
        finally:
            for scenario, mapping in zip(scenarios, originals):
                scenario["mapping"] = mapping

    PathwaysDataPackage._add_variables_mapping = add_variables_mapping
    try:
        yield
    finally:
        PathwaysDataPackage._add_variables_mapping = original
