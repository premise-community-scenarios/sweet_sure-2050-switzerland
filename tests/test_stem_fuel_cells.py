from io import BytesIO

import numpy as np
import pandas as pd
import pytest
from openpyxl import Workbook

from dev import stem_fuel_cells as fc


def frame():
    return pd.DataFrame(
        [
            {
                "model": "STEM",
                "scenario": "arbitrary",
                "region": "CH",
                "variables": v,
                "unit": "%" if v == fc.EFFICIENCY else "PJ/yr.",
                2020: 0,
                2050: value,
            }
            for v, value in [
                (fc.ELECTRICITY, 5.7),
                (fc.HEAT, 2.3),
                (fc.FUEL_E, 0),
                (fc.FUEL_H, 0),
                (fc.EFFICIENCY, 0),
            ]
        ]
    )


def test_recovers_physical_efficiency_and_conserves_allocated_fuel():
    original = frame()
    result = fc.add_derived_series(
        original,
        {
            "electricity": np.array([0, 5.7]),
            "heat": np.array([0, 2.3]),
            "fuel": np.array([0, 10.0]),
        },
    )
    pd.testing.assert_frame_equal(
        result.iloc[: len(original)].reset_index(drop=True), original, check_dtype=False
    )
    rows = result.set_index("variables")
    assert rows.loc[fc.EFFICIENCY + fc.RECOVERED, 2050] == pytest.approx(0.57)
    assert rows.loc[fc.FUEL_E + fc.RECOVERED, 2050] == pytest.approx(7.125)
    assert rows.loc[fc.FUEL_H + fc.RECOVERED, 2050] == pytest.approx(2.875)
    assert (
        rows.loc[
            [v + fc.RECOVERED for v in [fc.FUEL_E, fc.FUEL_H, fc.EFFICIENCY]], 2020
        ]
        == 0
    ).all()
    assert fc.validate_recovered_series(result)[0]["active_years"] == 1
    unrelated = pd.DataFrame(
        [
            {
                "model": "STEM",
                "scenario": "arbitrary",
                "region": "elsewhere",
                "variables": "unrelated production",
                "unit": "PJ/yr.",
                2020: 1,
                2050: 2,
            }
        ]
    )
    assert len(fc.validate_recovered_series(pd.concat([result, unrelated]))) == 1
    result.loc[result.variables.eq(fc.FUEL_H + fc.RECOVERED), 2050] = 8
    with pytest.raises(ValueError, match="Inconsistent"):
        fc.validate_recovered_series(result)


@pytest.mark.parametrize(
    "e,h,f", [(1, 0, 0), (0, 0, 1), (-1, 1, 2), (1, 1, np.nan), (1, np.inf, 2)]
)
def test_invalid_or_unallocatable_source_balances_fail(e, h, f):
    with pytest.raises(ValueError):
        fc.recovered_values(np.array([e]), np.array([h]), np.array([f]))


def test_cache_output_must_reconcile_to_export():
    with pytest.raises(ValueError, match="does not match exported electricity"):
        fc.add_derived_series(
            frame(),
            {
                "electricity": np.array([0, 6]),
                "heat": np.array([0, 2.3]),
                "fuel": np.array([0, 10]),
            },
        )


def test_external_source_is_selected_from_formula_not_link_order(monkeypatch):
    book = Workbook()
    sheet = book.active
    sheet.title = "SPS_arbitrary"
    sheet.append(["model", "scenario", "region", "variables", "unit", 2020, 2050])
    sheet.append(
        [
            "STEM",
            "arbitrary",
            "CH",
            fc.ELECTRICITY,
            "PJ/yr.",
            "='[7]Electricity and CHPs'!B2",
            "='[3]Electricity and CHPs'!H2",
        ]
    )
    payload = BytesIO()
    book.save(payload)

    def cache(fuel):
        cells = {"E3": "2020", "F3": "2050"}
        for row, kind, commodity, value in [
            (10, "VAR_fout", "ITOT_NKEV_GAS_ELC_CHP", 5.7),
            (11, "VAR_fout", "ITOT_LTH_CHP", 2.3),
            (12, "VAR_fin", "INGA", fuel),
        ]:
            cells.update(
                {
                    f"B{row}": "ITOT-CHPGFC",
                    f"C{row}": kind,
                    f"D{row}": commodity,
                    f"E{row}": value,
                    f"F{row}": value,
                }
            )
        return {"sheets": {"OUT_Electricity_CHP": cells}}

    monkeypatch.setattr(fc, "external_caches", lambda _: {3: cache(10), 7: cache(12)})
    components = fc.source_components(payload.getvalue(), frame())
    assert components["fuel"].tolist() == [12, 10]


def test_derived_variables_cannot_be_added_twice():
    c = {
        "electricity": np.array([0, 5.7]),
        "heat": np.array([0, 2.3]),
        "fuel": np.array([0, 10]),
    }
    with pytest.raises(ValueError, match="already exists"):
        fc.add_derived_series(fc.add_derived_series(frame(), c), c)
