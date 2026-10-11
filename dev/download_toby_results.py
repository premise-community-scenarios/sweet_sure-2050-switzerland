#!/usr/bin/env python3
"""Cache public Toby report 11 and raw records for the scenarios in a batch."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

import pandas as pd


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(batch, output, workers=3):
    reference = batch / "toby_reference"
    inputs = json.loads((batch / "inputs.json").read_text())
    registry = pd.read_csv(reference / "scenarios.csv")
    if registry.scn_code.duplicated().any():
        raise ValueError("Duplicate scenario codes")
    ids = registry.set_index("scn_code").scn_id.to_dict()
    selected = sorted(e["scenario"] for e in inputs["packages"])
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "sources.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"report_id": 11, "files": {}}
    )
    if manifest["report_id"] != 11:
        raise ValueError("Wrong cached report")
    manifest["scenario_registry_sha256"] = sha256(reference / "scenarios.csv")

    def fetch(scenario, kind):
        scn_id = int(ids[scenario])
        filename = f"old_{scenario}{'_raw' if kind == 'raw' else ''}.csv"
        path = output / filename
        url = (
            f"https://toby.euler.usi.ch/raw.php?mth=csv&rpt_id=11&scn_id={scn_id}"
            if kind == "raw"
            else f"https://toby.euler.usi.ch/rpt.php?mth=csv&rpt_id=11&scn_id={scn_id}"
        )
        cached = manifest["files"].get(filename)
        if cached and path.exists():
            if cached["url"] != url or sha256(path) != cached["sha256"]:
                raise ValueError(f"Changed cached source: {path}")
            return filename, cached
        for attempt in range(3):
            try:
                with urlopen(
                    Request(url, headers={"User-Agent": "SWEET-SURE comparison"}),
                    timeout=60,
                ) as response:
                    payload = response.read()
                    content_type = response.headers.get("Content-Type")
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        frame = pd.read_csv(
            io.BytesIO(payload),
            **(
                {"names": ["prm_id", "scn_id", "year", "value"]}
                if kind == "raw"
                else {}
            ),
        )
        required = (
            {"prm_id", "scn_id", "year", "value"}
            if kind == "raw"
            else {"prm_id", "scn_id", "path", "unit"}
        )
        if (
            not required.issubset(frame.columns)
            or frame.empty
            or set(frame.scn_id) != {scn_id}
        ):
            raise ValueError(f"Unexpected report response: {url}")
        tmp = path.with_suffix(".part")
        tmp.write_bytes(payload)
        tmp.replace(path)
        return filename, {
            "url": url,
            "scenario": scenario,
            "scn_id": scn_id,
            "kind": kind,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "sha256": sha256(path),
            "bytes": len(payload),
            "rows": len(frame),
            "content_type": content_type,
        }

    jobs = [(scenario, kind) for scenario in selected for kind in ("report", "raw")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, *job) for job in jobs]
        for completed, future in enumerate(as_completed(futures), 1):
            filename, info = future.result()
            manifest["files"][filename] = info
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            tmp = manifest_path.with_suffix(".part")
            tmp.write_text(json.dumps(manifest, indent=2) + "\n")
            tmp.replace(manifest_path)
            print(
                f"{completed}/{len(jobs)} {filename}: {info['rows']} rows", flush=True
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 5))
    args = parser.parse_args()
    download(
        args.batch,
        args.output_dir or args.batch / "toby_comparison" / "old_downloads",
        args.workers,
    )


if __name__ == "__main__":
    main()
