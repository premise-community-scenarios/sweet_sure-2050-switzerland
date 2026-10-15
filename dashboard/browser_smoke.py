"""Exercise real Dash callbacks, signed charts, CSV download and mobile layout."""

import argparse
import json
import os
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".venv-dashboard/browsers")
)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--results",
    type=Path,
    default=ROOT / "results/sps_battery_corrected/refined_metals",
)
parser.add_argument("--url", default="http://127.0.0.1:8050")
args = parser.parse_args()
OUT = args.results / "dashboard_preview"
OUT.mkdir(parents=True, exist_ok=True)
errors = []


def wait_traces(page, count):
    page.wait_for_function(
        "n => document.querySelector('#trend .js-plotly-plot')?.data?.length === n",
        arg=count,
    )


def dropdown(page, identifier, text):
    item = page.locator(f"#{identifier} input")
    item.fill(text)
    item.press("Enter")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": 1440, "height": 1050}, device_scale_factor=1
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: (
            errors.append(f"HTTP {response.status}: {response.url}")
            if response.status >= 500
            else None
        ),
    )
    page.goto(args.url, wait_until="networkidle")
    wait_traces(page, 4)
    years = page.locator("#trend .js-plotly-plot").evaluate("el => el.data[0].x")
    assert years == [2020, 2025, 2030, 2035, 2040, 2045, 2050]
    page.screenshot(path=str(OUT / "desktop.png"), full_page=True)
    page.get_by_role("button", name="All 4", exact=True).click()
    wait_traces(page, 4)
    page.get_by_role("button", name="Baselines", exact=True).click()
    wait_traces(page, 4)
    dropdown(page, "method-family", "RELICS")
    page.wait_for_function(
        "document.querySelector('#chart-title').textContent === 'Aluminium'"
    )
    dropdown(page, "indicator", "Copper")
    page.wait_for_function(
        "document.querySelector('#chart-title').textContent === 'Copper' && document.querySelector('#trend .js-plotly-plot')?.layout?.yaxis?.title?.text === 'tonnes'"
    )
    page.screenshot(path=str(OUT / "relics.png"), full_page=True)
    with page.expect_download() as download_info:
        page.get_by_role("button", name="↓ Download CSV", exact=True).click()
    download_info.value.save_as(str(OUT / "relics_download.csv"))
    relics = pd.read_csv(OUT / "relics_download.csv")
    assert set(relics.native_unit) == {"kilogram"}
    assert set(relics.display_unit) == {"tonnes"}
    assert (relics.value - relics.display_value * 1000).abs().max() < 1e-6
    dropdown(page, "method-family", "Refined-metal inputs")
    page.wait_for_function(
        "document.querySelector('#method-scope').textContent.includes('contained aluminium')"
    )
    dropdown(page, "indicator", "Copper")
    page.wait_for_function(
        "document.querySelector('#trend .js-plotly-plot')?.layout?.yaxis?.title?.text === 'tonnes Cu'"
    )
    assert "consuming activity" in page.locator("#location-help").inner_text()
    assert "do not add" in page.locator("#method-scope").inner_text()
    page.screenshot(path=str(OUT / "refined_copper.png"), full_page=True)
    with page.expect_download() as download_info:
        page.get_by_role("button", name="↓ Download CSV", exact=True).click()
    download_info.value.save_as(str(OUT / "refined_copper_download.csv"))
    copper = pd.read_csv(OUT / "refined_copper_download.csv")
    assert set(copper.native_unit) == {"kg Cu"}
    assert set(copper.display_unit) == {"tonnes Cu"}
    assert (copper.value - copper.display_value * 1000).abs().max() < 1e-6
    assert copper.selected_products.str.contains("copper, cathode").all()
    assert copper.location_basis.str.contains("consuming activity").all()
    assert copper.energy_equipment_boundary.str.contains("vehicle chassis/body").all()
    assert copper.energy_equipment_interpretation.str.contains(
        "unresolved equipment coverage"
    ).all()
    page.locator("#method-details summary").click()
    with page.expect_download() as download_info:
        page.get_by_role("button", name="↓ Download definition", exact=True).click()
    download_info.value.save_as(str(OUT / "copper_definition.json"))
    definition = json.loads((OUT / "copper_definition.json").read_text())
    assert definition["method"]["definition"]["kind"] == "contained_metal"
    assert "Uranium" in definition["coverage_gaps"]
    assert len(definition["energy_equipment_boundary"]["coverage"]) == 101
    dropdown(page, "method-family", "Alloy inputs")
    page.wait_for_function(
        "document.querySelector('#chart-title').textContent === 'Aluminium alloys'"
    )
    dropdown(page, "indicator", "Iron and steel alloys")
    page.wait_for_function(
        "document.querySelector('#chart-title').textContent === 'Iron and steel alloys' && document.querySelector('#trend .js-plotly-plot')?.layout?.yaxis?.title?.text === 'tonnes alloy'"
    )
    assert "no elemental composition" in page.locator("#method-scope").inner_text()
    page.locator("#method-details summary").click()
    page.screenshot(path=str(OUT / "steel_alloys.png"), full_page=True)
    with page.expect_download() as download_info:
        page.get_by_role("button", name="↓ Download CSV", exact=True).click()
    download_info.value.save_as(str(OUT / "alloy_download.csv"))
    alloy = pd.read_csv(OUT / "alloy_download.csv")
    assert set(alloy.native_unit) == {"kg alloy"}
    assert set(alloy.display_unit) == {"tonnes alloy"}
    assert alloy.quantity_basis.eq("whole alloy product mass").all()
    page.locator("#view label").filter(has_text="Contributions").click()
    dropdown(page, "breakdown", "Contribution location")
    wait_traces(page, 4)
    assert "consuming activity" in page.locator("#location-help").inner_text()
    page.get_by_role("button", name="Reset", exact=True).click()
    page.wait_for_function(
        "document.querySelector('#chart-title').textContent === 'Climate change'"
    )
    dropdown(page, "indicator", "Land use")
    page.wait_for_function(
        "document.querySelector('#chart-title').textContent === 'Land use'"
    )
    page.locator("#view label").filter(has_text="Contributions").click()
    page.wait_for_function(
        "document.querySelector('#trend .js-plotly-plot')?.data?.some(t => t.type === 'bar')"
    )
    wait_traces(page, 5)
    dropdown(page, "breakdown", "Final-energy use")
    wait_traces(page, 12)
    page.screenshot(path=str(OUT / "contributions.png"), full_page=True)
    page.get_by_role("button", name="Reset", exact=True).click()
    wait_traces(page, 4)
    dropdown(page, "variables", "bus hydrogen")
    page.wait_for_function(
        "document.querySelector('#chart-subtitle').textContent.includes('1 selected uses')"
    )
    page.locator("#scale label").filter(has_text="Change from 2020").click()
    page.wait_for_function(
        "document.querySelector('#selection-notice').textContent.includes('undefined')"
    )
    page.get_by_role("button", name="Reset", exact=True).click()
    wait_traces(page, 4)
    page.wait_for_function(
        "document.querySelector('#chart-subtitle').textContent.includes('all final-energy uses')"
    )
    initial = page.locator("#trend .js-plotly-plot").evaluate("el => el.data[0].y")
    slider = page.locator("#years .rc-slider")
    slider.scroll_into_view_if_needed()
    box = slider.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 3, box["y"] + box["height"] / 2)
    page.wait_for_function(
        "document.querySelector('#trend .js-plotly-plot')?.data?.[0]?.x[0] === 2030"
    )
    page.locator("#scale label").filter(has_text="Change from 2020").click()
    page.wait_for_function(
        "document.querySelector('#trend .js-plotly-plot')?.layout?.yaxis?.title?.text.includes('% change')"
    )
    relative = page.locator("#trend .js-plotly-plot").evaluate("el => el.data[0].y[0]")
    assert abs(relative - 100 * (initial[2] - initial[0]) / abs(initial[0])) < 1e-10
    page.get_by_role("button", name="Reset", exact=True).click()
    page.wait_for_function(
        "document.querySelector('#trend .js-plotly-plot')?.data?.[0]?.x?.length === 7 && document.querySelector('#trend .js-plotly-plot').layout.yaxis.title.text === 'Mt CO₂-eq.'"
    )
    with page.expect_download() as download_info:
        page.get_by_role("button", name="↓ Download CSV", exact=True).click()
    download = download_info.value
    download.save_as(str(OUT / "download.csv"))
    df = pd.read_csv(OUT / "download.csv")
    assert len(df) == 28 and df.series.nunique() == 4
    assert set(df.year) == set(years) and set(df.native_unit) == {"kg CO2-Eq"}
    assert set(df.display_unit) == {"Mt CO₂-eq."}
    assert df.selected_locations.eq("CH; EU wo CH; RoW").all()
    for box in page.locator("#sectors input").all():
        box.uncheck()
    page.wait_for_function(
        "document.querySelector('#trend .js-plotly-plot')?.data?.length === 0"
    )
    page.get_by_role("button", name="Reset", exact=True).click()
    wait_traces(page, 4)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(800)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), "Mobile page overflows horizontally"
    page.screenshot(path=str(OUT / "mobile.png"), full_page=True)
    dropdown(page, "method-family", "Refined-metal inputs")
    page.wait_for_function(
        "document.querySelector('#method-scope').textContent.includes('contained aluminium')"
    )
    page.wait_for_timeout(500)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), "Metal scope overflows mobile viewport"
    page.screenshot(path=str(OUT / "mobile_metals.png"), full_page=True)
    assert not errors, errors
    browser.close()

report = {
    "passed": True,
    "checks": [
        "four baseline traces",
        "year X axis",
        "all four scenarios",
        "EF and RELICS family switches and reset",
        "RELICS kilogram-to-tonne display and CSV",
        "refined-metal family, contained-metal units and CSV scope",
        "downloaded metal definition and coverage gaps",
        "separate alloy-mass units and CSV",
        "material consumption location contributions",
        "EF indicator switch",
        "sector contributions",
        "top 10 plus other uses",
        "undefined zero baseline",
        "year range control and fixed 2020 baseline",
        "reset filters",
        "CSV download",
        "empty selection",
        "mobile layout",
        "mobile metal scope layout",
    ],
    "browser_errors": errors,
}
(OUT / "browser_checks.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
