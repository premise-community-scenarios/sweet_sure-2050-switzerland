#!/usr/bin/env python3
"""Render the current, qualified equipment/metal audit without historical claims."""

import argparse
from html import escape
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def render(base):
    out = base / "metal_audit"
    numerical = json.loads((out / "numerical_summary.json").read_text())
    equipment = json.loads((out / "equipment_review_summary.json").read_text())
    solver = json.loads((out / "solver_backend_provenance.json").read_text())
    audit_log = base / "independent_audit.log"
    conditioning_warnings = (
        audit_log.read_text().count("UmfpackWarning: (almost) singular matrix!")
        if audit_log.exists()
        else 0
    )
    conditioning_note = (
        f"<p>The independent solver also emitted {conditioning_warnings} near-singularity "
        "warnings. These are retained in the <a href='../independent_audit.log'>audit log</a>; "
        "agreement at the stated result tolerances does not establish that the inventory "
        "matrices are well-conditioned. Before/after residual diagnostics remain available "
        "with the calculations.</p>"
        if conditioning_warnings
        else ""
    )
    coverage = pd.read_csv(base / "vehicle_audit/equipment_coverage.csv").fillna("")
    aggregate_profiles = []
    for path in sorted(
        (base / "build/packages").glob("*.aggregate_electric_validation.json")
    ):
        validation = json.loads(path.read_text())
        assert validation["passed"], "Aggregate equipment validation failed"
        aggregate_profiles.append(validation["profiles"])
    assert len(aggregate_profiles) == 4
    assert all(p == aggregate_profiles[0] for p in aggregate_profiles[1:])
    aggregate_coverage = pd.DataFrame(
        [
            {
                "Other-electricity use": profile["id"].capitalize(),
                "Energy with some identified components (%)": 100
                * profile["fraction with some identified components"],
                "Energy with no added equipment (%)": 100
                * profile["fraction without equipment"],
            }
            for profile in aggregate_profiles[0]
        ]
    )
    aggregate_coverage.to_csv(out / "aggregate_equipment_coverage.csv", index=False)
    totals = pd.read_csv(out / "old_new_indicator_totals.csv")
    sensitivities = pd.read_parquet(out / "coefficient_sensitivities.parquet")
    methods = json.loads((base / "refined_metals/method_registry.json").read_text())[
        "methods"
    ]
    labels = {m["indicator"]: m["label"] for m in methods}
    selected = {
        m["indicator"]
        for m in methods
        if "definition" in m
        and m["label"]
        in [
            "Copper",
            "Lithium",
            "Nickel",
            "Cobalt",
            "Neodymium",
            "Aluminium alloys",
            "Iron and steel alloys",
        ]
    }
    comparison = totals[
        totals.impact_category.isin(selected) & totals.year.eq(2050)
    ].copy()
    comparison["indicator"] = comparison.impact_category.map(labels)
    for c in ["value_old", "value_new", "difference"]:
        comparison[c] /= 1000
    comparison = comparison[
        ["scenario", "indicator", "value_old", "value_new", "difference"]
    ].rename(
        columns={
            "value_old": "previous tonnes",
            "value_new": "updated tonnes",
            "difference": "change tonnes",
        }
    )
    attribution = pd.read_parquet(out / "old_new_attribution.parquet")
    components = [
        "demand",
        "foreground_coefficients",
        "upstream_intensities",
        "added_suppliers",
        "removed_suppliers",
        "direct_boundary",
    ]
    system_attribution = attribution.groupby(
        ["scenario", "year", "indicator"], as_index=False
    )[["difference_kg", *components, "residual_kg"]].sum()
    system_attribution.to_csv(out / "old_new_system_attribution.csv", index=False)
    attribution_example = system_attribution[
        system_attribution.scenario.eq("SPS1_bas0")
        & system_attribution.year.eq(2050)
        & system_attribution.indicator.isin(selected)
    ].copy()
    attribution_example["material"] = attribution_example.indicator.map(labels)
    attribution_example[["difference_kg", *components]] /= 1000
    attribution_example = attribution_example[
        ["material", "difference_kg", *components]
    ].rename(
        columns={
            "difference_kg": "total change",
            **{c: c.replace("_", " ") for c in components},
        }
    )
    effects = sensitivities.groupby(
        ["scenario", "year", "sensitivity", "indicator"], as_index=False
    ).agg(change_kg=("change_kg", "sum"), system_kg=("system_kg", "first"))
    effects["change_percent"] = (
        100 * effects.change_kg / effects.system_kg.where(effects.system_kg.abs() > 1)
    )
    effects = effects[effects.year.eq(2050) & effects.change_percent.notna()].copy()
    effects["material"] = effects.indicator.map(labels)
    effects["change tonnes"] = effects.change_kg / 1000
    all_material_effects = effects.sort_values(
        "change_percent", key=lambda s: s.abs(), ascending=False
    ).head(20)[
        ["scenario", "material", "sensitivity", "change tonnes", "change_percent"]
    ]
    effects = effects[effects.indicator.isin(selected)]
    effects = effects.sort_values(
        "change_percent", key=lambda s: s.abs(), ascending=False
    ).head(32)
    effects = effects[
        ["scenario", "material", "sensitivity", "change tonnes", "change_percent"]
    ]
    sample = pd.read_csv(out / "sample.csv")
    verification_status = (
        f"{solver['failures']:,} solver discrepancies remain unresolved."
        if solver["failures"]
        else (
            f"No unresolved numerical discrepancies after {solver['rechecks']:,} strict solver rechecks."
            if solver["rechecks"]
            else "No numerical discrepancies in the independent checks."
        )
    )
    heat_paths = pd.read_parquet(out / "heat_equipment_paths.parquet")
    unresolved_heat = heat_paths[heat_paths.status.ne("identified equipment input")][
        ["variable", "provider", "status"]
    ].drop_duplicates()
    branches = pd.read_parquet(out / "equipment_branches.parquet")
    examples = branches[
        branches.scenario.eq("SPS1_bas0")
        & branches.year.eq(2050)
        & branches.variable.str.contains(
            "cars_electricity|heavy_duty_truck_electricity|residential_water_heating_electric_boiler|services_other_electric|industry_process_FE_other_electric"
        )
        & branches.indicator.map(labels).isin(["Copper", "Lithium", "Neodymium"])
        & branches.supplier.str.contains(
            "motor|storage capacity|battery|charger|inverter|fuel cell|powertrain|boiler|water heater|equipment production",
            case=False,
        )
        & branches.branch_kg.abs().gt(1e-9)
    ].copy()
    examples = examples.loc[
        examples.groupby(["variable", "indicator"]).branch_kg.apply(
            lambda s: s.abs().idxmax()
        )
    ].copy()
    examples["material"] = examples.indicator.map(labels)
    examples["annual PJ"] = examples.annual_MJ / 1e9
    examples["branch tonnes"] = examples.branch_kg / 1000
    examples = examples[
        [
            "variable",
            "supplier",
            "material",
            "annual PJ",
            "unit",
            "input_per_MJ",
            "kg_per_unit",
            "branch tonnes",
        ]
    ]
    examples.to_csv(out / "worked_equipment_examples.csv", index=False)
    recipes = []
    for path in sorted((base / "build/packages").glob("*.motor_magnet_checks.csv")):
        frame = pd.read_csv(path)
        frame.insert(0, "scenario", path.name.split("-stem-")[1].split(".")[0])
        recipes.append(frame)
    assert len(recipes) == 4, "Missing package-level motor/magnet checks"
    recipes = pd.concat(recipes, ignore_index=True)
    assert recipes.passed.all(), "Motor/magnet recipe validation failed"
    recipes.to_csv(out / "motor_magnet_source_checks.csv", index=False)
    nd_method = next(
        m for m in methods if m["label"] == "Neodymium" and "definition" in m
    )
    nd_fraction = next(
        p["factor"]
        for p in nd_method["definition"]["products"]
        if p["product"] == "neodymium oxide"
    )
    reference_motor_nd = 0.3148 * nd_fraction * (1.98 / 53)
    vehicle_proof = json.loads(
        next(
            (base / "build/packages").glob("*-SPS1_bas0.vehicle_energy.json")
        ).read_text()
    )
    car = next(
        r
        for r in vehicle_proof
        if r["year"] == 2020
        and r["wrapper"] == "electricity, used in passenger car vehicle"
        and r["location"] == "CH"
    )
    motor = next(
        e
        for e in car["retained equipment"]
        if e["supplier"][1] == "electric motor, electric passenger car"
    )
    assert car["source unit"] == "kilometer"
    car_energy = car["purchased MJ per source service"]
    lifetime_km = 1 / motor["vehicle per source service"]

    def table(frame):
        return frame.to_html(
            index=False, escape=True, float_format=lambda n: f"{n:,.5g}", border=0
        )

    links = [
        "sample.csv",
        "chemical_factor_checks.csv",
        "raw_STEM_demand_checks.csv",
        "raw_boiler_stock_energy_checks.csv",
        "old_new_indicator_totals.csv",
        "solver_flag_rechecks.csv",
        "equipment_review_summary.json",
        "numerical_summary.json",
        "motor_magnet_source_checks.csv",
        "worked_equipment_examples.csv",
        "aggregate_equipment_coverage.csv",
        "old_new_system_attribution.csv",
    ]
    body = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SPS energy equipment and metal audit</title><style>
body{{font:16px/1.55 system-ui,sans-serif;max-width:1240px;margin:35px auto;padding:0 22px;color:#172b36;background:#f7fafb}}h1,h2{{line-height:1.2}}h1{{font-size:34px}}section{{background:white;padding:22px;margin:22px 0;border:1px solid #dce6e9;border-radius:10px}}.qualified{{border-left:6px solid #b87816}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #dde5e8;vertical-align:top}}.scroll{{overflow:auto}}a{{color:#126a8b}}summary{{cursor:pointer;font-weight:650}}code{{white-space:normal}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style>
<h1>SPS energy equipment and metal quantities</h1><p>Four SPS base scenarios · 2020–2050 · lifetime-attributed energy consumption</p>
<section class="qualified"><h2>What the results establish</h2><p>The calculation is checked against original matrices and explicit equipment coefficients. The results describe selected refined-product deliveries under the documented inventory boundary. They do <strong>not</strong> establish complete physical system metal demand: several equipment technologies, component compositions and aggregate uses still rely on proxies or have coverage gaps.</p><p>Equipment necessary for energy conversion is included. Vehicle chassis, bodies, roads and general buildings are excluded. Upstream manufacturing remains included. These are scenario totals, not marginal policy additions, annual equipment sales, or primary/secondary supply shares. Element and alloy indicators may overlap and must not be added together.</p></section>
<section><h2>Numerical verification</h2><p><strong>{escape(verification_status)}</strong> The review includes {len(sample):,} sampled and targeted consumer cases. Independent checks cover {numerical['adjoint_checks']:,} consumer–material results, {numerical['system_checks']:,} system totals and {numerical['location_checks']:,} regional results. Strict precision flags are retained and rechecked with another solver.</p>{conditioning_note}<details><summary>Original numerical checks, including initial flags</summary><pre>{escape(json.dumps(numerical, indent=2))}</pre></details><details><summary>Solver rechecks</summary><pre>{escape(json.dumps(solver, indent=2))}</pre></details><details><summary>Coefficient and old/new reconciliation checks</summary><pre>{escape(json.dumps(equipment, indent=2))}</pre></details></section>
<section><h2>Corrected energy inventories</h2><p>Updated car, truck, bus and motorcycle service inventories supply the energy denominator and equipment lifetime allocation. Each transport wrapper consumes one MJ of its named purchased energy carrier; source auxiliary fuels are retained separately and disclosed. Charging losses are counted once; occupancy and payload cancel through the source service-to-vehicle allocation. Final non-fuel emissions follow the post-GAINS source, while fuel-derived emissions retain scenario accounting. CNG retains its physical compression process and uses Swiss scenario supplies.</p><p>Dedicated electric-boiler hardware is allocated from each scenario's installed capacity, annual electricity and lifetime. Industrial boiler material shares are provisional, and steel grades and cable/electronics splits remain uncertain. Static Swiss aggregate electricity shares include identified energy components and preserve uncovered residual demand. Tram and hydrogen-motorcycle coefficients use explicit vehicle-component proxies.</p></section>
<section><h2>Motor and magnet coefficients</h2><p>{len(recipes):,} checks compare the exported motor and magnet coefficients with the original source inventory. The shared magnet retains 0.3148 kg neodymium oxide and 0.029 kg boric oxide per kg magnet. Motor magnet masses are retained, and whole-vehicle lithium observations are not added to motors. The earlier trial build failed these checks and was rejected. The rebuilt scenarios use premise's reviewed intensity units and component-scope preservation rules; unsupported years preserve source inputs. These checks cover the named recipes, not every material coefficient in every technology. Generic motor aluminium and cobalt observations are available only for 2020; later years retain the original inventory. Trends can therefore reflect a change in coefficient basis as well as changes in the energy system.</p><p>A direct hand calculation for the source 53 kg passenger-car motor is 0.3148 × {nd_fraction:g} × (1.98 / 53) = <strong>{reference_motor_nd:.8f} kg Nd per kg motor</strong>: oxide per kg magnet × contained-Nd fraction × magnet per kg motor. Multiply by the motor kg/MJ and annual MJ to obtain this branch's contained-Nd delivery. Other upstream metal uses remain separate contributions in the full calculation.</p></section>
<section><h2>Worked equipment back-calculations</h2><p>The source representative electric car in SPS1/2020 uses {car_energy:.6g} MJ/km ({car_energy * 100 / 3.6:.3f} kWh/100 km), with a {lifetime_km:,.0f} km lifetime and {motor['amount per vehicle']:.4f} kg motor. Its allocated motor input is {motor['amount per vehicle']:.4f} ÷ ({lifetime_km:,.0f} × {car_energy:.6g}) = <strong>{motor['amount per MJ']:.8g} kg motor/MJ</strong>. Equipment is allocated once through the source energy and lifetime assumptions.</p><p>The table shows SPS1 in 2050: the largest equipment branch for each shown consumer/material pair. Branch tonnes = annual PJ × 10⁹ × equipment input per MJ × refined-metal kg per equipment unit ÷ 1000. The last coefficient includes the equipment's full upstream supply chain; it is not its physical metal composition. These are individual branch contributions, not consumer totals, and different materials must not be added together. The complete branch table retains every equipment input and both endpoint years for all scenarios.</p><div class="scroll">{table(examples)}</div></section>
<section><h2>2050 old/new totals</h2><p>All numbers below are tonnes of the indicated element or alloy. The downloadable comparison includes every indicator and year. The endpoint attribution table separates demand, foreground coefficients, upstream intensities and added/removed suppliers; it is an accounting decomposition, not a causal policy estimate.</p><div class="scroll">{table(comparison)}</div></section>
<section><h2>What changed in the metal totals</h2><p>SPS1 in 2050, tonnes of each element or alloy. Demand is evaluated first, then foreground coefficients at the previous supplier intensities, then upstream intensity changes. Added and removed suppliers include their respective supply chains. Direct boundary changes account for deliveries counted at the final-energy activity itself. These contributions reconcile to the total change; this ordered decomposition does not isolate the causal effect of each model revision. The downloadable system table covers all scenarios at both endpoint years.</p><div class="scroll">{table(attribution_example)}</div></section>
<section class="qualified"><h2>Assumption sensitivities</h2><p>Largest relative changes across all materials in 2050, excluding totals at or below 1 kg from percentage comparisons. Each case changes one assumption and leaves central results unchanged. Steel-grade and electronics alternatives test undisclosed material classifications; they are not statistical confidence bounds. Aggregate tests scale only modelled components and cannot bound missing equipment. Li–S removal renormalizes remaining battery capacity shares. Different cases must not be summed.</p><div class="scroll">{table(all_material_effects)}</div><p>The aggregate lighting inventory uses a CFL archetype without a projected LED transition. Mercury results are consequently sensitive to that proxy. Circuit-board and wiring assumptions affect tin and gold; the rail auxiliary-fuel sensitivity tests the inherited shunting input whose applicability to passenger rail and tram remains unresolved. These material-specific limitations matter even where arithmetic agrees.</p><details><summary>Sensitivities for the selected metals and alloys shown above</summary><div class="scroll">{table(effects)}</div></details></section>
<section><h2>Heat-provider equipment paths</h2><p>Positive heat links are followed to identifiable equipment, preserving each native-unit coefficient and market share. Fuel and electricity supply chains are excluded from this coverage screen but remain in the full LCI. A path ending without identified hardware needs review; it does not establish zero equipment. Existing gas-engine proxies do not establish fuel-cell stack or reformer material quantities.</p><div class="scroll">{table(unresolved_heat)}</div><p><a href="heat_equipment_paths.parquet">All heat-provider equipment paths</a></p></section>
<section class="qualified"><h2>Aggregate electricity equipment gaps</h2><p>These percentages refer only to each sector's aggregate “other electricity” use, not all sectoral energy. The Swiss 2020 end-use shares are held constant to 2050. Electricity supply remains included for the entire demand, including the shares with no added end-use equipment.</p><div class="scroll">{table(aggregate_coverage)}</div><p>Identified components are partial inventories. In particular, the residential additions cover documented electrical parts, without inferring motor, heater or structural metal masses from undifferentiated appliance inventories. Missing equipment quantities remain unresolved.</p></section>
<section><h2>Equipment coverage register</h2><p>A gap is not zero equipment demand. “Some identified components” does not imply complete functional coverage. Source technology and service intensity remain separate questions from correct matrix arithmetic.</p><div class="scroll">{table(coverage[["pathway", "status", "basis"]])}</div></section>
<section><h2>Reproducible evidence</h2><ul>{"".join(f'<li><a href="{name}">{name}</a></li>' for name in links if (out / name).exists())}<li><a href="equipment_branches.parquet">Equipment branch back-calculations</a></li><li><a href="old_new_attribution.parquet">Endpoint old/new attribution</a></li><li><a href="coefficient_sensitivities.parquet">All coefficient sensitivities</a></li></ul><p>Source specifications and URLs are embedded in the frozen workbook and package sidecars; generated inventories and results remain local. The dashboard uses the validated combined results and carries this boundary and the coverage register in its metadata.</p></section></html>"""
    (out / "audit_report.html").write_text(body)
    print(out / "audit_report.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / "results/sps_updated")
    render(parser.parse_args().base)
