# SWEET-SURE repository handoff

## November 2026 STEM goal

**Status — drafted, inactive.** Begin the workflow when the user asks to run it.
Integrate the STEM delivery dated 24 November 2026, regenerate
`scenario_data/scenario_data.csv`, rebuild all 40 premise/Pathways datapackages,
and deliver a validated environmental indicator release.

### Start on a fresh machine

- Use the `ei311` workflow as the starting point. SWEET-SURE commit
  `11a4d20a75f62b5368499141afbea0bb333bc7ab` records the last validated
  baseline workflow and contains the current formatter, inventory adapters and
  batch drivers. If you start on `main`, bring this handoff into a working
  branch based on `ei311`. The historical README examples and formatter on
  `main` describe earlier workflows.
- Copy `dev/OneDrive_1_24-11-2026.zip` from the original machine. This delivery
  is a local input. Verify its SHA-256 before extracting it.

  ```text
  91714c5654e8f5eaefc581d601593956b8d8e113109cfbe35c568cb54a68a23a
  ```

- Transfer `dev/reference_data/nox_baseline_2020/`, including `provenance.json`,
  through an authorized local transfer. The builder uses these Git-ignored
  reference matrices for the independent NOx audit. Verify the matrix hashes
  against that provenance file. For comparison, also transfer the previous
  `results/sps_battery_corrected/refined_metals/` files `indicator_totals.csv`,
  `method_registry.json`, `provenance.json` and `completion_summary.json`.
  That release covers the four baselines. The old STEM inputs for all 40
  scenarios are tracked under `dev/STEM raw data/` on `ei311`.
- Set up the licensed ecoinvent 3.12 cutoff source and matching biosphere in
  Brightway. The prior project and source database were both named
  `ecoinvent-3.12-cutoff`, with biosphere `ecoinvent-3.12-biosphere`. Provide
  encrypted IAM access through the local `PREMISE_KEY` environment variable.
  Keep credentials, licensed matrices and the new delivery out of handoff
  commits.
- Use separate premise and Pathways environments as described in
  [Data package builds](dev/DATA_PACKAGE_BUILD.md) and
  [Indicator generation](dev/INDICATORS.md) on `ei311`. Start with premise
  `b4ea884ef09505cd06ab5bb7e4cf5b35161d725d` in a sibling checkout or worktree.
  Pathways must contain the numeric matrix reader fix `a491976`. The previous
  indicator run recorded Pathways 1.0.0 and Edges 1.2.5. Record exact revisions,
  package versions and input hashes for the new run. Confirm the NOx reference,
  Brightway databases, method assets and free disk space before starting the
  batch.

### 1. Extract and audit the scenario inputs

- Extract the workbooks into a versioned local staging directory and hash both
  the ZIP and each workbook. Expect four baselines (SPS1–SPS4) and 36 FIN,
  SOC and NUC sensitivities at Low, Mid and High levels. Build a crosswalk from
  the archive member and scenario sheet to the established ID. The existing
  formatter maps the baselines to `SPS1_bas0` through `SPS4_bas0` and the
  sensitivities to `fin1`–`fin3`, `soc1`–`soc3` and `nuc1`–`nuc3` within each
  SPS family. Verify the labels against the delivery and prior inputs.
- Audit all workbooks for gaps, missing cached formula values, Excel errors,
  duplicate rows, unexpected zeros or signs, unit changes, unmapped variables
  and sharp changes between years. Use the `check` sheets and reconcile key
  energy totals with their components. Compare each scenario with its previous
  counterpart and each new sensitivity with its new parent baseline. Trace
  flags to workbook cells and record whether they reflect a modeled change,
  a data issue or a verified transformation.
- Reuse `dev/format.py` on `ei311` and write a candidate CSV with `--folder`
  and `--output` before replacing the canonical file. Inspect the raw values
  before the formatter fills blanks or efficiencies. Check that the formatter
  and the workbook-dependent build adapters all use this delivery. Keep the
  previous workbooks available for comparison. Verify CSV, descriptor, config
  and demand-mapping coverage before freezing the inputs.
- Keep the supplied STEM years `2020, 2022, 2025, 2030, 2035, 2040, 2050`
  in the scenario CSV. Use the supplied 2035 column for the 2035 run. Keep the
  five-year model reporting schedule `2020, 2025, 2030, 2035, 2040, 2045,
  2050`. The build interpolates the 2045 STEM inputs between 2040 and 2050
  and calculates indicators from the resulting 2045 inventories and demands.

### 2. Rebuild and validate the Swiss inventories

- Use REMIND `SSP2-PkBudg1000`, ecoinvent 3.12 cutoff and the reporting
  years above. Run the input dry run in `dev/create_data_packages.py`, then
  pilot one baseline and one sensitivity with `dev/build_all_data_packages.py`.
  Include 2035 in both pilots. Use `--scenarios` to select pilots within the
  full batch, then resume the rest. Freeze inputs and software in a dated
  run directory and retain logs, hashes and per-scenario progress.
- Run automated checks on the exported inventory or package matrices for all
  40 scenarios and seven reporting years. Inspect representative Swiss
  datasets and every material flagged case at exchange level. Check the
  appropriate unit, reference product, location and supplier identity. Verify
  market shares, electricity trade and voltage levels, final-energy demand
  coverage, and routing to the intended Swiss technologies and market mixes.
- Independently calculate physical efficiencies or energy ratios from exported
  exchange amounts for representative technologies. Check fuel-to-output
  efficiency for power, heat and hydrogen, heat-pump COP, CHP fuel allocation
  and combined output, grid losses and storage round-trip efficiency. Check
  equipment scaling against service or energy demand, capacity, operating
  lifetime and replacements. Use technology-specific bounds and explicit
  allocation and unit conventions. Compare with the STEM target, physical
  balances or documented inventory assumptions, as applicable.
- Capture the scenario, year, dataset, expected and actual values, tolerance
  and source for each material discrepancy. Trace it to the STEM input,
  mapping or inventory assumption. Resolve material issues or record an
  evidence-based explanation before accepting the package. Confirm each of
  the 40 ZIPs opens in Pathways and contains all seven years.

### 3. Calculate, verify and deliver indicators

- Use `dev/run_indicator_batch.py` for the 16 EF 3.1 headline and 65 RELICS
  indicators, then `dev/run_refined_metals.py` for the 60 refined-element
  inputs and five alloy-mass indicators. Reuse the established method and
  coverage definitions and record any necessary changes. Run a pilot and
  check the calculation manifests reference the fresh packages before scaling
  up to all 40 scenarios.
- Export annual totals and contributions by sector, final-energy use and
  location with units and coverage metadata. Reconcile the contribution sums
  and independently verify all 40,880 scenario-year-indicator totals within
  documented tolerances. Compare the new baselines with the previous four and
  each new sensitivity with its current parent baseline. Trace material
  shifts, especially around 2035, to input, inventory or method changes.
- Deliver the updated scenario CSV and datapackage resources, 40 validated
  ZIPs, consolidated CSV and Parquet tables, method registry and an input,
  inventory and results validation report. Record commands, revision IDs,
  input and output hashes, crosswalk, coverage limitations and exceptions in
  the dated results directory. Keep the previous release available for
  comparisons and record any blocked checks explicitly.

**Completion criteria.** Every workbook maps to one scenario and one
validated ZIP. All 280 scenario-year combinations pass the applicable input
and inventory checks. All 146 indicators are complete and finite for every
combination, all totals reconcile and pass independent checks, and every
material anomaly has a recorded resolution or evidence-based explanation.
