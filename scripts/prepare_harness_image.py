"""Build the frozen first dev image with the official ARM64 harness, without answers."""

import hashlib
import io
import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path

import docker
import pyarrow.parquet as pq
from swebench.harness.docker_build import build_instance_images
from swebench.harness.test_spec.test_spec import make_test_spec


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if platform.machine() != "arm64":
        raise RuntimeError("This preparation is registered for the local ARM64 pilot")
    split = json.loads((root / "data/split.json").read_text())
    iid = split["dev"][0]
    if iid != "sympy__sympy-22840":
        raise ValueError("Frozen first instance changed")
    private = root / "results/private/harness"
    private.mkdir(parents=True, exist_ok=True)
    source_url = (
        f"https://huggingface.co/datasets/{split['dataset']}/resolve/"
        f"{split['revision']}/{split['source_file']}"
    )
    columns = ["instance_id", "repo", "base_commit", "version", "environment_setup_commit"]
    with tempfile.TemporaryDirectory(dir=private) as temporary:
        source = Path(temporary) / "source.parquet"
        subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "15",
                "--max-time",
                "180",
                "--max-filesize",
                "104857600",
                "--retry",
                "0",
                source_url,
                "--output",
                str(source),
            ],
            check=True,
            timeout=190,
        )
        if hashlib.sha256(source.read_bytes()).hexdigest() != split["source_sha256"]:
            raise ValueError("Frozen source checksum mismatch")
        rows = pq.read_table(
            source, columns=columns, filters=[("instance_id", "=", iid)]
        ).to_pylist()
    if len(rows) != 1:
        raise ValueError("Expected exactly the frozen first dev instance")
    # No gold patch, hidden tests, test names, hints or evaluation scripts are decoded.
    row = {**rows[0], "test_patch": "", "FAIL_TO_PASS": [], "PASS_TO_PASS": []}
    spec = make_test_spec(row)
    if spec.arch != "arm64":
        raise ValueError("Official harness requires a different architecture for this task")
    client = docker.DockerClient(
        base_url=f"unix://{root}/.tools/vm/ctx/sock/docker.sock", timeout=180
    )
    # Build contexts are private; no project directory is sent as Docker build context.
    os.chdir(private)
    try:
        build_instance_images(client, [spec], max_workers=1, tag="latest")
        image = client.images.get(spec.instance_image_key)
        if image.attrs["Architecture"] != "arm64":
            raise ValueError("Built image is not ARM64")
        # Official setup removes the remote, but tags and reflogs can still retain
        # future commits. Remove these from the inference image before the agent runs.
        dockerfile = f"""FROM {image.id}
WORKDIR /testbed
RUN current=$(git symbolic-ref HEAD) && \\
    git for-each-ref --format='%(refname)' | while IFS= read -r ref; do \\
      if [ "$ref" != "$current" ]; then git update-ref -d "$ref"; fi; done
RUN git reflog expire --expire=now --all && git gc --prune=now && \\
    test "$(git rev-list --all --not HEAD | wc -l)" -eq 0
"""
        task_image, _ = client.images.build(
            fileobj=io.BytesIO(dockerfile.encode()),
            tag=f"ctxroute.task.arm64.{iid}:audited-history",
            network_mode="none",
            rm=True,
            forcerm=True,
        )
        receipt = {
            "instance_id": iid,
            "image_tag": spec.instance_image_key,
            "image_id": image.id,
            "task_image_id": task_image.id,
            "history_sanitization_dockerfile": dockerfile,
            "architecture": image.attrs["Architecture"],
            "source_sha256": split["source_sha256"],
            "decoded_columns": columns,
            "harness_version": "3.0.17",
            "audit_passed": False,
        }
        path = root / "results/validation/live-preflight/harness-image.json"
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        registry = root / "configs/private/images.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({iid: task_image.id}, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
