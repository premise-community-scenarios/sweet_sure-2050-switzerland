import numpy as np
import pandas as pd
from datapackage import Package
from premise.external_data_validation import check_scenario_data_file, get_recursively
import yaml


def test_scenario_data_file():
    package = Package("datapackage.json")
    check_scenario_data_file(package, package.descriptor["scenarios"][0])
    frame = pd.read_csv(package.get_resource("scenario_data").source)
    config = yaml.safe_load(package.get_resource("config").raw_read())
    variables = set(get_recursively(config, "variable"))
    assert frame.variables.eq(frame.variables.str.strip()).all()
    assert not frame.duplicated(["model", "scenario", "region", "variables"]).any()
    years = [c for c in frame if c.isdigit()]
    assert np.isfinite(frame[years].to_numpy(dtype=float)).all()
    for scenario in package.descriptor["scenarios"]:
        available = set(frame.loc[frame.scenario == scenario, "variables"])
        assert variables <= available, (scenario, variables - available)
