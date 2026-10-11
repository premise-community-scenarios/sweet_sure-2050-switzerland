"""Exercise real Dash callbacks, signed charts, CSV download and mobile layout."""

import json
import os
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".venv-dashboard/browsers")
)
OUT = ROOT / "results/indicators_ei312_20261001/dashboard_preview"
OUT.mkdir(exist_ok=True)
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
    page.goto("http://127.0.0.1:8050", wait_until="networkidle")
    wait_traces(page, 4)
    years = page.locator("#trend .js-plotly-plot").evaluate("el => el.data[0].x")
    assert years == [2020, 2025, 2030, 2035, 2040, 2045, 2050]
    page.screenshot(path=str(OUT / "desktop.png"), full_page=True)
    page.get_by_role("button", name="All 40", exact=True).click()
    wait_traces(page, 40)
    page.get_by_role("button", name="Baselines", exact=True).click()
    wait_traces(page, 4)
    dropdown(page, "indicator", "Land occupation")
    page.wait_for_function(
        "document.querySelector('#chart-title').textContent === 'Land occupation'"
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
    assert set(df.year) == set(years) and set(df.native_unit) == {"kg CO2-eq."}
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
    assert not errors, errors
    browser.close()

report = {
    "passed": True,
    "checks": [
        "four baseline traces",
        "year X axis",
        "all 40 scenarios",
        "indicator switch",
        "sector contributions",
        "top 10 plus other uses",
        "undefined zero baseline",
        "year range control and fixed 2020 baseline",
        "reset filters",
        "CSV download",
        "empty selection",
        "mobile layout",
    ],
    "browser_errors": errors,
}
(OUT / "browser_checks.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
