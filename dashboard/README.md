# SWEET-SURE results explorer

An entirely local Dash app for validated Pathways indicator results.
Every chart uses years on the X axis. The app reads the existing results and
does not recalculate inventories or indicators.

The default batch is `results/sps_battery_corrected/refined_metals`: four SPS base scenarios,
seven years (2020–2050), 16 EF 3.1 headline categories, all 65 RELICS indicators,
60 refined-element inputs and five separate alloy-mass indicators (146 total).
To regenerate it, follow [the indicator workflow](../dev/INDICATORS.md).

From the repository root:

```bash
python3 -m venv .venv-dashboard
.venv-dashboard/bin/python -m pip install -r dashboard/requirements.txt
.venv-dashboard/bin/python -m dashboard.app
```

Open **http://127.0.0.1:8050**. Override the port with `--port 8051` or select
another completed batch with `--results /path/to/indicator-batch`.

The app starts with the four baseline scenarios and EF 3.1 climate change. Select
a method family and indicator, four sectors, three contribution locations or
101 final-energy uses. RELICS is displayed in tonnes while retaining kilograms
in the downloadable native values. Clear the final-energy dropdown to include
all uses. An empty scenario, sector or location selection shows an empty chart.

The **Refined-metal inputs** family reports tonnes of contained element in
explicitly selected refined products; **Alloy inputs** reports tonnes of whole
alloy product. Both include direct suppliers and markets, excluding transfers
between selected forms of the same indicator. The scope notice and expandable
product list explain each boundary and factor. A JSON definition download carries
the product list, factors, sources and coverage gaps. These quantities can overlap
across indicators and must not be summed. Their location filter identifies the
consuming activity at the counted delivery, not the mining or refining origin.

The [current equipment and metal audit](../results/sps_battery_corrected/metal_audit/audit_report.html)
checks sampled consumers, all system totals, exported equipment coefficients and
the changes from the previous batch. Updated vehicle inventories now set the
purchased-energy denominator and lifetime equipment allocation, including truck
operating electricity. Dedicated boilers and partial aggregate electricity
component mixes are included.

This batch uses premise `b4ea884e`: corrected truck battery resizing through
cached inventories, fixed-range electric bus sizing, NMC532 energy-density
mapping, chargers without stationary battery storage, and the restored rail
hydrogen-tank supplier. A separate check compares exported capacity per MJ with
vehicle capacity, replacements, lifetime and electricity use. Physical battery
mass follows the upstream capacity markets. The previous `results/sps_updated/`
batch is retained for comparison.

The boundary includes energy-specific equipment and its upstream manufacture.
Vehicle bodies, chassis, roads and general buildings are excluded. Quantities
are lifetime-attributed scenario totals, not annual equipment sales or marginal
policy additions. Numerical agreement does not certify complete physical metal
demand: boiler materials, transport proxies, fuel-cell CHP proxies and unresolved
aggregate equipment remain documented limitations. The expandable coverage
register and CSV/JSON exports carry this boundary. Primary/secondary shares are
not inferred. The previous `results/sps_base/` batch is retained for comparison.

- **Scenario comparison:** one line per scenario, with annual impacts or signed
  percentage changes from the fixed 2020 baseline.
- **Contributions:** one selected scenario, split by sector, contribution
  location or final-energy use. Annual bars preserve negative contributions;
  the dotted line shows the net total. The variable view groups all but the ten
  largest uses into an explicit residual so totals stay unchanged.
- **Period summary:** annual endpoint values and change relative to the absolute
  first-year value. Percentage changes from zero remain undefined.
- **Exports:** the current plotted series as CSV, including original units and
  values, quantity basis, selected products, location basis and limitations;
  an SVG image is available from the chart toolbar.

Data come from the selected batch's `df_final.parquet` and `method_registry.json`.
The app checks their SHA256 hashes, units, independent-check count and complete
scenario/year/indicator/energy-use/location grid. Inactive uses and zero-valued
indicators are explicit zeros. Negative contributions retain their signs.
Older six-indicator batches with a validated `toby_export/` remain supported.
Data stay on the local computer. Styles and JavaScript are served locally.

Numerical and browser checks:

```bash
.venv-dashboard/bin/python -m pip install -r dashboard/requirements-dev.txt
.venv-dashboard/bin/python -m pytest tests/test_dashboard.py -q
PLAYWRIGHT_BROWSERS_PATH=.venv-dashboard/browsers .venv-dashboard/bin/python -m playwright install chromium --only-shell
# With the app running on port 8050:
.venv-dashboard/bin/python dashboard/browser_smoke.py
```

The numerical checks compare all 4,088 plotted annual totals with the validated
indicator totals. Browser screenshots and the interaction report are saved under
`results/sps_battery_corrected/refined_metals/dashboard_preview/`. The browser script accepts
`--results` for the artifact directory and `--url` for another running app.

Built with [Dash callbacks](https://dash.plotly.com/basic-callbacks),
[dropdowns](https://dash.plotly.com/dash-core-components/dropdown) and
[CSV downloads](https://dash.plotly.com/dash-core-components/download).
