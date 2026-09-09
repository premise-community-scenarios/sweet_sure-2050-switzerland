from datapackage import Package
from premise.external_data_validation import check_config_file


def test_config_file():
    check_config_file(Package("datapackage.json"))
