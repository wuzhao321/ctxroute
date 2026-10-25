"""Grade completed fixed pilot predictions with official harness in separate containers.

Grading fields stay inside this post-inference process and private files. No model
adapter or credential loader is invoked. The official run_instance/grader is unmodified.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import docker
import pyarrow.parquet as pq
from docker.models.containers import ContainerCollection
from swebench.harness.reporting import make_run_report
from swebench.harness.run_evaluation import run_instance
from swebench.harness.test_spec.test_spec import make_test_spec

from ctxroute.evalkit.workflow import completed_runs, digest, load


class IsolatedContainers(ContainerCollection):
    def create(self, *args, **kwargs):
        if any(kwargs.get(key) for key in ("volumes", "mounts", "environment", "cap_add")):
            raise ValueError("Unexpected host access or environment in grading container")
        kwargs.update(
            network_mode="none",
            mem_limit="4g",
            nano_cpus=2_000_000_000,
            pids_limit=128,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            privileged=False,
            labels={"ctxroute.grading": "true"},
        )
        return super().create(*args, **kwargs)


class GradingClient(docker.DockerClient):
    @property
    def containers(self):
        return IsolatedContainers(client=self)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--negative-control", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    workflow_dir = (root / args.workflow).resolve()
    workflow = load(root, workflow_dir)
    # Grading data cannot be loaded while any scheduled inference is incomplete.
    runs = completed_runs(root, workflow)
    handoff = json.loads((workflow_dir / "handoff.json").read_text())
    split = json.loads((root / "data/split.json").read_text())
    iid = split["dev"][0]
    if {json.loads((r / "manifest.json").read_text())["instance_id"] for r in runs} != {iid}:
        raise ValueError("This launcher only supports the frozen first dev instance")
    image = json.loads((root / "results/validation/live-preflight/harness-image.json").read_text())
    # Each workflow owns its mapping and logs; never replace a previous pilot's files.
    private = root / "results/private/harness" / workflow["plan_id"]
    private.mkdir(parents=True, exist_ok=True)
    url = (
        f"https://huggingface.co/datasets/{split['dataset']}/resolve/"
        f"{split['revision']}/{split['source_file']}"
    )
    with tempfile.TemporaryDirectory(dir=private) as temporary:
        source = Path(temporary) / "grading.parquet"
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
                url,
                "--output",
                str(source),
            ],
            check=True,
            timeout=190,
        )
        if hashlib.sha256(source.read_bytes()).hexdigest() != split["source_sha256"]:
            raise ValueError("Frozen grading source checksum mismatch")
        rows = pq.read_table(
            source,
            columns=[
                "instance_id",
                "repo",
                "base_commit",
                "version",
                "environment_setup_commit",
                "test_patch",
                "FAIL_TO_PASS",
                "PASS_TO_PASS",
            ],
            filters=[("instance_id", "=", iid)],
        ).to_pylist()
    if len(rows) != 1:
        raise ValueError("Expected one frozen grading instance")
    spec = make_test_spec(rows[0])
    if spec.arch != "arm64":
        raise ValueError("Grading architecture differs")
    client = GradingClient(base_url=f"unix://{root}/.tools/vm/ctx/sock/docker.sock", timeout=180)
    reports = {}
    receipts = []
    os.chdir(private)
    try:
        if client.images.get(spec.instance_image_key).id != image["image_id"]:
            raise ValueError("Official grading image changed")
        for cohort, entry in handoff["cohorts"].items():
            predictions = root / entry["predictions"]
            if digest(predictions) != entry["sha256"]:
                raise ValueError("Handed-off prediction changed")
            pred_rows = [json.loads(line) for line in predictions.read_text().splitlines()]
            if len(pred_rows) != 1 or pred_rows[0]["instance_id"] != iid:
                raise ValueError("Unexpected grading prediction membership")
            pred = pred_rows[0]
            grading_id = entry["grading_run_id"]
            inference_id = workflow["cohorts"][cohort][0]["run_id"]
            # Match the official CLI: empty predictions are reported as empty,
            # not sent to git apply and not mislabeled as infrastructure failures.
            result = None
            if pred["model_patch"]:
                result = run_instance(spec, pred, False, False, client, grading_id, timeout=600)
            aggregate = make_run_report({iid: pred}, rows, grading_id, client).resolve()
            report_path = (
                private
                / "logs/run_evaluation"
                / grading_id
                / pred["model_name_or_path"].replace("/", "__")
                / iid
                / "report.json"
            )
            receipt = {
                "inference_run_id": inference_id,
                "grading_run_id": grading_id,
                "prediction_sha256": entry["sha256"],
                "official_image_id": image["image_id"],
                "source_sha256": split["source_sha256"],
                "harness_version": "3.0.17",
                "network_mode": "none",
                "memory_bytes": 4 * 1024**3,
                "cpus": 2,
                "report_available": result is not None and report_path.is_file(),
                "official_summary": str(aggregate.relative_to(root)),
                "official_summary_sha256": digest(aggregate),
            }
            if receipt["report_available"]:
                reports[inference_id] = str(report_path.relative_to(root))
                receipt["resolved"] = result[1][iid]["resolved"]
                receipt["report_sha256"] = digest(report_path)
            else:
                receipt["missing_reason"] = (
                    "empty_prediction"
                    if not pred["model_patch"].strip()
                    else "official_harness_error_see_private_log"
                )
            receipts.append(receipt)
            print(json.dumps(receipt), flush=True)
            (workflow_dir / "grading-receipts.json").write_text(
                json.dumps(receipts, indent=2) + "\n"
            )
            (private / "reports.json").write_text(json.dumps(reports, indent=2) + "\n")
        if args.negative_control:
            # A marker-file addition does not fix application behavior. This is an
            # infrastructure negative control, never a model result or pilot arm.
            control_id = f"ctxroute-infra-negative-{workflow['plan_id']}"
            prediction = {
                "instance_id": iid,
                "model_name_or_path": "infrastructure-negative-control-not-model",
                "model_patch": (
                    "diff --git a/.ctxroute-harness-control b/.ctxroute-harness-control\n"
                    "new file mode 100644\n--- /dev/null\n+++ b/.ctxroute-harness-control\n"
                    "@@ -0,0 +1 @@\n+Infrastructure negative control; no behavior change.\n"
                ),
            }
            (private / "negative-control-prediction.json").write_text(
                json.dumps(prediction, indent=2) + "\n"
            )
            result = run_instance(spec, prediction, False, False, client, control_id, timeout=600)
            summary = make_run_report({iid: prediction}, rows, control_id, client).resolve()
            control = {
                "kind": "infrastructure_negative_control_not_model_or_pilot_result",
                "grading_run_id": control_id,
                "report_available": result is not None,
                "resolved": result[1][iid]["resolved"] if result is not None else None,
                "official_summary": str(summary.relative_to(root)),
                "official_summary_sha256": digest(summary),
            }
            (workflow_dir / "negative-control.json").write_text(
                json.dumps(control, indent=2) + "\n"
            )
            print(json.dumps(control), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
