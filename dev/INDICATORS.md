# SWEET-SURE indicators with Pathways

Run from the repository root with the existing `pathways` conda environment:

```bash
conda run --no-capture-output -n pathways python dev/generate_indicators.py \
  dev/smoke_sps1_ei312_20260928/gains_fixed_run/remind-SSP2-PkBudg1000-stem-SPS1_bas0.zip \
  --output-dir results/indicators_sps1_ei312_20260928
```

The script accepts additional package paths. By default it processes CH, all
`EXT - 0 - FE` variables, and the notebook's six methods and six years:
2020, 2025, 2030, 2035, 2040, 2050. Ecoinvent **3.12** characterization factors
are selected explicitly. For all seven exported years, add
`--years 2020 2025 2030 2035 2040 2045 2050`.

Years run sequentially to limit memory use. `--multiprocessing` enables the
notebook's parallel execution. Results are deterministic (`use_distributions=0`).
No Brightway database import or rebuild is performed; Pathways reads the package
matrices. Its dependencies can still write logs in the Brightway user directory.

## Outputs and checks

- `results_<package>.gzip`: gzip-compressed Parquet with contributions by original
  inventory location, variable, year and indicator. Activity categories are summed
  before caching, matching their eventual sum in the notebook.
- `results_<package>_indicators.xlsx`: notebook-style year columns, with sector,
  variable, region, scenario, indicator, contribution location and unit.
- `df_final.xlsx`, `df_final.parquet`: combined long-format indicator results.
- `indicator_totals.csv`: annual totals for each scenario/indicator.
- `pivottable_*.html`: interactive tables (their JavaScript dependencies need
  internet access when opened); `--no-html` skips these.
- `results_<package>_demands.csv`: supplier, location, unit conversion and scenario
  demand for every selected variable/year.
- `results_<package>.manifest.json`: package/configuration/characterization-factor
  checksums, actual software versions, settings and validation results.
- `coverage/<package>/`: full variable coverage, omitted energy shares and the
  audited copy described below.
- `results_<package>_omitted_impacts.csv`: the six indicators' actual contributions
  that the original notebook workflow would omit.
- `run.log`, `pathways.log`, `warnings.json`: diagnostic records, including
  dependency warnings. Missing activity classifications are recorded as
  `undefined` and counted in the manifest. All activities remain in the sums.

The script checks all selected suppliers and unit conversions, requires results
for every positive demand and requested year/indicator, rejects nonfinite
results, and verifies that grouping preserves totals. Negative demand below
`-1e-12 PJ/yr` fails; smaller negative roundoff is clipped to zero and counted.
Negative **impacts** are retained.

Use a new output directory for new inputs. `--resume` reuses raw results only
when input, method, year and characterization-factor checksums match, and the
raw result checksum is intact. It can also retry an interrupted calculation.

## Corrections to the notebook workflow

The final tables contain annual **scenario-weighted** final-energy impacts, not
per-unit intensities. The six scientific methods are unchanged. Freshwater use
is labeled `m3`; the notebook's mismatched label fell through to `kg`.

The sector classifier preserves all 85 notebook entries and includes the other
current final-energy names. Unknown sectors fail explicitly. The notebook's
static lookup omitted 17 of the package's 101 variables, including the spelling
`FE_services_other_electric` (the notebook expects `FE_services_FE_other_electric`).
Ten of those 17 have nonzero demand in SPS1_bas0.

Three additional variables exist in the scenario data but are absent from the
ZIP's mapping resource:

- `FE_industry_heat_DH` and `FE_services_DH`: premise's temporary dictionary is
  keyed by dataset name/product. New datasets skip duplication, so the final
  residential entry overwrites these two shared district-heating mappings.
- `FE_industry_heat_CHP_fuel_cell`: its regionalization entry overwrites the
  production-pathway metadata, including the variable name.

`indicator_coverage.py` restores only these known omissions in a local package
copy. It requires the exact configuration checksum from the build manifest and
a unique matching CH supplier in **every exported year**. All scenario data and
inventory matrices are verified byte-for-byte unchanged. The original ZIP is
untouched. Unexpected missing mappings or ambiguous suppliers stop the run.
Both mapping losses are fixed generically in premise commits `33e606a3`
(shared new inventories) and `a9a7f7d7` (direct regionalization).
Export bindings are preserved separately from transformation metadata, keeping
the existing regionalization precedence. Regression tests cover unrelated
commodities and scenario names, original and imported inventories, regional
exclusions, and independent exported demands. Fourteen focused tests passed.
A before/after check using the exported fuel-cell inventory confirmed unchanged
inventory and transformation metadata; all five affected mappings match the
verified indicator run, and all 35 demands across seven years survive export.
Existing ZIPs retain their original mappings, so this script continues to repair
verified copies when necessary.

The local Pathways checkout also needs the tested `get_lca_matrices` correction:
the matrix file's immediate parent must equal the requested year. Matching a year
anywhere in the absolute path incorrectly selects all years when this repository's
name contains `2050`. This correction is in Pathways commit `142ab4b`, in
`pathways/lca.py`, with regression tests in `tests/test_matrix_year_selection.py`.

The three additional mappings refer to existing inventories. Any documented
limitations of those inventories remain, including the unavailable STEM
efficiency for industrial gas fuel-cell CHP from 2030 onward.

The contribution geography labels preserve the notebook: CH, `EU wo CH` and RoW.
`EU wo CH` means REMIND EUR + NEU excluding CH, which is broader than political
EU membership. Contributions are assigned by inventory activity location.

## Tests

```bash
conda run -n pathways python -m pytest tests/test_generate_indicators.py -q
```

Independently check the 2020 and 2050 totals using a direct sparse solve of the
exported matrices and summed scenario demand (without Pathways calculation code):

```bash
conda run -n pathways python dev/verify_indicator_totals.py \
  results/indicators_sps1_ei312_20260928/results_remind-SSP2-PkBudg1000-stem-SPS1_bas0.manifest.json
```

### Pumped-storage water balance and efficiency (2026-10-02)

Premise's Swiss hydropower correction now covers both reservoir and pumped-storage
inventories. The configured evaporation is 0.00175 m³/kWh, following Flury and
Frischknecht (2012), sections 4.1.3 and 4.2.1. Discharge is calculated as the
inventory's own turbine-water withdrawal minus evaporation. Thus the pumped-storage
withdrawal of approximately 4.3 m³ is retained, instead of using the reservoir
inventory's approximately 0.81 m³. Foreign datasets and other hydro technologies
are outside this correction. Missing or ambiguous water-balance components fail
explicitly; relative uncertainty is rescaled where possible.

The SWEET `hydro (pumped storage)` mapping now requests an absolute efficiency,
with explicit technosphere filters for high-voltage electricity markets and
market groups. Both names are needed because the background electricity update
can relink the original Swiss market to a regional market group before the
external transformation. `dev/stem_efficiency.py` interprets explicitly filtered
electricity-to-electricity efficiency as output divided by the selected inputs.
It scales only those inputs, retaining supplier proportions and every other
exchange. It requires one electricity output and positive selected electricity
inputs, and records the before/after balance in `.efficiencies.json`. This is
independent of scenario and technology labels; the filter is explicitly configured.
The target comes from STEM each year, rather than a hard-coded 80% value.

`dev/validate_stem_build.py` now verifies the exported pumping input and water
balance whenever this mapping is enabled. The paired check in
`dev/validate_hydropower_fix.py` compares complete A/B matrices with the prior
packages, allowing only the configured pumping input and Swiss hydro water flows
to change. Tests and fresh 2030 builds for SPS2_bas0 and SPS4_bas0 are recorded in
`results/hydropower_fix_20261002_v2`. Both have no unlinked exchanges or missing
active efficiency targets. Their existing 81 background warnings are unchanged.
All twelve indicator totals pass independent SuperLU checks. Freshwater totals
are 178.213 and 156.431 million m³/year, respectively, versus 536.069 and 253.072
in the previous batch. The full 40-scenario batch and dashboard still contain
the earlier results until explicitly regenerated.
