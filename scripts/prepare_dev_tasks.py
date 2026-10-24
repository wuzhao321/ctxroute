"""Extract only four permitted columns for the frozen dev prefix; never read answers."""

import argparse
import hashlib
import json
import tempfile
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

from ctxroute.evalkit.campaign import canonical_hash
from ctxroute.evalkit.split import agent_input


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("data/private/dev-smoke.jsonl"))
    args = parser.parse_args()
    if not 1 <= args.count <= 5:
        parser.error("Frozen pilot supports only the first 1-5 dev instances")
    if args.output.exists():
        parser.error("Refusing to overwrite a task selection")
    split_path = Path("data/split.json")
    split = json.loads(split_path.read_text())
    wanted = split["dev"][: args.count]
    url = (
        f"https://huggingface.co/datasets/{split['dataset']}/resolve/"
        f"{split['revision']}/{split['source_file']}"
    )
    Path(".cache/dataset").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dev-extract-", dir=".cache/dataset") as temporary:
        source = Path(temporary) / "source.parquet"
        hasher = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(url, timeout=30) as response, source.open("wb") as output:
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > 100 * 1024 * 1024:
                    raise ValueError("Unexpected dataset size")
                hasher.update(block)
                output.write(block)
        if hasher.hexdigest() != split["source_sha256"]:
            raise ValueError("Pinned dataset checksum differs; no task was exported")
        # Projection happens in Parquet, before records enter Python. No answer columns decoded.
        columns = ["instance_id", "repo", "base_commit", "problem_statement"]
        table = pq.read_table(source, columns=columns, filters=[("instance_id", "in", wanted)])
        tasks = {row["instance_id"]: agent_input(row) for row in table.to_pylist()}
        if set(tasks) != set(wanted):
            raise ValueError("Frozen instances missing from source")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as output:
            for iid in wanted:
                output.write(json.dumps(tasks[iid], ensure_ascii=False) + "\n")
    evidence = {
        "kind": "frozen_dev_input_preparation_not_evaluation",
        "source_revision": split["revision"],
        "source_sha256": split["source_sha256"],
        "split_sha256": hashlib.sha256(split_path.read_bytes()).hexdigest(),
        "instance_ids": wanted,
        "decoded_columns": columns,
        "task_sha256": {iid: canonical_hash(tasks[iid]) for iid in wanted},
        "output_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "source_cache_removed": True,
    }
    receipt = args.output.with_suffix(".manifest.json")
    with receipt.open("x") as output:
        json.dump(evidence, output, indent=2)
        output.write("\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
