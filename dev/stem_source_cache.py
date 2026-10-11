"""Read saved external STEM cells without opening or refreshing external files."""

from io import BytesIO
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def external_caches(payload):
    result = {}
    with ZipFile(BytesIO(payload)) as book:
        for name in book.namelist():
            match = re.fullmatch(r"xl/externalLinks/externalLink(\d+)\.xml", name)
            if not match:
                continue
            root = ET.fromstring(book.read(name))
            names = [x.attrib["val"] for x in root.findall(".//m:sheetName", NS)]
            sheets = {}
            for sheet in root.findall(".//m:sheetData", NS):
                cells = {}
                for cell in sheet.findall("m:row/m:cell", NS):
                    value = cell.findtext("m:v", None, NS)
                    if value is not None and cell.get("t", "n") == "n":
                        value = float(value)
                    cells[cell.attrib["r"]] = value
                sheets[names[int(sheet.attrib["sheetId"])]] = cells
            rels = ET.fromstring(
                book.read("xl/externalLinks/_rels/" + Path(name).name + ".rels")
            )
            target = next(r.attrib["Target"] for r in rels if r.attrib["Id"] == "rId1")
            result[int(match[1])] = {"target": target, "sheets": sheets}
    return result


def raw_series(cache, technology, kind, commodity, allow_absent_flow=False):
    """Match named STEM rows; row positions vary between source workbooks."""
    cells = cache["sheets"]["OUT_Electricity_CHP"]
    rows = [
        address[1:]
        for address, value in cells.items()
        if re.fullmatch(r"B\d+", address)
        and value == technology
        and cells.get("C" + address[1:]) == kind
        and cells.get("D" + address[1:]) == commodity
    ]
    years = {
        int(value): address[:-1]
        for address, value in cells.items()
        if re.fullmatch(r"[E-Z]3", address) and value is not None
    }
    if not rows and allow_absent_flow:
        # Excel SUMPRODUCT over no matching rows returns zero. Retain that
        # distinction, and separately reconcile against the exported coproduct.
        return {year: (0.0, None, True) for year in years}
    if len(rows) != 1:
        raise ValueError(
            f"Expected one {technology}/{kind}/{commodity} row; got {rows}"
        )
    # Excel's sparse cache omits empty numeric cells. Arithmetic treats them as 0.
    return {
        year: (
            cells.get(col + rows[0]) or 0.0,
            col + rows[0],
            cells.get(col + rows[0]) is None,
        )
        for year, col in years.items()
    }
