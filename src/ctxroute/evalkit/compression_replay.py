"""Offline compression-point evidence and paired requests; no model-dispatch entry point."""

import argparse
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ctxroute.context.manager import ContextManager
from ctxroute.context.memory import Memory
from ctxroute.domain import Action, RunState
from ctxroute.tracing.cost import Usage


def normalized_action(action: Action) -> str:
    """Bash equality is exactly the repeat gate's equality; never normalize shell text."""
    payload = action.payload()
    payload.pop("remember", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def capture_point(context: ContextManager, state: RunState) -> dict[str, Any]:
    full = context.full_history_messages(state.history)
    compressed = context.messages()
    return {
        "schema": 1,
        "kind": "compression_point",
        "run_id": state.run_id,
        "step": state.step,
        "accepted_memory": asdict(context.memory),
        "history": [step.context_record() for step in state.history],
        "retained_step_numbers": [step.number for step in context.recent],
        "common": {
            "system": context.system,
            "task": context.task,
            "fixed": dict(context.fixed),
            "progress": dict(context.progress),
        },
        "full_history_messages": full,
        "compressed_messages": compressed,
        "full_history_estimated_tokens": context.estimator.count(
            json.dumps(full, ensure_ascii=False)
        ),
        "compressed_estimated_tokens": context.estimator.count(
            json.dumps(compressed, ensure_ascii=False)
        ),
        "previous_executed_actions": [
            {"step": step.number, "normalized": normalized_action(step.action)}
            for step in state.history
            if step.action is not None and step.exit_code is not None
        ],
        "replayable": True,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verified_snapshot(run: Path, reference: dict[str, Any], category: str) -> dict[str, Any]:
    name = reference.get("path", "")
    if not isinstance(name, str) or not re.fullmatch(
        category + r"/[a-zA-Z0-9_-]{1,100}\.json", name
    ):
        raise ValueError("Invalid evidence reference")
    path = run / name
    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or not path.resolve().is_relative_to(run.resolve())
    ):
        raise ValueError("Evidence path escapes the run")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != reference.get("sha256"):
        raise ValueError("Evidence hash mismatch")
    value: dict[str, Any] = json.loads(data)
    if value.get("replayable") is not True:
        raise ValueError("Evidence was redacted or is not replayable")
    return value


def build_forks(
    run: Path, controls: dict[str, Any], *, allow_synthetic: bool = False
) -> list[dict[str, Any]]:
    """Select the first three accepted points; missing evidence stops the entire build."""
    manifest = json.loads((run / "manifest.json").read_text())
    if manifest.get("kind") != "agent_run" and not (
        allow_synthetic and manifest.get("kind") == "synthetic_test"
    ):
        raise ValueError("Require a collector or an explicitly synthetic fixture")
    for key, expected in controls["manifest"].items():
        if manifest.get(key) != expected:
            raise ValueError(f"Collector control mismatch: {key}")
    for filename in ("config.json", "task.json"):
        expected = controls.get("input_hashes", {}).get(filename)
        if (
            expected is not None
            and hashlib.sha256((run / filename).read_bytes()).hexdigest() != expected
        ):
            raise ValueError(f"Collector input mismatch: {filename}")
    lifecycle = read_jsonl(run / "lifecycle.jsonl")
    accepted = [e for e in lifecycle if e.get("event") == "compression" and e.get("accepted")]
    if len(accepted) < 3:
        raise ValueError("Fewer than three accepted points; no replacement selection")
    selected = accepted[:3]
    if [e["step"] for e in selected] != sorted({e["step"] for e in selected}):
        raise ValueError("Compression points must be unique and chronological")
    requests: dict[str, dict[str, Any]] = {}
    for event in read_jsonl(run / "requests.jsonl"):
        if not re.fullmatch(r"[0-9a-f]{32}", str(event.get("request_id", ""))):
            raise ValueError("Invalid source request identifier")
        requests.setdefault(event["request_id"], {}).update(event)
    for request in requests.values():
        if request.get("error_type") or "prompt_tokens" not in request:
            raise ValueError("Collector has failed or unknown-usage requests")
        Usage(request["prompt_tokens"], request["cached_tokens"], request["completion_tokens"])
    output: list[dict[str, Any]] = []
    for compression in selected:
        step = compression["step"]
        point_events = [
            e for e in lifecycle if e.get("event") == "compression_point" and e["step"] == step
        ]
        if len(point_events) != 1:
            raise ValueError("Missing or ambiguous compression-point snapshot")
        point = verified_snapshot(run, point_events[0]["snapshot"], "compression_points")
        if (
            point.get("schema") != 1
            or point.get("step") != step
            or point.get("run_id") != manifest["run_id"]
            or point.get("accepted_memory") != compression.get("accepted_memory")
        ):
            raise ValueError("Point identity or accepted memory mismatch")
        selected_requests: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for purpose in ("compression", "action"):
            matches = [
                r for r in requests.values() if r.get("purpose") == purpose and r["step"] == step
            ]
            if len(matches) != 1:
                raise ValueError("Missing or ambiguous source request")
            request = matches[0]
            saved = verified_snapshot(run, request["request_snapshot"], "request_audits")
            if any(
                saved.get(k) != request.get(k)
                for k in ("run_id", "request_id", "step", "purpose", "tier")
            ):
                raise ValueError("Source request identity mismatch")
            if saved.get("kind") != "logical_transport_request":
                raise ValueError("Unknown source payload schema")
            selected_requests[purpose] = (request, saved)
        summary_request = selected_requests["compression"][0]
        summary_payload = selected_requests["compression"][1]["payload"]
        if (
            summary_request["tier"] != "cheap"
            or summary_payload.get("model") != controls["summary_model"]
        ):
            raise ValueError("Source summary model mismatch")
        for key, expected in controls["payload"].items():
            if key not in {"model", "messages"} and summary_payload.get(key) != expected:
                raise ValueError(f"Source summary parameter mismatch: {key}")
        audits = [
            e
            for e in lifecycle
            if e.get("event") == "response_audit"
            and e.get("request_id") == summary_request["request_id"]
        ]
        if len(audits) != 1 or not audits[0].get("saved"):
            raise ValueError("Missing summary response audit")
        audit_path = run / "response_audits" / (summary_request["request_id"] + ".json")
        if (
            audit_path.is_symlink()
            or audit_path.parent.is_symlink()
            or not audit_path.resolve().is_relative_to(run.resolve())
        ):
            raise ValueError("Summary audit path escapes the run")
        raw = audit_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != audits[0].get("sha256"):
            raise ValueError("Summary response audit hash mismatch")
        content = json.loads(raw)["content"]
        if (
            content.get("capture_complete") is not True
            or content.get("redacted") is not False
            or hashlib.sha256(content["text"].encode()).hexdigest() != content.get("sha256")
        ):
            raise ValueError("Summary response is missing, truncated or redacted")
        if asdict(Memory.parse(content["text"])) != point["accepted_memory"]:
            raise ValueError("Accepted memory differs from the captured response")
        action_request, saved = selected_requests["action"]
        if action_request["tier"] != "strong":
            raise ValueError("Source action was routed to a different tier")
        payload = saved["payload"]
        if set(payload) != {
            "model",
            "messages",
            "max_tokens",
            "temperature",
            "thinking",
            "stream",
            "response_format",
        }:
            raise ValueError("Unexpected source payload parameters")
        for key, expected in controls["payload"].items():
            if payload.get(key) != expected:
                raise ValueError(f"Source request parameter mismatch: {key}")
        if payload["messages"] != point["compressed_messages"]:
            raise ValueError("Point messages differ from the dispatched source request")
        full, compressed = point["full_history_messages"], point["compressed_messages"]
        if (
            len(full) != 2
            or len(compressed) != 2
            or full[0] != compressed[0]
            or full[0] != {"role": "system", "content": point["common"]["system"]}
            or full[1]["role"] != "user"
            or compressed[1]["role"] != "user"
        ):
            raise ValueError("Fork envelope mismatch")
        if point["full_history_estimated_tokens"] > controls["input_guard_tokens"]:
            raise ValueError("Full history exceeds the frozen input guard; never truncate it")
        for condition, messages in (("full_history", full), ("compressed_history", compressed)):
            fork_payload = deepcopy(payload)
            fork_payload["messages"] = deepcopy(messages)
            output.append(
                {
                    "schema": 1,
                    "kind": "synthetic_test" if allow_synthetic else "replay_candidate",
                    "execution_authorized": False,
                    "point": step,
                    "condition": condition,
                    "source_run_id": manifest["run_id"],
                    "source_action_request_id": action_request["request_id"],
                    "source_summary_request_id": summary_request["request_id"],
                    "payload": fork_payload,
                    "timeout_seconds": saved["timeout_seconds"],
                    "previous_executed_actions": point["previous_executed_actions"],
                    "accepted_memory": point["accepted_memory"],
                    "point_snapshot": point_events[0]["snapshot"],
                }
            )
        left, right = output[-2:]
        if {k: v for k, v in left["payload"].items() if k != "messages"} != {
            k: v for k, v in right["payload"].items() if k != "messages"
        } or left["timeout_seconds"] != right["timeout_seconds"]:
            raise ValueError("Non-context fork parameters differ")
    return output


def classify_sample(text: str, previous: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        action = Action.parse(text, recall_enabled=True)
    except ValueError:
        return {"valid": False, "repeated": None, "matching_steps": []}
    normalized = normalized_action(action)
    matches = [item["step"] for item in previous if item["normalized"] == normalized]
    return {
        "valid": True,
        "normalized": normalized,
        "repeated": bool(matches),
        "matching_steps": matches,
    }


def judge_h1(samples: list[dict[str, Any]], omissions: dict[int, bool | None]) -> str:
    if any(type(item.get("point")) is not int or item["point"] < 1 for item in samples):
        return "无法判断"
    points: list[int] = sorted({int(item["point"]) for item in samples})
    if len(samples) != 6 or len(points) != 3 or any(not item.get("valid") for item in samples):
        return "无法判断"
    differing: list[int] = []
    for point in points:
        pair = [item for item in samples if item["point"] == point]
        if len(pair) != 2 or {item["condition"] for item in pair} != {
            "full_history",
            "compressed_history",
        }:
            return "无法判断"
        by_condition = {item["condition"]: item for item in pair}
        if any(type(item.get("repeated")) is not bool for item in pair):
            return "无法判断"
        if (
            by_condition["compressed_history"]["repeated"]
            and not by_condition["full_history"]["repeated"]
        ):
            differing.append(point)
    if not differing:
        return "证伪"
    if len(differing) >= 2 and all(omissions.get(point) is True for point in differing):
        return "支持"
    return "无法判断"


def write_forks(run: Path, output: Path, controls: dict[str, Any]) -> Path:
    records = build_forks(run, controls)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for index, record in enumerate(records):
        path = output / f"{index:02d}-{record['point']}-{record['condition']}.json"
        with path.open("x") as stream:
            path.chmod(0o600)
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--controls", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(write_forks(args.run, args.output, json.loads(args.controls.read_text())))


if __name__ == "__main__":
    main()
