"""Render the completed metal audit without changing any scientific results."""

import json
from html import escape
from pathlib import Path
import shutil
from importlib import metadata
import platform

import pandas as pd

from audit_metal_quantities import ROOT, digest, write_json

OUT = ROOT / "results/sps_base/metal_audit_20261012"
RESULTS = ROOT / "results/sps_base/refined_metals"


def table(frame):
    return (
        '<div class="table">'
        + frame.to_html(index=False, border=0, float_format=lambda x: f"{x:,.6g}")
        + "</div>"
    )


def run():
    num = json.loads((OUT / "numerical_summary.json").read_text())
    recheck = json.loads((OUT / "solver_backend_provenance.json").read_text())
    coeff = json.loads((OUT / "coefficient_summary.json").read_text())
    chemical = json.loads((OUT / "boundary_review_provenance.json").read_text())
    original = json.loads((RESULTS / "completion_summary.json").read_text())
    for name, expected in original["outputs_sha256"].items():
        assert digest(RESULTS / name) == expected
    assert num["processed_consumer_cases"] == 301 and num["adjoint_checks"] == 183820
    assert num["detailed_traces"] == 104 and coeff["raw_STEM_checks"] == 2828
    assert (
        coeff["raw_STEM_failures"]
        == coeff["heat_pump_failures"]
        == chemical["factor_failures"]
        == 0
    )
    flags = pd.read_csv(OUT / "solver_flag_rechecks.csv")
    flags["absolute_difference_kg"] = flags.difference_kg.abs()
    flags["relative_difference"] = (
        flags.difference_kg.abs() / flags.saved_edges_kg.abs()
    )
    # Keep strict flags; do not convert numerical near-agreement into exact passes.
    remaining = flags[~flags.passed]
    remaining.to_csv(OUT / "remaining_precision_flags.csv", index=False)
    systems = flags[(flags.kind == "system") & flags.saved_edges_kg.abs().gt(1)]
    sensitivities = (
        pd.read_parquet(OUT / "coefficient_sensitivities.parquet")
        .groupby(["scenario", "year", "sensitivity", "indicator"])
        .agg(
            change_kg=("change_kg", "sum"),
            current_system_kg=("current_system_kg", "first"),
        )
        .reset_index()
    )
    sensitivities["change_tonnes"] = sensitivities.change_kg / 1000
    sensitivities["change_percent"] = (
        100 * sensitivities.change_kg / sensitivities.current_system_kg
    )
    sensitivities.to_csv(OUT / "system_sensitivities.csv", index=False)
    sample = pd.read_csv(OUT / "sample.csv")
    checks = pd.read_parquet(OUT / "adjoint_checks.parquet")
    raw = pd.read_csv(OUT / "raw_STEM_demand_checks.csv")
    examples = []
    for r in sample[
        (sample.selection == "seeded random active")
        & sample.detailed
        & sample.year.isin([2020, 2050])
    ].to_dict("records"):
        match = checks[
            (checks.scenario == r["scenario"])
            & (checks.year == r["year"])
            & (checks.variable == r["variable"])
        ]
        traced = pd.read_parquet(OUT / "trace_reconciliation.parquet")
        indicator = (
            traced[
                (traced.scenario == r["scenario"])
                & (traced.year == r["year"])
                & (traced.variable == r["variable"])
            ]
            .iloc[0]
            .indicator
        )
        x = match[match.indicator == indicator].iloc[0]
        demand = raw[
            (raw.scenario == r["scenario"])
            & (raw.year == r["year"])
            & (raw.variable == r["variable"])
        ].iloc[0]
        examples.append(
            {
                "scenario": r["scenario"],
                "year": r["year"],
                "consumer": r["variable"].removeprefix("EXT - 0 - FE_"),
                "indicator": indicator,
                "annual_PJ": demand.raw_PJ,
                "kg_per_GJ": x.kg_per_GJ,
                "backcalculated_tonnes": demand.raw_PJ * x.kg_per_GJ * 1000,
                "saved_tonnes": x.saved_edges_kg / 1000,
            }
        )
    examples = pd.DataFrame(examples)
    examples.to_csv(OUT / "random_backcalculation_examples.csv", index=False)
    findings = [
        dict(
            id="F01",
            priority="High",
            status="Confirmed source omission",
            finding="Electric heavy-truck operation has no electricity input",
            evidence="Frozen lci-sweet_sure.xlsx rows 2242–2258 and exported CH heavy-truck columns; equipment is present, electricity is absent.",
            effect="SPS1/2050: adding 1/3.6 kWh per MJ raises copper by 1196.277 t (1.95293%) and steel/alloy input by 3397.335 t (1.42298%).",
            action="Add and validate the operating-electricity exchange in the foreground, then rebuild packages and all indicators together. Sensitivity uses the actual Swiss SPS low-voltage supplier.",
        ),
        dict(
            id="F02",
            priority="High",
            status="Equipment coverage limitation",
            finding="Car and several other end-use inventories omit vehicle glider/body or appliances",
            evidence="Electric car rows 1885–1901 include motor, battery and power electronics only. Electric-appliance/industrial-process wrappers are electricity-only; electric boilers omit the boiler itself.",
            effect="For SPS1/2050, each extra 100 kg of car glider across car-based electric demands would add 40012.44 t steel/alloy input and 461.11 t copper. This is a scaling sensitivity, not an estimate of actual missing glider mass.",
            action="Define equipment coverage consistently and supply technology-specific masses. Do not use these totals as complete equipment-material demand.",
        ),
        dict(
            id="F03",
            priority="High",
            status="Source formula/documentation mismatch",
            finding="Electric-car formulas use 0.75 MJ/km; metadata says 0.703 MJ/km",
            evidence="Cells B1896, B1898, B1900 and B1901 divide equipment by 200000 km and 0.75 MJ/km; B1887 documents 703 kJ/km.",
            effect="Using the comment raises these four coefficients by 6.6856%. SPS1/2050 system lithium +88.242 t (+6.06393%); copper +855.415 t (+1.39647%). Hard-coded converter and distribution-unit coefficients were left unchanged in this sensitivity.",
            action="Choose the authoritative energy/lifetime assumptions and regenerate all equipment coefficients from one parameter set.",
        ),
        dict(
            id="F04",
            priority="Medium",
            status="Source formula/documentation mismatch",
            finding="Electric-truck battery formula uses 710000 km; metadata says 1000000 km",
            evidence="B2258 =283/710000/5.45; B2250 documents 283 kWh, 1000000 km and 5447 kJ/km.",
            effect="The current battery coefficient is about 40.77% above the comment-based coefficient. Applying the documented coefficient reduces SPS1/2050 system lithium by 12.998 t (0.8932%).",
            action="Reconcile lifetime and energy basis; document any intentional battery replacement multiplier.",
        ),
        dict(
            id="F05",
            priority="High",
            status="Technology proxy",
            finding="Electric vans and other electric transport use passenger-car coefficients",
            evidence="Frozen configuration and exported final-energy mappings point to passenger-car activity clones.",
            effect="These demands inherit the same 48 kWh battery, motor coefficients, omissions and car-assumption sensitivity.",
            action="Replace or explicitly justify passenger-car proxies using service-specific vehicle size, lifetime, energy use and batteries.",
        ),
        dict(
            id="F06",
            priority="High",
            status="Sensitive prospective assumption",
            finding="Li–S is a small capacity share with a large lithium contribution",
            evidence="SPS1/2050 MIX: 5.1% Li–S capacity; source Li–S cells contain 0.160869565 kg Li foil/kg cell and module cell share 0.751591847.",
            effect="Li–S contributes about 31.9% of the car-battery lithium branch. Removing it and renormalizing the remaining mix reduces modeled system lithium by 26.6–27.7% across the four 2050 scenarios.",
            action="Review application suitability and prospective chemistry shares. This sensitivity is not a proposed forecast or proof the Li–S coefficient is erroneous.",
        ),
        dict(
            id="F07",
            priority="High",
            status="Interpretation limit",
            finding="Lifetime-attributed commodity deliveries are not annual equipment purchases or elemental stocks",
            evidence="Equipment coefficients divide production inventories by lifetime energy throughput. Selected products and allocated background inventories define the counted boundary.",
            effect="The results include upstream manufacturing losses and ancillary inputs; generic-alloy element contents and some compounds are absent; indicators can overlap.",
            action="The user selected lifetime-attributed metal use with complete equipment coverage. Complete and document that coverage while retaining the lifetime allocation basis.",
        ),
        dict(
            id="F08",
            priority="Low to medium",
            status="Numerical precision limit",
            finding="Some strict cross-solver flags remain after rechecking",
            evidence=f"{recheck['rechecks']} flagged checks re-solved with SuperLU; {recheck['failures']} still exceed rtol=1e-8, atol=1e-6 kg. Original flags retained.",
            effect=f"Worst rechecked annual relative difference for totals above 1 kg: {100*systems.relative_difference.max():.6g}%; largest absolute annual difference {systems.absolute_difference_kg.max():.6g} kg. A near-zero gallium total changes sign by a few milligrams.",
            action="Avoid excessive significant figures. Retain flags and use numerically scaled/high-precision checks if trace-level values become decision-relevant.",
        ),
        dict(
            id="F09",
            priority="Information",
            status="Checks support current implementation",
            finding="Demand units, selected-product arithmetic and electric heat-pump electricity balance reconcile",
            evidence="2828 raw STEM demand checks, 168 independent mass-factor checks and 32 endpoint electric-heat-pump energy checks pass.",
            effect="No PJ-to-MJ or kg-to-tonne error found. Direct refinery supplies and market routes are included; selected internal transfers excluded.",
            action="Retain these checks in future rebuilds; they do not certify omitted equipment or every chemical identity.",
        ),
    ]
    pd.DataFrame(findings).to_csv(OUT / "findings.csv", index=False)
    summary = {
        "verdict": "Numerically consistent at useful system precision, but not validated as complete physical energy-system metal demand. Source omissions and modelling assumptions require correction or explicit acceptance.",
        "original_results_unchanged": True,
        "random_seed": 20261012,
        "sampled_consumers": 301,
        "detailed_traces": 104,
        "raw_STEM_checks": 2828,
        "chemical_factor_checks": 168,
        "heat_pump_checks": 32,
        "strict_initial_flags": num["adjoint_failures"]
        + num["location_failures"]
        + num["system_failures"],
        "strict_flags_rechecked": recheck["rechecks"],
        "strict_remaining_flags": recheck["failures"],
        "max_rechecked_system_relative_difference_above_1kg": float(
            systems.relative_difference.max()
        ),
        "max_rechecked_system_absolute_difference_kg": float(
            systems.absolute_difference_kg.max()
        ),
        "findings": findings,
        "primary_secondary_analysis": "Excluded as requested",
        "test_result": "3 analytical-network tests passed; market/direct routes, non-unit production, signed cycles and unit conversion",
        "interpretation_question": "User confirmed: lifetime-attributed metal use with complete equipment coverage. Current inventories do not yet meet the coverage requirement.",
    }
    write_json(OUT / "audit_summary.json", summary)
    sections = []

    def add(title, body):
        sections.append("<section><h2>" + escape(title) + "</h2>" + body + "</section>")

    add(
        "Verdict",
        "<p><strong>"
        + escape(summary["verdict"])
        + "</strong></p><p>The user confirmed lifetime-attributed metal use with complete equipment coverage as the acceptance criterion.</p><p>No material-counting or unit-conversion implementation error was found in the tested scope. The inventory itself is incomplete for a full system-material claim. The calculations below identify concrete issues and their magnitudes. Original datapackages, Edges outputs and dashboard values are unchanged; all counterfactuals are explicitly unapplied. Primary versus secondary shares are outside this audit.</p>",
    )
    add(
        "Coverage and independent calculation",
        f'<p>All four SPS base scenarios; seven years, 2020–2050; 101 final-energy uses; 60 element and five alloy indicators. Seed <code>20261012</code>: 112 random active consumers (one per sector per scenario-year), 28 random inactive consumers and 161 additional targeted cases. There are 56 random and 48 targeted detailed metal traces.</p><p>{num["adjoint_checks"]:,} demand–indicator checks, {num["location_checks"]:,} sampled regional checks, {num["system_checks"]:,} annual system checks. All 2,828 final-energy demands were separately traced to the original STEM workbooks, including interpolation and the exact 10⁹ MJ/PJ conversion.</p><p>The audit reads the raw matrix CSVs directly without importing Edges or Pathways calculation helpers. For material m it constructs a coefficient vector c from selected inputs crossing the boundary, then independently solves <code>Aᵀ z = c</code>. Each final demand D predicts <code>D × z[root]</code>. Forward solves and explicit quantity × coefficient supply-chain trees provide additional checks. Non-unit production is normalized, signed credits are retained, and unexpanded/cyclic branches remain explicit residuals.</p><p>This tests a separate implementation of the chosen boundary. Because the selected products come from the same method registry, boundary completeness and chemical identity need the separate source review below.</p>',
    )
    add(
        "Numerical findings",
        f'<p>The first UMFPACK pass produced {summary["strict_initial_flags"]:,} strict flags, including small cancellation-sensitive differences. All were rechecked from raw CSV matrices with SuperLU at the original tolerance (<code>rtol=10⁻⁸, atol=10⁻⁶ kg</code>). {summary["strict_initial_flags"]-recheck["failures"]:,} resolved; {recheck["failures"]} remain flagged. They are not relabelled as passes.</p><p>The largest remaining annual relative difference for totals above 1 kg is <strong>{100*systems.relative_difference.max():.6f}%</strong> (tin, SPS2/2040: approximately 9.80 kg out of 137,165 kg). The largest absolute difference is 27.78 kg of steel/alloy out of approximately 231 million kg. A trace gallium result near zero changes sign by roughly 4 mg. These indicate numerical sensitivity, not an observed mapping failure; the report does not claim exact solver agreement or certify trace values. The percent-scale inventory effects below dominate these numerical differences.</p><p><a href="remaining_precision_flags.csv">All remaining strict flags</a> · <a href="solver_flag_rechecks.csv">Every solver recheck</a> · <a href="numerical_summary.json">Original numerical summary</a></p>',
    )
    for f in findings:
        add(
            f["id"] + " · " + f["finding"],
            '<p class="tag">'
            + escape(f["priority"] + " · " + f["status"])
            + "</p><p>"
            + escape(f["evidence"])
            + "</p><p><strong>Effect:</strong> "
            + escape(f["effect"])
            + "</p><p><strong>Action:</strong> "
            + escape(f["action"])
            + "</p>",
        )
    add(
        "Random worked examples",
        "<p>These eight examples are the independently selected detailed random consumers at 2020 and 2050. The arithmetic is <code>PJ/year × kg/GJ × 1,000 = tonnes/year</code>. Intensities are independently solved from the original inventory; all 301 sampled cases and 104 detailed traces remain available in the machine-readable files.</p>"
        + table(examples)
        + '<p><a href="random_backcalculation_examples.csv">Download examples</a> · <a href="sample.csv">Complete fixed sample</a> · <a href="supply_chain_steps.parquet">Detailed branch calculations</a></p>',
    )
    equip = pd.read_parquet(OUT / "equipment_branch_backcalculations.parquet")
    car = equip[
        (equip.scenario == "SPS1_bas0")
        & (equip.year == 2050)
        & (equip.variable == "EXT - 0 - FE_cars_electricity")
        & (equip.indicator == "Refined-metal inputs - Lithium")
    ]
    add(
        "Worked car back-calculation",
        "<p>SPS1/2050 passenger-car electricity is about 38.20697 PJ/year. The source coefficient <code>48 kWh / (200,000 km × 0.75 MJ/km) = 0.00032 kWh/MJ</code> implies about 12.226 GWh/year of lifetime-attributed battery capacity, or about 254,713 48-kWh vehicle equivalents. These are not observed annual vehicle sales.</p>"
        + table(
            car[
                [
                    "supplier_name",
                    "input_per_MJ",
                    "annual_input_quantity",
                    "input_intensity_kg_per_unit",
                    "branch_kg",
                ]
            ]
        )
        + "<p>The battery branch contributes about 966,336 kg Li to the car total of 974,966 kg. Its Li–S contribution is approximately 308,576 kg: capacity demand × 5.1% × approximately 0.495 kg Li/kWh. Independently following module mass, cell fraction and lithium-foil input gives the same dominant chain. High Li–S lithium use therefore has an inventory explanation; suitability of the assumed future mix remains a modelling question.</p>",
    )
    hp = pd.read_csv(OUT / "heat_pump_equipment_backcalculation.csv")
    add(
        "Worked heat-pump back-calculation",
        "<p>SPS1/2050 residential space-heating heat pumps consume about 16.6403 PJ electricity and supply heat at COP 4.073536. The operating input is exactly 0.2777778 kWh per MJ final electricity after the two foreground coefficients are multiplied. The heat-production input of roughly 1/900,000 pump units per MJ heat implies about 75,241 lifetime-attributed pump equivalents. At 22.4703 kg copper input per pump, hardware contributes about 1,690,687 kg Cu; electricity contributes about 2,563,842 kg Cu; refrigerant supply adds about 1,687 kg Cu. Their sum reconciles with approximately 4,256,216 kg Cu.</p><p>The heat-production name is air–water while the hardware supplier is a brine–water 10 kW pump. That is an inherited equipment proxy to review, even though the energy arithmetic passes.</p>"
        + table(
            hp[hp.indicator == "Refined-metal inputs - Copper"][
                [
                    "supplier",
                    "input_per_heat_MJ",
                    "annual_input",
                    "kg_per_input_unit",
                    "branch_kg",
                ]
            ]
        ),
    )
    view = sensitivities[
        (sensitivities.year == 2050)
        & sensitivities.indicator.isin(
            ["Refined-metal inputs - Copper", "Refined-metal inputs - Lithium"]
        )
    ][["scenario", "sensitivity", "indicator", "change_tonnes", "change_percent"]]
    add(
        "System sensitivities",
        "<p>Each row is a separate counterfactual relative to that scenario’s original total. Do not add counterfactuals together: some overlap. No counterfactual is an accepted replacement forecast. The battery experiment changes direct final-energy battery purchases; it is not a global substitution of every battery market in the economy.</p>"
        + table(view)
        + '<p><a href="system_sensitivities.csv">All eight endpoint scenario-year sensitivities, ten indicators</a> · <a href="glider_per_100kg_sensitivity.csv">Glider sensitivity per 100 kg</a></p>',
    )
    add(
        "Chemical factors and boundary review",
        '<p>All 168 product–indicator factors (167 distinct products) reproduce independently from the declared formulas or documented fractions. This is an arithmetic check, not a claim that every product’s chemistry was independently remeasured. The reviewed high-contribution factors support the current assignments: ordinary LiOH is anhydrous (about 0.2898 kg Li/kg), battery-grade LiOH is monohydrate (about 0.1654), Li₂CO₃ about 0.1879, nickel sulfate NiSO₄ about 0.3793 and copper sulfate CuSO₄ about 0.3981. Neodymium oxide uses the explicit dataset content 0.8574; cobalt sulfate uses the source inventory’s 0.38 kg Co/kg product. Dry-reference solution products must not receive an extra solution-concentration multiplier.</p><p>Barium hydroxide required special scrutiny: its production prose discusses octahydrate crystallization, but its 0.941955 kg BaO input per kg product at 95% yield agrees with anhydrous-reference normalization (0.941959), not an octahydrate mass basis (0.511624). The source’s BaH₂O₂/CAS identity supports retaining the current factor. This matters because that product contributes about 95% of the SPS1/2050 barium indicator. Praseodymium oxide remains an explicit Pr₆O₁₁ convention; Pr₂O₃ would increase its factor by about 3.2%. Several elements and mixed rare-earth products remain unquantified, as recorded in the registry.</p><p>Market-to-market and selected refining/conversion transfers are excluded, including direct refining routes that bypass markets. This avoids obvious repeated market counting. It does not establish a unique-atom material balance across every unselected chemical transformation or allocated co-product inventory. Refined-element results and alloy-mass results overlap and cannot be summed into “total metals”. Steel mass is not kg Fe, and unspecified aluminium-alloy mass is not kg Al.</p><p><a href="chemical_factor_checks.csv">Every factor check</a> · <a href="barium_basis_evidence.json">Barium basis evidence</a> · <a href="product_boundary_contributions.parquet">Counted and excluded product contributions</a></p>',
    )
    add(
        "Source assessment",
        '<p>Atomic weights were checked against <a href="https://ciaaw.org/abridged-atomic-weights.htm">CIAAW’s 2024 abridged table</a>. The <a href="https://support.ecoinvent.org/system-models">ecoinvent system-model documentation</a> explains the allocated background on which these intensities depend; allocated LCA inventories should not be interpreted as unallocated physical stock balances.</p><p>The Li–S inventory cites <a href="https://pubs.acs.org/ascecg/article/11/26/9553/333846/Prospective-Life-Cycle-Assessment-of-Lithium">Wickerts et al. (2023)</a>, a prospective study for stationary storage. Its emphasis on specific energy and performance supports testing these assumptions; it does not independently validate their transfer to road vehicles. The model’s 2050 Li–S capacity supplier uses about 3.92157 kg module/kWh. Its source workbook gives 0.751592 kg cells/kg module and 0.160870 kg lithium foil/kg assembled cell. These coefficients explain the large lithium intensity without a kilogram/gram conversion error.</p><p>The <a href="https://www.iea.org/data-and-statistics/charts/minerals-used-in-electric-cars-compared-to-conventional-cars">IEA vehicle benchmark</a> covers an entire vehicle, including the glider, and assumes a 75 kWh NMC622 battery. Direct comparison with this model’s partial 48 kWh vehicle inventory and changing battery mix would be misleading. Accordingly, it is used to assess comparability of scope, not to declare the present coefficients valid or invalid from one headline number.</p>',
    )
    add(
        "Reproduction and evidence",
        '<p>Run in the Pathways Python environment from the repository root, in this order:</p><pre>python dev/audit_metal_quantities.py\npython dev/audit_metal_coefficients.py\npython dev/audit_metal_boundary_review.py\npython dev/audit_metal_solver_precision.py\npython dev/report_metal_audit.py\npython -m pytest -q tests/test_audit_metal_quantities.py</pre><p>The folder contains frozen executed scripts, source hashes, unchanged-result hashes, sample plan, raw workbook checks, all detailed traces and unapplied sensitivities. The three analytical-network tests cover direct and market supplies, market losses, non-unit production, signed cycles and unit-conversion rejection. <a href="goal_definition.json">Goal definition</a> · <a href="audit_summary.json">Audit summary</a> · <a href="findings.csv">Findings and proposed actions</a> · <a href="foreground_workbook_evidence.json">Source workbook cells and formulas</a>.</p><p>The audit is complete as an assessment. It does not certify complete system material demand. The user confirmed lifetime-attributed metal use with complete equipment coverage. Remediation therefore requires adding the omitted equipment and operating input, reconciling authoritative vehicle assumptions, and then rebuilding the packages and all indicators. A new annual stock-flow model is outside the requested interpretation.</p>',
    )
    html = (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SPS metal quantities audit — 12 October 2026</title><style>body{font:16px/1.6 system-ui,sans-serif;color:#183039;background:#f4f6f6;margin:0}main{max-width:1150px;margin:auto;padding:32px 24px}h1{line-height:1.2}h2{font-size:1.3rem;line-height:1.35}section{background:white;padding:20px 26px;margin:20px 0;border-radius:8px;border:1px solid #dde4e6}.tag{color:#805400;font-weight:600}.table{overflow:auto}table{border-collapse:collapse;font-size:13px;width:100%}td,th{padding:9px;border-bottom:1px solid #d8e0e2;text-align:left;vertical-align:top}th{background:#edf3f3}a{color:#00677b}code,pre{background:#eef3f4}pre{padding:16px;overflow:auto}.subtitle{color:#4d646d}@media print{body{background:white}section{break-inside:avoid;padding:10px;border:0}}</style><main><h1>SPS metal quantities audit</h1><p class="subtitle">12 October 2026 · Independent numerical and source-inventory review · SPS1–SPS4 base scenarios</p>"""
        + "".join(sections)
        + "</main></html>"
    )
    (OUT / "audit_report.html").write_text(html)
    software = {
        p: metadata.version(p)
        for p in ["numpy", "scipy", "pandas", "scikit-umfpack", "openpyxl", "PyYAML"]
    }
    write_json(
        OUT / "audit_environment.json",
        {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": software,
        },
    )
    for name in [
        "audit_stem_input_chain.py",
        "workflow_audit_core.py",
        Path(__file__).name,
    ]:
        shutil.copy2(ROOT / "dev" / name, OUT / name)
    write_json(
        OUT / "artifact_manifest.json",
        {
            "files_sha256": {
                p.name: digest(p)
                for p in sorted(OUT.iterdir())
                if p.is_file() and p.name != "artifact_manifest.json"
            },
            "original_results_unchanged": True,
        },
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "findings"}, indent=2))


if __name__ == "__main__":
    run()
