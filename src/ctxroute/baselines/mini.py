"""Unmodified native DefaultAgent loop with budgeted model and isolated sandbox adapters."""

import json
import shlex
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from ctxroute.baselines.upstream import NativeAPI
from ctxroute.config import ArmConfig
from ctxroute.context.artifacts import ArtifactStore
from ctxroute.context.manager import ContextLimit, TokenEstimator
from ctxroute.domain import Action, ModelRequest, ModelResponse, Observation, RunState, Step, Task
from ctxroute.models.base import Model, ModelFailure
from ctxroute.orchestrator.engine import RunResult
from ctxroute.sandbox.base import Sandbox
from ctxroute.tracing.budget import BudgetStopped
from ctxroute.tracing.writer import RunWriter


class NativeBoundaryStop(RuntimeError):
    """A tool limit was reached; termination is already recorded in RunState."""


class NativeModel:
    """Native parsing/formatting; provider calls and their retries use our ledger gateway."""

    def __init__(
        self,
        native: NativeAPI,
        model: Model,
        state: RunState,
        cfg: ArmConfig,
        writer: RunWriter,
        deadline: float,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        attempts: int,
        backoff: float,
    ) -> None:
        self.native, self.model, self.state, self.cfg = native, model, state, cfg
        self.writer, self.deadline = writer, deadline
        self.clock, self.sleep, self.attempts, self.backoff = clock, sleep, attempts, backoff
        self.config: dict[str, Any] = native.config["model"]
        self.estimator = TokenEstimator()
        self.pending: dict[str, Any] = {}

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        if kwargs:
            raise ValueError("Unexpected native provider overrides")
        self.state.step += 1
        prepared = tuple({"role": item["role"], "content": item["content"]} for item in messages)
        if any(
            item["role"] not in {"system", "user", "assistant"}
            or not isinstance(item["content"], str)
            for item in prepared
        ):
            raise ValueError("Native text baseline only accepts textual chat messages")
        serialized = json.dumps(prepared, ensure_ascii=False)
        count = self.estimator.count(serialized)
        if count > self.cfg.context.budget_tokens:
            raise ContextLimit("Native uncompressed history exceeds context allowance")
        result = self._complete(prepared)
        self.estimator.calibrate(serialized, result.usage.input_tokens)
        self.state.tier_history.append("strong")
        self.pending = {
            "instance_id": self.state.task.instance_id,
            "arm": "A0",
            "seed": self.state.seed,
            "step": self.state.step,
            "tier": "strong",
            "model_id": result.model_id,
            "prompt_tokens": result.usage.input_tokens,
            "cached_tokens": result.usage.cached_tokens,
            "completion_tokens": result.usage.output_tokens,
            "latency_ms": result.latency_ms,
            "cost_upper_usd": str(result.cost_upper),
            "ctx_tokens_before": count,
            "ctx_tokens_after": count,
            "compress_event": {"triggered": False, "accepted": False},
            "router_decision": {"tier": "strong", "reason": "native_fixed_tier"},
            "model_response": result.text,
        }
        try:
            actions = self.native.actions.parse_regex_actions(
                result.text,
                action_regex=self.native.policy["action_regex"],
                format_error_template=self.config["format_error_template"],
                template_kwargs={"finish_reason": "stop"},
            )
        except self.native.exceptions.FormatError as exc:
            # Native DefaultAgent charges this field even when parsing failed.
            exc.messages[0]["extra"].update(cost=float(result.cost_upper), response=result.text)
            self.writer.append(
                "traces",
                {**self.pending, "tool": None, "exit_code": None, "error_type": "format_error"},
            )
            self.state.history.append(
                Step(
                    self.state.step,
                    "strong",
                    None,
                    exc.messages[0]["content"],
                    error="format_error",
                )
            )
            raise
        return self.format_message(
            role="assistant",
            content=result.text,
            extra={"actions": actions, "cost": float(result.cost_upper), "timestamp": time.time()},
        )

    def _complete(self, messages: tuple[dict[str, str], ...]) -> ModelResponse:
        for attempt in range(self.attempts):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("Native run deadline reached")
            if self.state.accounted_cost >= self.cfg.limits.budget:
                raise BudgetStopped("Native task budget exhausted")
            request = ModelRequest(
                messages,
                "strong",
                "action",
                self.cfg.limits.max_output_tokens,
                remaining,
                self.state.run_id,
                self.state.step,
                self.cfg.limits.budget,
            )
            try:
                response = self.model.complete(request)
                self.state.accounted_cost += response.cost_upper
                if self.state.accounted_cost > self.cfg.limits.budget:
                    raise BudgetStopped("Native task cost exceeded allowance")
                if self.clock() >= self.deadline:
                    raise TimeoutError("Native model exceeded remaining time")
                return response
            except ModelFailure:
                self.writer.append(
                    "lifecycle",
                    {
                        "event": "model_failure",
                        "step": self.state.step,
                        "purpose": "action",
                        "attempt": attempt + 1,
                    },
                )
                if attempt + 1 == self.attempts:
                    raise
                delay = self.backoff * 2**attempt
                if self.clock() + delay >= self.deadline:
                    raise TimeoutError("No time remains for native model retry") from None
                self.sleep(delay)
        raise AssertionError("Unreachable model retry state")

    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    def format_observation_messages(
        self,
        message: dict[str, Any],
        outputs: list[dict[str, Any]],
        template_vars: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return self.native.actions.format_observation_messages(
            outputs,
            observation_template=self.config["observation_template"],
            template_vars=template_vars,
            multimodal_regex="",
        )

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {**self.config, **kwargs}

    def serialize(self) -> dict[str, Any]:
        return {"info": {"config": {"model": self.config, "model_type": "ctxroute.NativeModel"}}}


class NativeEnvironment:
    def __init__(
        self,
        native: NativeAPI,
        sandbox: Sandbox,
        handle: str,
        model: NativeModel,
        artifacts: ArtifactStore,
        system: dict[str, str],
        cwd: str,
    ) -> None:
        self.native, self.sandbox, self.handle, self.model = native, sandbox, handle, model
        self.artifacts, self.system, self.cwd = artifacts, system, cwd
        self.config: dict[str, Any] = {**native.config["environment"], "cwd": cwd}

    def execute(self, action: dict[str, Any], cwd: str = "") -> dict[str, Any]:
        if set(action) != {"command"} or not isinstance(action["command"], str):
            raise ValueError("Native action must contain one bash command")
        if cwd and cwd != self.cwd:
            raise ValueError("Native environment cannot override the audited workdir")
        remaining = self.model.deadline - self.model.clock()
        if remaining <= 0:
            raise TimeoutError("No time remains for native tool execution")
        command = action["command"]
        # Fixed upstream pager variables, never credentials or arbitrary host environment.
        exports = "; ".join(
            f"export {key}={shlex.quote(value)}"
            for key, value in self.config.get("env", {}).items()
        )
        observation = self.sandbox.execute(
            self.handle,
            exports + "; " + command if exports else command,
            min(remaining, self.model.cfg.limits.command_seconds),
        )
        artifact_id = self.artifacts.put(observation.output)
        self.artifacts.pin(artifact_id)
        error = (
            "command_timeout"
            if observation.timed_out
            else "output_limit"
            if observation.output_limited
            else "tool_error"
            if observation.exit_code
            else None
        )
        output = {
            "output": observation.output,
            "returncode": observation.exit_code,
            "exception_info": error or "",
            "extra": {"artifact_id": artifact_id},
        }
        submitted = False
        try:
            # Reuse upstream submission detection without constructing its Docker backend.
            if not observation.timed_out and not observation.output_limited:
                self.native.docker_environment._check_finished(self, output)
        except self.native.exceptions.Submitted:
            submitted = True
            self.model.state.termination = "submitted"
            raise
        finally:
            self._record(command, observation, artifact_id, error, submitted)
        if observation.timed_out or observation.output_limited:
            self.model.state.termination = error
            raise NativeBoundaryStop("Native sandbox stopped at a resource limit")
        return output

    def _record(
        self,
        command: str,
        observation: Observation,
        artifact_id: str,
        error: str | None,
        submitted: bool,
    ) -> None:
        self.model.writer.append(
            "traces",
            {
                **self.model.pending,
                "tool": "submit" if submitted else "bash",
                "command": command,
                "exit_code": observation.exit_code,
                "error_type": error,
                "artifact_id": artifact_id,
                "rpc_roundtrip_ms": observation.rpc_ms,
            },
        )
        self.model.state.history.append(
            Step(
                self.model.state.step,
                "strong",
                Action("submit" if submitted else "bash", command),
                observation.output,
                observation.exit_code,
                error,
                artifact_id,
            )
        )

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {**self.config, **self.system, **kwargs}

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "environment": self.config,
                    "environment_type": "ctxroute.NativeEnvironment",
                }
            }
        }


class MiniSweRunner:
    def __init__(
        self,
        cfg: ArmConfig,
        model: Model,
        sandbox: Sandbox,
        writer: RunWriter,
        native: NativeAPI,
        *,
        cwd: str,
        model_attempts: int = 2,
        backoff_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if cfg.arm != "A0" or cfg.engine != "mini-swe-agent":
            raise ValueError("Native runner requires the A0 configuration")
        if cfg.context.mode != "none" or cfg.router.enabled or cfg.router.default_tier != "strong":
            raise ValueError("Native baseline must not enable ctxroute optimizations")
        if not 1 <= model_attempts <= 2 or backoff_seconds < 0:
            raise ValueError("Invalid native retry policy")
        self.cfg, self.model, self.sandbox, self.writer, self.native = (
            cfg,
            model,
            sandbox,
            writer,
            native,
        )
        self.cwd, self.attempts, self.backoff = cwd, model_attempts, backoff_seconds
        self.clock, self.sleep = clock, sleep

    def run(self, task: Task, seed: int) -> RunResult:
        state = RunState(str(self.writer.manifest["run_id"]), task, "A0", seed)
        started = self.clock()
        deadline = started + self.cfg.limits.wall_seconds
        artifacts = ArtifactStore(
            self.writer.path / "artifacts",
            max_bytes=self.cfg.limits.artifact_bytes,
            recall_chars=self.cfg.limits.recall_chars,
        )
        bridge = NativeModel(
            self.native,
            self.model,
            state,
            self.cfg,
            self.writer,
            deadline,
            clock=self.clock,
            sleep=self.sleep,
            attempts=self.attempts,
            backoff=self.backoff,
        )
        handle, agent, patch = None, None, ""
        try:
            handle = self.sandbox.create(task)
            self.writer.append(
                "lifecycle",
                {"event": "sandbox_created", "create_ms": (self.clock() - started) * 1000},
            )
            remaining = min(self.cfg.limits.command_seconds, deadline - self.clock())
            if remaining <= 0:
                raise TimeoutError("No time for native environment metadata")
            info = self.sandbox.execute(
                handle, "uname -s && uname -r && uname -v && uname -m", remaining
            )
            lines = info.output.splitlines()
            if info.exit_code or info.timed_out or info.output_limited or len(lines) != 4:
                raise RuntimeError("Cannot obtain target system metadata for native prompts")
            system: dict[str, str] = dict(
                zip(("system", "release", "version", "machine"), lines, strict=True)
            )
            self.writer.append(
                "lifecycle",
                {"event": "native_system_probe", "system": system, "rpc_roundtrip_ms": info.rpc_ms},
            )
            environment = NativeEnvironment(
                self.native, self.sandbox, handle, bridge, artifacts, system, self.cwd
            )
            settings = {
                **self.native.config["agent"],
                "step_limit": self.cfg.limits.max_steps,
                "cost_limit": float(self.cfg.limits.budget),
                "wall_time_limit_seconds": int(self.cfg.limits.wall_seconds),
                "max_consecutive_format_errors": self.cfg.limits.format_attempts,
                "output_path": self.writer.path / "trajectory.json",
            }
            agent = self.native.agent(bridge, environment, **settings)
            native_result = agent.run(task.problem_statement)
            status = native_result.get("exit_status")
            state.termination = {
                "Submitted": "submitted",
                "RepeatedFormatError": "format_failure",
                "TimeExceeded": "wall_timeout",
            }.get(status)
            if status == "LimitsExceeded":
                state.termination = (
                    "step_limit" if state.step >= self.cfg.limits.max_steps else "budget_limit"
                )
            state.termination = state.termination or "native_exit"
        except BudgetStopped:
            state.termination = "budget_limit"
        except ContextLimit:
            state.termination = "context_limit"
        except TimeoutError:
            state.termination = "wall_timeout"
        except ModelFailure:
            state.termination = "model_failure"
        except NativeBoundaryStop:
            pass
        except Exception as exc:
            state.termination = "execution_error"
            self.writer.append(
                "lifecycle", {"event": "native_execution_error", "type": type(exc).__name__}
            )
        finally:
            if handle is not None:
                try:
                    patch = self.sandbox.patch(handle)
                except Exception:
                    self.writer.append("lifecycle", {"event": "patch_export_failed"})
                    if state.termination == "submitted":
                        state.termination = "patch_export_failed"
                try:
                    self.sandbox.destroy(handle)
                except Exception:
                    state.termination = "cleanup_failed"
                    self.writer.append("lifecycle", {"event": "cleanup_failed", "handle": handle})
            self.writer.manifest.update(
                instance_id=task.instance_id,
                arm="A0",
                seed=seed,
                wall_ms=(self.clock() - started) * 1000,
                steps=state.step,
                accounted_cost_upper_usd=str(state.accounted_cost),
            )
            self.writer.snapshot(
                "state",
                {"state": asdict(state), "messages": agent.messages if agent is not None else []},
            )
            self.writer.finish(
                state.termination or "execution_error",
                {
                    "instance_id": task.instance_id,
                    "model_name_or_path": "mini-swe-agent-A0",
                    "model_patch": patch,
                },
            )
        return RunResult(state, patch)
