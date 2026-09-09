# ecoinvent 3.12 compatibility validation

This branch targets ecoinvent 3.12 cut-off. Validation uses Brightway project
`ecoinvent-3.12-cutoff`, source database `ecoinvent-3.12-cutoff` (26,533 datasets),
biosphere database `biosphere`, and premise 2.5.1.

## Changes

- Keep the workbook source version at 3.10 in `datapackage.json`. Premise migrates
  its exchanges through 3.11 to 3.12, including one-to-many supplier replacements.
  This field is not the target database version.
- Declare the inventory resource as XLSX, and require premise 2.5.1 or newer.
- Update the wood-chip production alias and replacement selector from `wet` to
  `green`, following the 3.11-to-3.12 cut-off migration mapping distributed with premise.
- Correct the Swiss multi-Si rooftop photovoltaic supplier's malformed name.
  Only one shared string in the XLSX changes; all other ZIP members, including
  formulas, cached values, styles and parameter cells, are byte-identical.
- Use the current bundled BIGCC **post-combustion** CCS dataset instead of the
  unavailable pre-combustion alias. Pipeline distance and storage depth remain
  200 km and 1,000 m. This is a technology substitution, so BECCS results need not
  reproduce the previous branch.
- Strip trailing whitespace from rail and coach variable labels. Comparing the
  old and new CSV after stripping labels gives identical data frames: no scenario
  amounts, units, years, regions or scenario identifiers change.
- Use premise's current configuration and scenario validators in the tests;
  load the inventory with `ExcelImporter` instead of `CSVImporter`.

## Reproduction and scope

Validation completed on 2026-09-09: **5 tests passed**. The no-write build contains
**30,019 datasets and 27 Swiss SPS markets**, with zero unresolved or ambiguous
technosphere links, zero unresolved biosphere links, zero non-finite amounts and
zero missing markets with positive scenario volumes. See `ei312-validation.json`
for the input checksums and machine-readable results.

From the repository root, in a Python environment with the licensed source
database available:

```bash
python -m pytest tests -q
export PREMISE_KEY="<your IAM decryption key>"
python dev/validate_ei312.py --scenario SPS1_bas0 --year 2050
```

`--use-cache` reuses premise's source and bundled-inventory caches. The custom
datapackage is loaded again. The IAM file is encrypted and the key is supplied
explicitly. No Brightway result database is written.

The JSON report records input checksums, scenario metadata, dataset counts and
link validation. This is an external-sector build with REMIND `SSP2-NPi` for
`SPS1_bas0`, 2050. It does not certify every scenario/year combination, run full
IAM sector updates, or verify LCIA scores. The test suite checks required-variable
coverage for every scenario declared in the datapackage.

Premise emits four efficiency-factor warnings (approximately 2.262 and 2.870)
for natural-gas CHP datasets. Their scenario assumptions are retained. It also
skips the light- and heavy-fuel-oil markets because their 2050 production volumes
are zero; the validator requires markets with positive scenario volumes.

References: [premise supported configurations](https://premise.readthedocs.io/en/latest/reference/compatibility.html)
and [inventory migration and catalogue](https://premise.readthedocs.io/en/latest/reference/inventories.html).
