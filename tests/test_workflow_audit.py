"""Analytical reference cases for the independent audit, not the model builder."""

import json
from pathlib import Path
import sys
import zipfile

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from audit_stem_input_chain import expected_format, scenario_code
from workflow_audit_core import MatrixAudit


def test_waste_aggregation_conserves_all_four_generation_categories():
    rows = {
        "Imports|Electricity": [3.0, 2.0],
        "Exports|Electricity": [5.0, 1.0],
        "Electricity generation|Nuclear Fuel": [10.0, 20.0],
    }
    for fuel in ["Renewable", "Non Renewable"]:
        for suffix in ["", " CCS"]:
            prefix = f"Electricity generation|Wastes|{fuel}|"
            rows[prefix + "Wastes Incineration (electric only)" + suffix] = [2.0, 4.0]
            rows[prefix + "CHP Wastes (for District Heating)" + suffix] = [1.0, 3.0]
    raw = pd.DataFrame.from_dict(rows, orient="index", columns=[2020, 2050])
    raw["region"] = "CH"
    raw["unit"] = "PJ/yr."
    result = expected_format(raw, [2020, 2050])
    waste = result[result.index.str.startswith("Electricity generation|Wastes|")]
    assert len(waste) == 4
    np.testing.assert_allclose(waste[[2020, 2050]].sum(), [12.0, 28.0])
    np.testing.assert_allclose(
        result.loc["Imports|Electricity", [2020, 2050]].astype(float), [0.0, 1.0]
    )
    nuclear = result[
        result.index.str.startswith("Electricity generation|Nuclear fuel|")
    ]
    np.testing.assert_allclose(nuclear[[2020, 2050]].sum(), [10.0, 20.0])


@pytest.mark.parametrize(
    "name,expected",
    [
        ("STEM_to_Premise_SPS1_2035.xlsx", "SPS1_bas0"),
        ("STEM_to_Premise_SPS3_SOC_Mid_2035.xlsx", "SPS3_soc2"),
        ("STEM_to_Premise_SPS4_NUC_High_2035.xlsx", "SPS4_nuc3"),
    ],
)
def test_scenario_identity_comes_from_filename(name, expected):
    assert scenario_code(name) == expected


def test_independent_matrix_math_against_known_answer(tmp_path):
    # Three kWh use six MJ fuel; land is 6*3 + 3*0.5 = 19.5 m2-year,
    # carbon is 6*5 = 30 kg. Two duplicate fuel exchanges must add to 2 MJ/kWh.
    ai = pd.DataFrame(
        [
            ["grid", "electricity", "kilowatt hour", "CH", 0],
            ["fuel", "fuel", "megajoule", "RoW", 1],
        ],
        columns=["name", "reference product", "unit", "location", "index"],
    )
    bi = pd.DataFrame(
        [
            ["Occupation, forest", "natural resource", "land", "square meter-year", 0],
            ["Carbon dioxide, fossil", "air", "unspecified", "kilogram", 1],
        ],
        columns=["name", "compartment", "subcompartment", "unit", "index"],
    )
    a = pd.DataFrame(
        [[0, 0, 1.0, 0], [1, 1, 1.0, 0], [0, 1, 0.75, 1], [0, 1, 1.25, 1]],
        columns=["index of activity", "index of product", "value", "flip"],
    )
    b = pd.DataFrame(
        [[0, 0, 0.5], [1, 0, 3.0], [1, 1, 5.0]],
        columns=["index of activity", "index of biosphere flow", "value"],
    )
    package = tmp_path / "toy.zip"
    with zipfile.ZipFile(package, "w") as z:
        for name, frame in [
            ("A_matrix_index", ai),
            ("B_matrix_index", bi),
            ("A_matrix", a),
            ("B_matrix", b),
        ]:
            z.writestr(
                f"inventories/model/scenario/2050/{name}.csv",
                frame.to_csv(index=False, sep=";"),
            )
    cf = tmp_path / "cf.json"
    cf.write_text(
        json.dumps(
            [
                {
                    "name": ["Land"],
                    "exchanges": [
                        {
                            "name": "Occupation, forest",
                            "categories": ["natural resource", "land"],
                            "amount": 1.0,
                        }
                    ],
                },
                {
                    "name": ["Climate"],
                    "exchanges": [
                        {
                            "name": "Carbon dioxide, fossil",
                            "categories": ["air"],
                            "amount": 1.0,
                        }
                    ],
                },
            ]
        )
    )
    m = MatrixAudit(package, 2050, cf, ["Land", "Climate"])
    supply, score, residual = m.solve(np.array([[3.0, 0.0], [0.0, 0.0]]))
    np.testing.assert_allclose(supply, [[3.0, 0.0], [6.0, 0.0]])
    np.testing.assert_allclose(score, [[19.5, 0.0], [30.0, 0.0]])
    np.testing.assert_allclose(m.upstream_intensities(), [[6.5, 3.0], [10.0, 5.0]])
    assert float(np.max(residual)) < 1e-15
    contributions = m.contributions(supply[:, 0], 0).set_index("location")
    assert contributions.loc["CH", "score"] == 1.5
    assert contributions.loc["RoW", "score"] == 18.0
    assert m.supplier_contributions(0, 0).contribution_per_output.sum() == 6.0
    with pytest.raises(ValueError, match="Non-unique supplier"):
        m.find("grid", location="RoW")
