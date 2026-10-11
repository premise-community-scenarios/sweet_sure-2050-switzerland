# SWEET-SURE indicators with Pathways

All generated packages, indicator results, and audit artifacts were removed on
11 October 2026. The dated paths and findings below document historical work;
the referenced files are no longer available locally. Rebuild packages and
recalculate indicators before running the dashboard or result-dependent audits.

## End-to-end audit of the September 2026 batch

The audit in `results/indicators_ei312_20260930/workflow_audit/` found material
issues upstream of otherwise reproducible indicator calculations. The existing
batch should not be published until the findings in `AUDIT_REPORT.txt` are
resolved. No source input, package or final score was changed by the audit.

```bash
conda run -n pathways python dev/audit_stem_input_chain.py results/indicators_ei312_20260930
conda run -n pathways python dev/audit_random_workflow.py results/indicators_ei312_20260930
conda run -n pathways python dev/recheck_random_solver_flags.py results/indicators_ei312_20260930
conda run -n pathways python dev/diagnose_heat_pump_land.py results/indicators_ei312_20260930
conda run -n pathways python dev/summarize_workflow_audit.py results/indicators_ei312_20260930
conda run -n pathways python -m pytest tests/test_workflow_audit.py -q
```

The source audit compares all original STEM workbooks and all 28,280 final-energy
demand chains. Seed `20260930` selects 70 demands across 14 scenario/year pairs,
covering all sectors and calculated years. Independent matrix calculations check
420 scores and 1,260 geographic contributions; a separate check resolves strict
cross-solver numerical flags without changing any original result.

The findings concern lost electricity-only waste generation in `format.py`, a
Swiss heat-pump proxy that uses NEU operating electricity, scenario-specific CHP
scaling propagating through unchanged background market shares, and anomalous
wood-CHP energy balances already present in the original STEM export. The heat
pump script records in-memory sensitivities for diagnosis; these are not approved
replacement results. The workbook collects checks, source discrepancies and
physical diagnostics. Exact execution scripts are retained in the audit's
`software/` directory alongside source and result checksums.

## Approved correction: electricity-only waste

`dev/format.py` retains the four raw electricity-only waste series separately:
renewable/nonrenewable, each with/without CCS. They have distinct production
pathways in `configuration_file/config.yaml` and enter the Swiss electricity
market once. Waste CHP and industrial CCS pathways now use their corresponding
CCS electrical efficiencies, rather than a non-CCS or electricity-only series.

Four proxies in `inventories/lci-sweet_sure.xlsx` assign all energy-production
burdens to electricity because these pathways have no useful heat coproduct.
They divide the source electricity inventories' non-production exchanges by
their actual workbook allocation factors (0.33 plastic waste; 0.29 biowaste).
The declared physical baseline efficiency is retained, then the build applies
the absolute STEM electrical efficiency. Capture, storage, residual emissions
and biomass uptake are scaled together. Existing CHP inventories and their
allocations are unchanged. These are documented proxy adaptations; waste fuel
is not an explicit exchange, so the efficiency check relies on the declared
baseline rather than an independently measured fuel input.

`dev/add_waste_electricity_inventories.py` reproduces the additions and preserves
existing workbook formulas and cached values. It leaves already-added variants
unchanged. Rebuild the scenario CSV directly from the latest archive:

```bash
conda run -n premise python dev/format.py \
  --archive 'dev/STEM raw data/new data export/OneDrive_2_28-09-2026.zip'
conda run -n premise python -m pytest tests/test_waste_electricity.py tests/test_stem_efficiency.py -q
```

The isolated test build and audit are in `results/waste_electricity_fix_20260930/`.
`dev/audit_waste_electricity.py` checks all raw waste series across all 40 scenarios,
then the exported market shares and carbon-exchange scaling in a rebuilt package.
The previous batch remains unchanged and still has other unresolved audit issues.

## Approved correction: Swiss heat-pump electricity

`dev/stem_heat_pumps.py` is installed as a scoped adapter by
`dev/create_data_packages.py`. During the external-scenario update it resolves
the physical inventory behind the Swiss air-water heat-pump forwarding process
and creates five dedicated Swiss heat processes: industry, services, residential
space heating, residential water heating and district heating. The original
background heat-pump processes remain unchanged.

Each process uses its corresponding STEM COP from `scenario_data.csv`, with
linear interpolation for requested intermediate years and no extrapolation or
efficiency clipping. Only operating electricity per MJ of useful heat is changed
to `1 / (3.6 * COP)` kWh. Equipment, refrigerant supply and refrigerant emissions
retain their source quantities per MJ of heat. The normal Swiss market replacement
then links operating electricity to the Swiss SPS low-voltage market; the adapter
checks that this actually happened.

The four final-energy wrappers represent MJ of electricity, so each consumes
`COP` MJ of heat per MJ of final electricity. Their resulting direct operating
electricity is exactly one MJ per MJ of STEM final electricity. A Swiss wrapper
is created where premise's duplicate exists only in NEU (residential water
heating). Existing direct biosphere bookkeeping flows in the wrappers are
preserved. District heating instead retains a heat functional unit and consumes
`1 / COP` MJ of electricity per MJ of heat. Generic relative efficiency scaling
is removed from that pathway's in-memory configuration to avoid applying the
COP twice or rescaling equipment and refrigerants.

The source configuration and inventory workbook do not need changes for this
adapter. The built package contains the complete corrected inventories and
mapping; downstream Pathways requires no special treatment. The build manifest
records the adapter hash and `*.heat_pumps.json` records its choices.

`dev/audit_heat_pumps.py` is called by the standard package validator for builds
using the adapter. It checks the exported electricity supplier, COP, final-energy
conversion, equipment/refrigerant quantities and district-heating mapping.
The batch builder freezes the adapter and collects its validation reports.

```bash
conda run -n premise python -m pytest tests/test_stem_heat_pumps.py tests/test_create_data_packages.py tests/test_build_all_data_packages.py -q
conda run -n premise python dev/audit_heat_pumps.py path/to/rebuilt-package.zip
```

The targeted rebuilds and comparisons are in `results/heat_pump_fix_20260930/`.
They retain the previously approved waste correction. Source CHP energy-balance
and background CHP-provider issues are separate and still require resolution.

## Comparing the latest results with Toby

Download the public report and raw database records for the batch's scenarios,
then build the comparison locally:

```bash
conda run -n pathways python dev/download_toby_results.py \
  results/indicators_ei312_20260930
conda run -n pathways python dev/compare_toby_indicators.py \
  results/indicators_ei312_20260930
```

The downloader caches report 11 for each exact scenario ID in
`toby_comparison/old_downloads/`, retaining URLs, timestamps and SHA256 hashes.
Rerunning it reuses verified cached files. The comparison checks parameter IDs,
paths, units, raw records and the hashes of the latest validated exports.
Only shared years are compared; absent old years are not interpolated.

`toby_comparison/old_vs_latest.xlsx` contains scenario, sector, location and
variable comparisons. `old_vs_latest.html` is an offline interactive chart with
a scenario selector. Full cell comparisons are retained as Parquet and compressed
CSV. `comparison_validation.json` records checks and source hashes.

The workbook separates changes on populated old cells, latest contributions to
registered but blank old cells, and latest contributions from unregistered
variables. Old blanks remain unknown rather than becoming stored zeros.
Percent changes use the absolute old total as denominator; a zero denominator
stays blank. Negative scores are retained. The sum of new contributions in
unregistered variables does not by itself prove all their activity was omitted
previously: some STEM categories may have been reclassified. Historical software,
inventory and characterization-factor versions are not specified by Toby's
download, so this comparison cannot isolate the effect of each model update.
It does not upload or change any platform data.

```bash
conda run -n pathways python -m pytest tests/test_compare_toby_indicators.py -q
```

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

The three additional mappings refer to existing inventories. Their documented
proxy limitations remain. The unavailable industrial gas fuel-cell efficiency
was subsequently recovered from the saved STEM source rows (see below).

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

`--years` extends this check to all exported years. The relative comparison
tolerance defaults to `1e-8`; `--rtol` makes an alternative tolerance explicit
and records it in the verification JSON. The rebuilt SPS1 package was checked
across all seven years at `--rtol 1e-7`: small differences between the summed-demand
UMFPACK solve and Pathways' separate-demand calculations can exceed `1e-8`.
This changes only the verification threshold, not the package or indicator values.

## Full 40-scenario run and Toby export

After building a new batch in `results/data_packages`, calculate its indicators:

```bash
conda run --no-capture-output -n pathways python dev/run_indicator_batch.py \
  results/data_packages \
  --output-dir results/indicators_next --workers 2
```

The runner accepts the current builder's completed `progress.json` as well as
the older `completion_summary.json`, and checks each package checksum before
starting. New runs independently verify every year using separate demands and
SuperLU at `rtol=1e-8`, with the existing `1e-12` normwise backward-error limit.
This uses the verification method established during the earlier numerical
investigation; it does not change the Pathways calculation or LCIA methods.

The corrected September 2026 packages were processed with the command below.
Their package ZIPs have since been removed; this is a historical command, not a
current runnable example:

```bash
conda run --no-capture-output -n pathways python dev/run_indicator_batch.py \
  dev/data_packages_ei312_20260929_biosphere_fixed \
  --output-dir results/indicators_ei312_20260930 --workers 2
```

The batch checks all input package hashes, freezes Pathways and the calculation
scripts, and uses isolated runtime directories per scenario. It calculates all
seven years and independently verifies all six indicator totals for every year.
`progress.json`, `STATUS.md` and `batch.log` record progress. Each scenario retains
its own raw results, demands, manifest, warnings and verification log. The final
combined outputs are `df_final.parquet`, `df_final.csv`, `df_final.xlsx`, and
`indicator_totals.csv`.

For ill-conditioned inventories, summing demands before solving can produce small
numerical differences from Pathways' separate-demand solves. The checker also
supports an independent separate-demand solve, built directly from the exported
CSV matrices and characterization factors:

```bash
conda run -n pathways python dev/verify_indicator_totals.py MANIFEST.json \
  --years 2020 2025 2030 2035 2040 2045 2050 \
  --solver superlu --demand-mode separate --rtol 1e-8
```

This retains the original Pathways results. The validation JSON records the solver,
demand mode, script checksum, differences and matrix residuals. The original
summed-demand check remains available with `--solver umfpack --demand-mode summed`.
The independent checker assembles repeated exchanges directly in CSR. Converting
via CSC first changes the duplicate summation order: in the diagnosed
`SPS4_fin2/2035` matrix, 48 coefficients differed by at most `1.67e-16`, whereas
direct CSR matched the Pathways matrix exactly. The checker uses a normwise
backward-error limit of `1e-12` for every functional unit, including the matrix,
supply and demand scales. It also records the original demand-normalized residual.
This follows the residual scaling described in the
[LAPACK error-analysis guide](https://www.netlib.org/lapack/lug/node81.html).
`finalize_indicator_batch.py` can follow a running batch, independently recheck
flagged scenarios using separate demands at the stricter `1e-8` tolerance, and
assemble the final tables and Toby export. It refuses calculation failures or
unresolved verification failures. Its progress is in `finalization.json`.

Toby's public upload instructions at
<https://toby.euler.usi.ch/upl.php?mth=nav> specify the `prem1` format:

```text
sector,variable,year,region,model,scenario,impact,location,value,unit
```

The exporter writes Latin-1 CSVs with 17 significant digits, retaining original
variable/scenario labels, all seven years, negative values and explicit zeros.
It verifies an exact numerical CSV round trip and unchanged indicator totals.
Use the `prem1` upload section for either the combined CSV or the scenario files.
The script only creates local files; it does not upload them.

```bash
conda run -n pathways python dev/export_toby_indicators.py \
  results/indicators_ei312_20260930 \
  --reference results/indicators_ei312_20260930/toby_reference \
  --output-dir results/indicators_ei312_20260930/toby_export
```

`toby_reference/` preserves the public parameter/scenario registry, the old
`SPS1_bas0` report and the upload instructions with source URLs and hashes in
`sources.json`. On 30 September 2026, all 40 scenario identifiers were present,
but the premise parameter registry covered only 81 of 101 final-energy variables:
1,458 existing series versus 1,818 required for six indicators and three locations.
The 360 missing series are listed in `parameters_to_register.csv`, and their
contributions are quantified in `registration_coverage.csv`. No IDs are invented.
The public documentation does not state whether `prem1` creates unknown series;
their registration or importer handling must be confirmed before uploading.

The main file is `toby_export/premise_prem1_all_scenarios.csv`; the complete set of
CSVs and review reports is bundled in `toby_export/toby_upload_bundle.zip`.
The `review_by_scenario/` tables resemble Toby's downloads but contain blank IDs
for new series and are explicitly not upload files. All new results are retained
in the main `prem1` files.

### External-provider isolation (approved correction, 2026-09-30)

Premise now copies existing production-pathway providers before applying external
scenario efficiency targets. Custom markets and exported demand mappings use the
copies; existing background consumers retain their original providers and IAM
adjustments. This applies to arbitrary external scenarios and technologies. Explicit
`replaces`/`replaces in` instructions retain their existing meaning.

SWEET's physical-efficiency adapter reads the recorded source identity of a copy to
recover its allocation factor, including when the copy has been regionalized. It
applies the STEM target, subject only to optional explicit configuration bounds
(see the 2026-10-01 update below). The 2026-09-30 builds used no clipping. Package and waste
validators read the actual exported mapping instead of guessing generated names.

Diagnostic builds for this correction are in
`results/provider_isolation_fix_20260930`. `dev/audit_provider_isolation.py` records
source exchanges before inventory checking and verifies that checking and efficiency
adjustment preserve them. `dev/validate_provider_isolation.py` compares those source
and copy inventories to the exported matrices and checks that background electricity
markets do not acquire isolated providers. Its optional `--reference` argument
also checks that background electricity-market supplier shares are unchanged.
Use a new frozen batch directory for final regeneration; earlier snapshots retain
their original code and packages. Initialize the normal Brightway/Premise
builder imports before installing the diagnostic context; this preserves the
installed Wurst logger's initialization order.

These builds retain the earlier waste-electricity and heat-pump corrections. STEM
wood-CHP source energy balances still require review before final publication.

### Wood CHP and biofuel coproducts (approved separation, 2026-09-30)

The original STEM wood-CHP output includes electricity/heat from wood-to-biogas
and heat from wood-to-biodiesel, while its fuel denominator covers only the CHP.
`format.py` now retains every original row and adds six derived series per
scenario: CHP-only electricity, heat, two allocated fuel inputs and electrical
efficiency, plus domestic biogas excluding wood gasification without CCS.
It checks the decomposition against the labelled external STEM cells cached in
each source workbook. Existing separately reported coproduct rows are reused.
The original archive is read without updating external links or modifying it.

`config.yaml` maps CHP-only production to CHP inventories and gives the three
energy coproducts their own providers. It also splits wood-derived biomethane
from the residual domestic biogas supply, preserving its total production.
`stem_wood_coproducts.py`, installed by `create_data_packages.py`, builds dedicated
Swiss process copies and five output-allocation providers. No core Premise or
Pathways changes are needed for this correction.

The Fischer-Tropsch basis uses Premise's energy-allocated wood-derived diesel
inventory (43.3 MJ/kg). The wood-to-biogas basis uses ecoinvent 3.12's Swiss
fluidised-bed wood-to-biomethane inventory (production-property net heating value
34.97 MJ/m³). These are documented process proxies for the STEM technologies.
Copies of regional sources receive Premise's normal supplier regionalization;
native Swiss sources retain their existing suppliers. Subsequent SPS market
substitutions apply to both. Their background source inventories are retained.

For fuel output F and coproduct outputs E and H, all measured in energy units,
the fuel share is F/(F+E+H). The fuel provider demands this share of the physical
process per original fuel unit; each MJ of coproduct demands this share/LHV.
Electricity additionally converts 3.6 MJ/kWh. Thus the joint fuel, electricity
and heat outputs demand exactly F/LHV units of the original process burden.
The fuel allocation changes alongside the coproducts; coproduct burdens are
not added on top of an unchanged fully burdened fuel. This is allocation of the
producing process, not combustion of additional fuel. Carbon-related flows are
conserved over the joint outputs along with all other upstream flows.

The builder writes `.wood_coproducts.json` with sources, output quantities,
allocation shares and conversion factors. `validate_wood_coproducts.py` reads
exported matrices to verify those providers, joint burden conservation and the
electricity, heat and gas market shares. The batch builder runs this validator
and retains its sidecars. Rebuild with a fresh batch snapshot; earlier frozen
software does not contain this adapter.

```bash
conda run -n premise python dev/format.py \
  --archive "dev/STEM raw data/new data export/OneDrive_2_28-09-2026.zip"
conda run -n premise python dev/validate_wood_coproducts.py PACKAGE.zip
```

This separation does not cap or replace the underlying CHP efficiencies. The
19 Latest CHP-only cases below 1% combined efficiency and 59 above 110% remain
source-review flags. `results/stem_chp_investigation_20260930` documents their
provenance; the separation checks are in `results/wood_coproduct_fix_20260930`.

### Explicit external efficiency bounds (2026-10-01)

Premise's external-scenario efficiency entries accept optional bounds:

```yaml
efficiency:
  - variable: Efficiency of power plants (electric)|Wood|CHP Wood (for District Heating)|CHP only
    absolute: true
    bounds: {min: 0.05, max: 0.30}
    heat pathway: DH_CHP_wood  # optional combined-output diagnostic
```

The mechanism identifies no technologies and supplies no default limits. The
datapackage author explicitly chooses which targets to bound. The limits use
the same units as the target: 0.05 means 5% for these electrical efficiencies.
The current SWEET configuration applies the approved 5–30% range to three wood
CHP pathways: district heating, industrial CHP and industrial CHP with CCS.
Other entries, including fuel cells, heat pumps and non-combustion renewables,
have no configured limits. Built-in IAM efficiency transformations are unchanged.

`dev/stem_efficiency.py` calls Premise's `efficiency_bounds.py` helper, retaining
the original `stem_target_efficiency` and recording the `applied_target_efficiency`
and full bounds audit. Fuel/allocation accounting and CCS supplier tracing remain
in the adapter. Inventory scaling uses the applied target. `scenario_data.csv`
is unchanged: clipping is an explicit inventory-modelling assumption, not a
correction to the STEM source data. Zero/missing targets retain the existing
fallback; they are not automatically raised to the floor.

Neither heat nor electricity production series are required for clipping.
The optional `heat pathway` references another configured production pathway.
When both annual output series have compatible energy units, Premise reports
the combined ratio implied by the electrical target and heat/electricity ratio.
Missing outputs or unsupported units produce `not_available` and clipping still
works. This is a diagnostic, not an independent fuel-balance validation or an
automatic cap on combined CHP efficiency. Heating-value conventions remain
relevant to ratios above 100%.

`dev/validate_stem_build.py` independently calculates the configured clipping
from the source CSV and checks the applied physical efficiency. The paired-build
validator `dev/validate_external_efficiency_bounds.py` checks every exported A/B
matrix coefficient: only explicitly clipped provider columns may change, all
their non-production exchanges must change by raw/applied efficiency, and their
production exchanges must remain unchanged. Results for SPS3_soc2 (2040/2050)
are in `results/efficiency_bounds_fix_20261001`.

### Industrial gas fuel-cell source recovery (2026-10-01)

The exports allocate industrial gas fuel-cell fuel using a fixed reference to
`Electricity and CHPs_IN!$B$47`, whose saved value is zero. Consequently, both
exported allocated fuel inputs are zero and the exported electrical efficiency
is unavailable even when heat and electricity are produced. The saved labelled
`OUT_Electricity_CHP` rows contain the actual `ITOT-CHPGFC / VAR_fin / INGA`
fuel input. All 140 active source-year cases in the 40 Latest exports reconcile
to the corresponding labelled electricity and heat outputs. They imply 52–57%
electrical efficiency and 78–80% combined efficiency.

`dev/stem_fuel_cells.py`, called by `dev/format.py`, follows each electricity
export formula to select its external workbook cache and year. It verifies the
labelled electricity/heat against the exports before recovering fuel. Three
derived variables end in `|Recovered from STEM`: allocated electricity fuel,
allocated heat fuel, and electrical efficiency. With annual fuel F, electricity
E and heat H, these are F×E/(E+H), F×H/(E+H), and E/F. Inactive years with no
fuel or output remain zero. Missing fuel for positive output, invalid inputs,
ambiguous source rows and output mismatches fail explicitly. No assumed value,
clipping or backfilling is used. As elsewhere in this datapackage, efficiency
values are fractions despite the inherited `%` label; 0.57 means 57%.

All 93,960 existing formatted rows are retained; 120 derived rows are added.
The raw archive stays unchanged. `config.yaml` uses the recovered efficiency
for `gas (CHP fuel cell reformer)`. Premise interpolates it for 2045 using the
same rule as other absolute targets. The existing gas-engine inventory proxy
and its allocation are retained: recovery corrects its fuel efficiency, and
does not establish equivalence between engine and fuel-cell inventories.

`dev/validate_stem_build.py` checks the recovered source-year balances and
verifies the applied physical efficiency. The diagnostic paired-package check
in `dev/validate_fuel_cell_recovery.py` verifies every A/B coefficient: only
the fuel-cell provider's non-production exchanges may change, by the ratio
of previous to recovered efficiency. Production and all other providers must
remain unchanged. Evidence is stored in `results/fuel_cell_fix_20261001`.

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
in the previous batch. The subsequent full rebuild was stored in
`results/data_packages_ei312_20261002/`, with the final indicator rerun and
dashboard data in `results/indicators_ei312_20261003/`. These generated files were
removed during the 11 October cleanup.
