import pytest
from dev.premise_compat import snapshot_mappings


def test_mapping_survives_inventory_ownership_transfer_and_keeps_lhv():
    dataset = {
        "name": "generator",
        "reference product": "electricity",
        "unit": "kWh",
        "lhv": 36,
        "exchanges": [{"name": "fuel"}],
    }
    snapshot = snapshot_mappings({"electricity": {"gas": [dataset, dataset]}})
    dataset.clear()
    assert snapshot == {
        "electricity": {
            "gas": [
                {
                    "name": "generator",
                    "reference product": "electricity",
                    "unit": "kWh",
                    "lhv": 36,
                }
            ]
        }
    }


def test_incomplete_mapping_is_not_silently_dropped():
    with pytest.raises(ValueError, match="electricity/gas"):
        snapshot_mappings({"electricity": {"gas": [{}]}})


def test_metals_provenance_is_not_exported_as_activity_suppliers():
    decision = {"material": "steel", "reason": "unchanged", "region": "CH"}
    mapping = {
        "metals": {
            "material decisions": [decision],
            "material update metrics": {"changed": 3},
            "applied market shares": {"CH": {"copper": 1}},
            "market diagnostics": [{"reason": "no projection"}],
            "resource diagnostics": [{"flow": "Copper"}],
            "methodology": {"boundary": "primary extraction"},
            "transformed activities": [
                {"name": "steel production", "reference product": "steel", "unit": "kg"}
            ],
        }
    }
    result = snapshot_mappings(mapping)
    assert set(result["metals"]) == {"transformed activities"}
    assert mapping["metals"]["material decisions"] == [decision]
    assert mapping["metals"]["material update metrics"] == {"changed": 3}


def test_unknown_metadata_is_an_explicit_error_instead_of_a_silent_omission():
    with pytest.raises(ValueError, match="metals/new diagnostic"):
        snapshot_mappings({"metals": {"new diagnostic": {"message": "unknown"}}})
