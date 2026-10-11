from pathlib import Path

import nbformat


NOTEBOOK = Path("SWEET-SURE - GeoPolRisk indicators generation.ipynb")


def load_namespace():
    nb = nbformat.read(NOTEBOOK, as_version=4)
    ns = {}
    for idx in [1, 3, 5]:
        exec(compile(nb.cells[idx].source, f"cell_{idx}", "exec"), ns)
    return ns


def main():
    ns = load_namespace()
    all_results_file, workbook, df_all_results = ns["run_all_packages_results_only"](
        ns["ALL_PACKAGES"],
        multiprocessing=ns["MULTIPROCESSING"],
        recompute_existing=False,
        comparison_years=ns["COMPARISON_YEARS"],
        top_n=ns["TOP_N"],
        aggregate_by=ns["ALL_SCENARIOS_AGGREGATE_BY"],
    )
    print("all_results_file", all_results_file, flush=True)
    print("workbook", workbook, flush=True)
    print("result_rows", len(df_all_results), flush=True)
    print(
        "scenarios",
        df_all_results["scenario_label"].nunique() if not df_all_results.empty else 0,
        flush=True,
    )


if __name__ == "__main__":
    main()
