"""Heat routing must wait for regionalization and preserve coefficients."""

import json
import pytest
from dev.stem_stationary_energy import route_swiss_heat


def test_heat_routing_requires_existing_swiss_provider():
    record = dict(supplier="heat", product="industrial heat", unit="megajoule")
    exchange = dict(
        name="heat",
        product="industrial heat",
        unit="megajoule",
        location="DE",
        amount=0.7,
        type="technosphere",
        input=("old", "code"),
    )
    wrapper = {
        "name": "heat use",
        "reference product": "oil",
        "unit": "megajoule",
        "location": "CH",
        "exchanges": [exchange],
        "Swiss heat provider routing": json.dumps([record]),
    }
    with pytest.raises(ValueError, match="provider is not unique"):
        route_swiss_heat([wrapper])
    assert exchange["location"] == "DE"
    provider = {
        "name": "heat",
        "reference product": "industrial heat",
        "unit": "megajoule",
        "location": "CH",
        "exchanges": [],
    }
    route_swiss_heat([wrapper, provider])
    assert exchange["location"] == "CH" and exchange["amount"] == 0.7
    assert "input" not in exchange
    route_swiss_heat([wrapper, provider])
    with pytest.raises(ValueError, match="provider is not unique"):
        route_swiss_heat([wrapper, provider, dict(provider)])
