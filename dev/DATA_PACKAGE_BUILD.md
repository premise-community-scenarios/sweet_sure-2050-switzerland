Build STEM Pathways packages in the `premise` environment with:

```sh
python dev/create_data_packages.py --scenarios SPS1_bas0
```

Defaults are Brightway project/database `ecoinvent-3.12-cutoff`, biosphere
`ecoinvent-3.12-biosphere`, REMIND `SSP2-PkBudg1000`, and 2020–2050 in five-year
steps. The 2045 inputs are interpolated. Omit `--scenarios` to build all 40
scenarios. `--dry-run` validates inputs without loading Brightway. `--years`
limits diagnostic builds; `--output-dir` keeps their artifacts separate.
The IAM key is read from `PREMISE_KEY`, `--key`, or the existing notebook literal.

For a resumable batch of all 40 scenarios, use the premise environment:

```sh
python dev/build_all_data_packages.py --workers 3
```

The default output directory is `dev/data_packages_ei312_20260928`. Inputs and
premise source are copied into it before the run, and each worker uses its own
premise cache. `STATUS.md` and `progress.json` show progress. Each scenario keeps
its build and validation logs under `scenarios/`; ZIPs appear in `packages/` only
after matrix, efficiency, PV, native final-energy coverage, and NOx checks pass.
Existing background warnings and missing STEM efficiency targets are recorded.
The batch does not calculate the indicator tables.

The already tested SPS1 package is reused only when its input, code and package
checksums match. Repeating the command resumes unfinished work and verifies
completed ZIP checksums; add `--retry-failed` to retry failed scenarios. A lock
prevents duplicate coordinators, and a live child from an interrupted run must
finish before resuming. `--prepare-only` freezes the batch without starting builds.

Use `--scenarios SPS2_bas0 --workers 1` for a pilot within a new batch directory;
run again without `--scenarios` to process the remaining scenarios. A successful
pilot leaves the batch status `partial`, with the other scenarios pending.
If a build reports dropped biosphere flows, its worker cache is quarantined and
that worker stops. A later scenario cannot silently reuse that cache and pass
validation just because the import warnings were absent from its log.

The 2026-09-28 batch predates the biosphere migration correction and must be
replaced. The corrected batch is `dev/data_packages_ei312_20260929_biosphere_fixed`.
The premise fix matches versioned biosphere UUIDs/full descriptors and updates
replacement compartments. It also changes the inventory cache version; source
ecoinvent caches remain reusable. The audit in `dev/audit_biosphere_imports.py`
checks the same 80 default workbooks selected for ecoinvent 3.12. Pass `--compare`
with an older audit JSON to report changed links and preserved amounts.

PV uses premise's `lci-PV-2026.xlsx` and `lci-PV-2026-electricity.xlsx` inventories
(IEA PVPS Task 12, 2026). Industry/services use the Swiss commercial mix;
households use the Swiss residential mix. Large-scale PV uses the 10 MWp
single-crystalline ground-mounted reference system at 1000 kWh/kWp/year,
regionalized to CH, because the supplied file has no Swiss utility-scale mix.
The at-plant reference product is retained. BECCS uses the available biomass
IGCC post-combustion inventory, as approved on 2026-09-28.

The build applies the absolute STEM efficiency targets through
`stem_efficiency.py`. For allocated source inventories, physical fuel demand
is allocated fuel demand divided by the production allocation factor. Fuel
energy uses premise's fuel properties. CCS wrappers trace their generating
suppliers. Custom waste inventories have no explicit waste-fuel exchange, so
their documented combustion efficiencies provide the baseline; their auxiliary
energy use is not treated as the waste fuel. Existing allocation is retained.
Input and emission amounts are scaled by physical baseline efficiency divided
by the STEM target, without clipping the target or substituting relative trends.
The adapter also restores each pathway's efficiency binding after premise
creates duplicate suppliers, and regionalizes an absolute-efficiency supplier
when the scenario region has no matching dataset.

Each adjustment is checked and recorded in `<package>.efficiencies.json`,
including the baseline, target, resulting efficiency, factor, and basis. For
waste inventories this is a consistency check against the declared baseline,
not an independent measurement of waste fuel input. Zero efficiency values are
left neutral, following premise's convention. The validation report flags any
positive-generation pathway with a zero efficiency target; the proxy remains
uncalibrated in that case.

`premise_compat.py` selects activity descriptors for the Pathways mapping export.
The installed premise version also stores metal-sector decisions and metrics
in that structure; these remain available for reports and validation, but are
not activity suppliers. Both adapters are scoped to the build and restore the
original methods afterward. They do not alter the installed premise source or
the source Brightway inventory.

A successful build produces a ZIP and a `.build.json` provenance file. Validate
its matrices and solve a Swiss electricity demand for every year with:

```sh
python dev/audit_pathways_package.py path/to/package.zip
```

The audit also accepts an unzipped matrix directory. Check `unlinked.log`, the
build log, and the `Validation Findings` sheets under `export/change reports/`.
A completed export or successful matrix solve does not imply that premise's
background-dataset warnings have been resolved.

For the complete STEM check (input hashes, every configured non-zero absolute
efficiency target, PV shares, matrix solves, and premise validation findings),
run `python dev/validate_stem_build.py path/to/package.zip`.

After rebuilding with the premise mapping fixes, run
`python dev/inspect_regenerated_package.py new/package.zip previous/package.zip`.
This checks ZIP integrity, all final-energy mappings directly in the new export,
and every exported final-energy demand against the original STEM series, including
interpolation. It also compares scenario data and both inventory matrices across
all years. Activities and biosphere flows are aligned by identity before comparing
coefficients, so reordered matrix indices do not count as numerical changes.
The `.comparison.json` report records added/removed mappings and coefficient
differences at `rtol=1e-12`, `atol=1e-15`. It compares deterministic coefficients;
it does not compare exchange uncertainty metadata. Neither package is modified.

The local premise checkout also contains a GAINS emissions fix: each pollutant
factor is applied to every matching biosphere exchange, including all air
compartments, exactly once. The previous updater marked a pollutant as processed
after its first exchange and skipped later compartments. Both the dictionary and
compact-store paths now take the already-processed pollutant set before the pass.
Rebuild from the source inventory to apply this correction; reapplying GAINS to
an old transformed inventory would encounter its existing processed markers.
The corrected run records the premise source hashes and patch in
`gains_fix_provenance.json` and `premise_emissions_fix.diff`.

Check the final gasoline EURO-6 car inventories with
`dev/audit_gains_car_emissions.py`. It takes the previous run's 2020 matrix
directory, the corrected run's `pathways_temp` directory, and `--output` for the
JSON audit. It checks each NOx compartment against the independent 2020 baseline,
the change in fuel use, and the region/year GAINS factor. Its aggregate HBEFA
comparison includes GAINS reductions; the original car-stage reference should
not be reapplied unchanged to final, emissions-adjusted inventories.
