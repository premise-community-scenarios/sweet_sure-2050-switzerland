"""An internally consistent export can still contain an invalid material recipe."""

import pandas as pd
import pytest

from dev.audit_motor_magnet_package import MAGNET, compare, selected_amounts


@pytest.mark.parametrize("amount, passed", [(0.3148, True), (1.623952e-6, False)])
def test_magnet_neodymium_collapse_is_detected(amount, passed):
    nodes = pd.DataFrame(
        [
            {
                "index": 0,
                "name": MAGNET,
                "reference product": "magnet",
                "unit": "kilogram",
                "location": "GLO",
            },
            {
                "index": 1,
                "name": "oxide supply A",
                "reference product": "neodymium oxide",
                "unit": "kilogram",
                "location": "GLO",
            },
            {
                "index": 2,
                "name": "oxide supply B",
                "reference product": "neodymium oxide",
                "unit": "kilogram",
                "location": "GLO",
            },
        ]
    )
    matrix = pd.DataFrame(
        [
            {"index of activity": 0, "index of product": 0, "value": 1, "flip": 0},
            {
                "index of activity": 0,
                "index of product": 1,
                "value": amount * 0.4,
                "flip": 1,
            },
            {
                "index of activity": 0,
                "index of product": 2,
                "value": amount * 0.6,
                "flip": 1,
            },
        ]
    )
    reference = {
        "records": [
            {
                "name": MAGNET,
                "product": "magnet",
                "location": "GLO",
                "unit": "kilogram",
                "amounts": {"neodymium oxide": 0.3148},
            }
        ]
    }
    assert compare(nodes, matrix, reference, 2050)[0]["passed"] is passed


def test_motor_lithium_overlay_and_signed_magnet_mass():
    amounts = selected_amounts(
        "electric motor production, vehicle",
        [
            ("lithium carbonate, battery grade", "kilogram", 0.5),
            ("permanent magnet, for electric motor", "kilogram", 0.05),
            ("permanent magnet, for electric motor", "kilogram", -0.01),
            ("lithium industry energy", "megajoule", 20),
        ],
    )
    assert amounts["all direct lithium products"] == 0.5
    assert amounts["permanent magnet, for electric motor"] == pytest.approx(0.04)
