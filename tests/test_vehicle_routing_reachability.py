"""Zero direct demand does not prove absence of indirect system use."""

import pandas as pd
from dev.audit_vehicle_energy_package import reachable_activities


def test_reachability_includes_credits_cycles_and_ignores_zero_links():
    a = pd.DataFrame(
        {
            "index of activity": [0, 0, 1, 2, 2, 4],
            "index of product": [0, 1, 2, 1, 3, 3],
            "value": [1, 2, -3, 1, 0, 7],
        }
    )
    assert reachable_activities(a, [0]) == {0, 1, 2}
    assert reachable_activities(a, [0, 4]) == {0, 1, 2, 3, 4}
