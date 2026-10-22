"""Read only the instance_id Parquet column; never expose answer columns.

Download a pinned official test-split Parquet into an ignored project cache.
The upstream split named 'test' supplies our 60 dev / 240 held-out instances.
No upstream dev examples or model/harness outcomes participate in selection.
"""

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

from ctxroute.evalkit.split import partition


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--seed", default="20261022")
    args = parser.parse_args()
    target = Path("data/split.json")
    if target.exists():
        raise SystemExit("Refusing to overwrite frozen data/split.json")
    url = (
        "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/resolve/"
        f"{args.revision}/{args.file}"
    )
    cache = Path(".cache/dataset/source.parquet")
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as source, cache.open("wb") as output:
            shutil.copyfileobj(source, output)
        source_hash = hashlib.sha256(cache.read_bytes()).hexdigest()
        # This is a data preparation script, never a task-agent input loader.
        ids = pq.read_table(cache, columns=["instance_id"]).column("instance_id").to_pylist()
        dev, test = partition(ids, args.seed)
        ids_hash = hashlib.sha256(("\n".join(sorted(ids)) + "\n").encode()).hexdigest()
        payload = {
            "dataset": "princeton-nlp/SWE-bench_Lite",
            "upstream_split": "test",
            "revision": args.revision,
            "source_file": args.file,
            "source_sha256": source_hash,
            "sorted_ids_sha256": ids_hash,
            "seed": args.seed,
            "algorithm": "sort by SHA256(seed + ':' + instance_id), then instance_id",
            "dev": dev,
            "test": test,
            "pilot_candidate_order": dev[:10],
            "smoke_instance": dev[0],
            "formal_test_enabled": False,
        }
        target.parent.mkdir(exist_ok=True)
        with target.open("x") as output:
            output.write(json.dumps(payload, indent=2) + "\n")
        print(f"Frozen: dev={len(dev)}, test={len(test)}, candidates=10")
        print(f"Split SHA256: {hashlib.sha256(target.read_bytes()).hexdigest()}")
    finally:
        cache.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
