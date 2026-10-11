# SWEET-SURE results explorer

An entirely local Dash app for the validated October 2026 indicator results.
Every chart uses years on the X axis. The app reads the existing results and
does not recalculate inventories or indicators.

Generated results were deleted on 11 October 2026. Recalculate and export a
validated indicator batch before starting the dashboard.

From the repository root:

```bash
python3 -m venv .venv-dashboard
.venv-dashboard/bin/python -m pip install -r dashboard/requirements.txt
.venv-dashboard/bin/python -m dashboard.app --results /path/to/indicator-batch
```

Open **http://127.0.0.1:8050**. Override the port with `--port 8051` or select
another completed batch with `--results /path/to/indicator-batch`.

The app starts with the four baseline scenarios and climate change. Select
any of the 40 scenarios, six indicators, four sectors, three contribution
locations or 101 final-energy uses. Clear the final-energy dropdown to include
all uses. An empty scenario, sector or location selection shows an empty chart.

- **Scenario comparison:** one line per scenario, with annual impacts or signed
  percentage changes from the fixed 2020 baseline.
- **Contributions:** one selected scenario, split by sector, contribution
  location or final-energy use. Annual bars preserve negative contributions;
  the dotted line shows the net total. The variable view groups all but the ten
  largest uses into an explicit residual so totals stay unchanged.
- **Period summary:** annual endpoint values and change relative to the absolute
  first-year value. Percentage changes from zero remain undefined.
- **Exports:** the current plotted series as CSV, including original units and
  values; an SVG image is available from the chart toolbar.

Data come from the selected batch's `toby_export/` directory. The legacy default
`results/indicators_ei312_20261001/` was removed; use `--results` for a new batch.
The app checks the dense CSV against its recorded SHA256 hash, units and complete
scenario/year grid. This export has exactly the same impacts as `df_final.parquet`
and also explicitly records inactive uses as zero. Data stay on the local computer;
no Toby access or upload is involved. Styles and JavaScript are served locally.

Numerical and browser checks:

```bash
.venv-dashboard/bin/python -m pip install -r dashboard/requirements-dev.txt
.venv-dashboard/bin/python -m pytest tests/test_dashboard.py -q
PLAYWRIGHT_BROWSERS_PATH=.venv-dashboard/browsers .venv-dashboard/bin/python -m playwright install chromium --only-shell
# With the app running on port 8050:
.venv-dashboard/bin/python dashboard/browser_smoke.py
```

The numerical checks compare all 1,680 plotted annual totals with the validated
indicator totals. Browser screenshots and the interaction report are saved under
`results/indicators_ei312_20261001/dashboard_preview/`.

Built with [Dash callbacks](https://dash.plotly.com/basic-callbacks),
[dropdowns](https://dash.plotly.com/dash-core-components/dropdown) and
[CSV downloads](https://dash.plotly.com/dash-core-components/download).
