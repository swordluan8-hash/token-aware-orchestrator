#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from local_worker_probe import ProbeResult, run_probe

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


SUPPORTED_EXECUTORS = ("command", "aider", "codex")


def _estimate_tokens(byte_count: int, chars_per_token: int = 4) -> int:
    if byte_count <= 0:
        return 0
    return max(1, math.ceil(byte_count / chars_per_token))


def _truncate_text(raw: str, max_bytes: int, marker: str = "[context-circuit-trimmed]") -> tuple[str, bool, int]:
    if raw is None:
        return "", False, 0
    encoded = raw.encode("utf-8", errors="ignore")
    if max_bytes <= 0 or len(encoded) <= max_bytes:
        return raw, False, 0
    marker_bytes = marker.encode("utf-8", errors="ignore")
    avail = max_bytes - len(marker_bytes)
    if avail <= 0:
        return marker[:max_bytes], True, len(raw.encode("utf-8", errors="ignore"))
    head = encoded[: max(1, avail // 2)]
    tail = encoded[-max(1, avail // 2):] if avail - len(head) > 1 else b""
    trimmed = (head + marker_bytes + tail).decode("utf-8", errors="ignore")
    return trimmed, True, len(raw.encode("utf-8", errors="ignore")) - len(trimmed.encode("utf-8", errors="ignore"))


def _estimate_context_pressure_bytes(accounting: TokenAccounting) -> int:
    return (
        accounting.codex_context_after_bytes
        + accounting.log_raw_bytes
        + accounting.tool_output_bytes
        + accounting.git_diff_bytes
        + accounting.source_truncated_bytes
        + accounting.log_truncated_bytes
        + accounting.tool_output_truncated_bytes
    )


def _circuit_checkpoint_text(state: ContextCircuitState, accounting: TokenAccounting) -> str:
    return (
        "CONTEXT CIRCUIT CHECKPOINT\\n"
        f"risk={state.trip_reason}\\n"
        f"trip_stage={state.trip_stage}\\n"
        f"context_pressure_bytes={_estimate_context_pressure_bytes(accounting)}\\n"
        f"context_pressure_tokens_est={_estimate_tokens(_estimate_context_pressure_bytes(accounting))}\\n"
        f"trip_metric={state.trip_metric}\\n"
        f"trip_value={state.trip_value}\\n"
        f"trip_threshold={state.trip_threshold}\\n"
        f"risk_events={len(state.risk_signals)}\\n"
        f"hand_off_version=1.0.0-dev"
    )


def _update_context_circuit(state: ContextCircuitState, accounting: TokenAccounting, stage: str, *, source_read: int = 0) -> bool:
    if not state.enabled:
        return False

    context_pressure = _estimate_context_pressure_bytes(accounting) + source_read
    state.peak_track(context_pressure)

    if source_read > 0:
        if source_read > state.source_read_hard_bytes:
            state.trip(stage, "source_read_bytes", source_read, state.source_read_hard_bytes, level="hard")
            return True
        if source_read > state.source_read_warning_bytes:
            state.trip(stage, "source_read_bytes", source_read, state.source_read_warning_bytes, level="warning")
            return True

    if context_pressure > state.hard_token_bytes:
        state.trip(stage, "estimated_context_pressure_bytes", context_pressure, state.hard_token_bytes, level="hard")
        return True
    if context_pressure > state.warning_token_bytes:
        state.trip(stage, "estimated_context_pressure_bytes", context_pressure, state.warning_token_bytes, level="warning")
        return True

    if accounting.log_raw_bytes > state.log_hard_bytes:
        state.trip(stage, "log_raw_bytes", accounting.log_raw_bytes, state.log_hard_bytes, level="hard")
        return True
    if accounting.log_raw_bytes > state.log_warning_bytes:
        state.trip(stage, "log_raw_bytes", accounting.log_raw_bytes, state.log_warning_bytes, level="warning")
        return True

    if accounting.tool_output_bytes > state.tool_output_hard_bytes:
        state.trip(stage, "tool_output_bytes", accounting.tool_output_bytes, state.tool_output_hard_bytes, level="hard")
        return True
    if accounting.tool_output_bytes > state.tool_output_warning_bytes:
        state.trip(stage, "tool_output_bytes", accounting.tool_output_bytes, state.tool_output_warning_bytes, level="warning")
        return True

    if accounting.git_diff_bytes > state.diff_hard_bytes:
        state.trip(stage, "git_diff_bytes", accounting.git_diff_bytes, state.diff_hard_bytes, level="hard")
        return True
    if accounting.git_diff_bytes > state.diff_warning_bytes:
        state.trip(stage, "git_diff_bytes", accounting.git_diff_bytes, state.diff_warning_bytes, level="warning")
        return True

    return False


@dataclass
class TokenAccounting:
    mode: str

    candidate_bytes: int = 0
    candidate_file_count: int = 0
    candidate_line_count: int = 0
    input_task_bytes: int = 0
    codex_context_before_bytes: int = 0
    codex_context_after_bytes: int = 0

    file_count_read: int = 0
    lines_read: int = 0

    log_raw_bytes: int = 0
    log_filtered_bytes: int = 0
    git_diff_bytes: int = 0
    tool_output_bytes: int = 0
    source_truncated_bytes: int = 0
    tool_output_truncated_bytes: int = 0
    log_truncated_bytes: int = 0

    local_worker_calls: int = 0
    codex_direct_calls: int = 0
    escalation_count: int = 0
    local_retry_count: int = 0

    real_input_tokens: Optional[int] = None
    real_output_tokens: Optional[int] = None
    real_cached_tokens: Optional[int] = None
    real_reasoning_output_tokens: Optional[int] = None

    budget_limit_tokens: Optional[int] = None
    budget_preflight_context_bytes: int = 0
    budget_exceeded: bool = False

    estimated_tokens: int = 0
    token_source: str = "estimated"

    def add_log(self, raw: str, truncated: bool = False, truncated_bytes: int = 0) -> None:
        if raw is None:
            return
        self.log_raw_bytes += len(raw.encode("utf-8", errors="ignore"))
        self.log_filtered_bytes += len(_filter_log(raw).encode("utf-8", errors="ignore"))
        if truncated:
            self.log_truncated_bytes += max(0, truncated_bytes)

    def add_tool_output(
        self,
        raw: str,
        truncated: bool = False,
        truncated_bytes: int = 0,
        measured_bytes: Optional[int] = None,
    ) -> None:
        if raw is None:
            return
        if measured_bytes is None:
            measured_bytes = len(raw.encode("utf-8", errors="ignore"))
        self.tool_output_bytes += max(0, measured_bytes)
        if truncated:
            self.tool_output_truncated_bytes += max(0, truncated_bytes)

    def set_context(self, before: int, after: int) -> None:
        self.codex_context_before_bytes = max(0, before)
        self.codex_context_after_bytes = max(0, after)

    def finalize_tokens(self) -> None:
        if self.real_input_tokens is None and self.real_output_tokens is None:
            self.token_source = "estimated"
            self.estimated_tokens = _estimate_tokens(self.codex_context_after_bytes)
        else:
            self.token_source = "actual"
            self.estimated_tokens = (self.real_input_tokens or 0) + (self.real_output_tokens or 0)

    def context_reduction_ratio(self) -> float:
        if self.codex_context_before_bytes <= 0:
            return 0.0
        return max(0.0, round(1 - (self.codex_context_after_bytes / self.codex_context_before_bytes), 6))

    def estimated_token_reduction_ratio(self) -> float:
        before = _estimate_tokens(self.codex_context_before_bytes)
        after = _estimate_tokens(self.codex_context_after_bytes)
        if before <= 0:
            return 0.0
        return max(0.0, round(1 - (after / before), 6))

    def to_json(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["context_reduction_ratio"] = self.context_reduction_ratio()
        payload["estimated_token_reduction_ratio"] = self.estimated_token_reduction_ratio()
        return payload


@dataclass
class ContextCircuitState:
    enabled: bool = True
    warning_token_bytes: int = 450_000
    hard_token_bytes: int = 900_000
    source_read_warning_bytes: int = 240_000
    source_read_hard_bytes: int = 420_000
    log_warning_bytes: int = 80_000
    log_hard_bytes: int = 120_000
    tool_output_warning_bytes: int = 80_000
    tool_output_hard_bytes: int = 120_000
    test_output_warning_bytes: int = 120_000
    test_output_hard_bytes: int = 200_000
    diff_warning_bytes: int = 120_000
    diff_hard_bytes: int = 200_000
    handoff_checkpoint_bytes: int = 20_000

    tripped: bool = False
    trip_stage: Optional[str] = None
    trip_reason: Optional[str] = None
    trip_metric: Optional[str] = None
    trip_value: int = 0
    trip_threshold: int = 0
    risk_signals: List[Dict[str, Any]] = field(default_factory=list)
    peak_context_pressure_bytes: int = 0
    truncated_due_circuit: Dict[str, int] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def note(self, stage: str, metric: str, value: int, threshold: int, level: str, detail: Optional[str] = None) -> None:
        self.risk_signals.append(
            {
                "stage": stage,
                "level": level,
                "metric": metric,
                "value": value,
                "threshold": threshold,
                "detail": detail or "",
            }
        )

    def trip(self, stage: str, metric: str, value: int, threshold: int, level: str = "warning") -> bool:
        if self.tripped:
            return True
        self.tripped = True
        self.trip_stage = stage
        self.trip_metric = metric
        self.trip_value = value
        self.trip_threshold = threshold
        self.trip_reason = f"{level}:{metric}:{value}>{threshold}"
        self.note(stage, metric, value, threshold, level)
        return True

    def peak_track(self, value: int) -> None:
        self.peak_context_pressure_bytes = max(self.peak_context_pressure_bytes, value)


def _build_context_circuit(config: Dict[str, Any]) -> ContextCircuitState:
    cfg = config["controls"].get("context_circuit", {})
    return ContextCircuitState(
        enabled=bool(cfg.get("enabled", True)),
        warning_token_bytes=int(cfg.get("warning_token_bytes", 450_000)),
        hard_token_bytes=int(cfg.get("hard_token_bytes", 900_000)),
        source_read_warning_bytes=int(cfg.get("source_read_warning_bytes", 240_000)),
        source_read_hard_bytes=int(cfg.get("source_read_hard_bytes", 420_000)),
        log_warning_bytes=int(cfg.get("log_warning_bytes", 80_000)),
        log_hard_bytes=int(cfg.get("log_hard_bytes", 120_000)),
        tool_output_warning_bytes=int(cfg.get("tool_output_warning_bytes", 80_000)),
        tool_output_hard_bytes=int(cfg.get("tool_output_hard_bytes", 120_000)),
        test_output_warning_bytes=int(cfg.get("test_output_warning_bytes", 120_000)),
        test_output_hard_bytes=int(cfg.get("test_output_hard_bytes", 200_000)),
        diff_warning_bytes=int(cfg.get("diff_warning_bytes", 120_000)),
        diff_hard_bytes=int(cfg.get("diff_hard_bytes", 200_000)),
        handoff_checkpoint_bytes=int(cfg.get("handoff_checkpoint_bytes", 20_000)),
    )


@dataclass
class ExecutionSummary:
    success: bool
    executor: str
    attempts: int
    exit_code: Optional[int]
    detail: str
    output: str
    duration_ms: float

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskInput:
    raw: str
    payload: Dict[str, Any]

    @property
    def task_text(self) -> str:
        return str(self.payload.get("task", self.raw)).strip()

    @property
    def executor(self) -> str:
        requested = str(self.payload.get("executor", "codex")).strip().lower()
        return requested if requested in SUPPORTED_EXECUTORS else "codex"

    @property
    def actions(self) -> List[Dict[str, Any]]:
        actions = self.payload.get("actions")
        return actions if isinstance(actions, list) else []

    @property
    def command(self) -> Optional[str]:
        cmd = self.payload.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
        return None

    @property
    def scope_files(self) -> List[str]:
        raw = self.payload.get("scope_files")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    @property
    def expected_files(self) -> List[str]:
        raw = self.payload.get("expected_files")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    @property
    def test_env(self) -> Dict[str, str]:
        raw = self.payload.get("test_env")
        env: Dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if value is None:
                    continue
                env[str(key)] = str(value)
        return env

    @property
    def task_type(self) -> str:
        return str(self.payload.get("task_type", "general")).strip() or "general"

    @property
    def target_model(self) -> Optional[str]:
        value = self.payload.get("target_model")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    @property
    def max_budget(self) -> Optional[int]:
        value = self.payload.get("max_budget")
        if isinstance(value, bool):
            return None
        try:
            budget = int(value)
        except (TypeError, ValueError):
            return None
        return budget if budget > 0 else None

    @property
    def priority(self) -> str:
        return str(self.payload.get("priority", "normal")).strip() or "normal"

    @property
    def success_criteria(self) -> List[str]:
        raw = self.payload.get("success_criteria")
        return [str(item).strip() for item in raw if str(item).strip()] if isinstance(raw, list) else []


@dataclass
class ContextPayload:
    files: List[str]
    prompt: str
    file_count: int
    line_count: int
    byte_count: int


@dataclass
class RoutingDecision:
    route: str
    reason: str
    executor_mode: str
    local_preferred: bool
    probe: Optional[Dict[str, Any]] = None


class _BaseExecutor:
    name = "base"

    def run(
        self,
        task: TaskInput,
        repo: Path,
        config: Dict[str, Any],
        selected_files: List[str],
        attempt: int,
        accounting: TokenAccounting,
        context_circuit: Optional[ContextCircuitState] = None,
    ) -> ExecutionSummary:
        raise NotImplementedError


class CommandExecutor(_BaseExecutor):
    name = "command"

    def run(
        self,
        task: TaskInput,
        repo: Path,
        config: Dict[str, Any],
        selected_files: List[str],
        attempt: int,
        accounting: TokenAccounting,
        context_circuit: Optional[ContextCircuitState] = None,
    ) -> ExecutionSummary:
        timeout = int(config["controls"]["execution"]["command_timeout_seconds"])
        circuit_cfg = config["controls"]["context_circuit"]
        log_limit = int(circuit_cfg.get("tool_output_warning_bytes", 80_000))
        if task.actions:
            start = time.perf_counter()
            outputs: List[str] = []
            for action in task.actions:
                if not isinstance(action, dict) or action.get("type") != "shell":
                    return ExecutionSummary(False, "command", attempt, None, "invalid_action", "unsupported action", 0.0)
                cmd = action.get("command")
                if not isinstance(cmd, str) or not cmd.strip():
                    return ExecutionSummary(False, "command", attempt, None, "invalid_action", "empty command", 0.0)
                cp = self._run_shell(cmd, repo, timeout)
                raw_output = (cp.stdout or "") + (cp.stderr or "")
                raw_output_bytes = len(raw_output.encode("utf-8", errors="ignore"))
                trimmed, _, trimmed_bytes = _truncate_text(raw_output, log_limit)
                accounting.add_tool_output(
                    trimmed,
                    truncated=(trimmed_bytes > 0),
                    truncated_bytes=trimmed_bytes,
                    measured_bytes=raw_output_bytes,
                )
                if trimmed_bytes > 0 and context_circuit:
                    context_circuit.truncated_due_circuit["command_output"] = context_circuit.truncated_due_circuit.get(
                        "command_output", 0
                    ) + 1
                    context_circuit.note(
                        "command",
                        "command_output_trimmed",
                        len(raw_output.encode("utf-8", errors="ignore")),
                        log_limit,
                        "warning",
                        "command output clipped by context circuit",
                    )
                outputs.append(trimmed)
                if cp.returncode != 0:
                    elapsed = (time.perf_counter() - start) * 1000.0
                    return ExecutionSummary(False, "command", attempt, cp.returncode, "action_failed", (cp.stderr or ""), elapsed)
            elapsed = (time.perf_counter() - start) * 1000.0
            return ExecutionSummary(True, "command", attempt, 0, "actions_ok", "\n".join(outputs), elapsed)

        cmd = task.command
        if not cmd:
            return ExecutionSummary(False, "command", attempt, None, "command_missing", "", 0.0)

        start = time.perf_counter()
        cp = self._run_shell(cmd, repo, timeout)
        elapsed = (time.perf_counter() - start) * 1000.0
        raw_output = (cp.stdout or "") + (cp.stderr or "")
        raw_output_bytes = len(raw_output.encode("utf-8", errors="ignore"))
        trimmed, _, trimmed_bytes = _truncate_text(raw_output, log_limit)
        accounting.add_tool_output(
            trimmed,
            truncated=(trimmed_bytes > 0),
            truncated_bytes=trimmed_bytes,
            measured_bytes=raw_output_bytes,
        )
        if trimmed_bytes > 0 and context_circuit:
            context_circuit.truncated_due_circuit["command_output"] = context_circuit.truncated_due_circuit.get(
                "command_output", 0
            ) + 1
            context_circuit.note("command", "command_output_trimmed", len(raw_output.encode("utf-8", errors="ignore")), log_limit, "warning", "command output clipped by context circuit")
        return ExecutionSummary(cp.returncode == 0, "command", attempt, cp.returncode, f"returncode={cp.returncode}", trimmed[:12000], elapsed)

    def _run_shell(self, command: str, repo: Path, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            cwd=str(repo),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )


class AiderExecutor(_BaseExecutor):
    name = "aider"

    def run(
        self,
        task: TaskInput,
        repo: Path,
        config: Dict[str, Any],
        selected_files: List[str],
        attempt: int,
        accounting: TokenAccounting,
        context_circuit: Optional[ContextCircuitState] = None,
    ) -> ExecutionSummary:
        mode = config["executors"]["aider"].get("mode", "real")
        if mode == "mock":
            return self._run_mock(task, config, attempt, accounting, context_circuit=context_circuit)

        if not config["local_worker"].get("model"):
            return ExecutionSummary(False, "aider", attempt, None, "missing_model", "local_worker.model empty", 0.0)

        aider_cfg = config["executors"].get("aider", {})
        binary = str(aider_cfg.get("binary") or "aider")
        timeout = int(
            aider_cfg.get(
                "command_timeout_seconds",
                config["controls"]["execution"].get("command_timeout_seconds", 120),
            )
        )

        model = _build_aider_model(config)
        cmd = [binary, "--yes-always", "--no-tty", "--message", task.task_text]
        if model:
            cmd.extend(["--model", model])

        extra_args = aider_cfg.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend(map(str, extra_args))

        cmd.extend(selected_files)

        start = time.perf_counter()
        cp = subprocess.run(
            cmd,
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        elapsed = (time.perf_counter() - start) * 1000.0
        raw_output = (cp.stdout or "") + (cp.stderr or "")
        circuit_cfg = config["controls"]["context_circuit"]
        output_limit = int(circuit_cfg.get("tool_output_warning_bytes", 80_000))
        trimmed, _, trimmed_bytes = _truncate_text(raw_output, output_limit)
        accounting.add_tool_output(
            trimmed,
            truncated=(trimmed_bytes > 0),
            truncated_bytes=trimmed_bytes,
            measured_bytes=len(raw_output.encode("utf-8", errors="ignore")),
        )
        if trimmed_bytes > 0 and context_circuit:
            context_circuit.truncated_due_circuit["aider_output"] = context_circuit.truncated_due_circuit.get(
                "aider_output", 0
            ) + 1
            context_circuit.note(
                "aider",
                "aider_output_trimmed",
                len(raw_output.encode("utf-8", errors="ignore")),
                output_limit,
                "warning",
                "aider output clipped by context circuit",
            )

        for label, value in _parse_aider_tokens(trimmed).items():
            if value is None:
                continue
            if label == "input":
                accounting.real_input_tokens = value
            elif label == "output":
                accounting.real_output_tokens = value
            elif label == "cache":
                accounting.real_cached_tokens = value

        return ExecutionSummary(cp.returncode == 0, "aider", attempt, cp.returncode, f"returncode={cp.returncode}", trimmed[:12000], elapsed)

    def _run_mock(
        self,
        task: TaskInput,
        config: Dict[str, Any],
        attempt: int,
        accounting: TokenAccounting,
        context_circuit: Optional[ContextCircuitState] = None,
    ) -> ExecutionSummary:
        mock_cfg = config["local_worker"].get("mock", {})
        fail_on_attempts = int(mock_cfg.get("fail_on_attempts", 0))
        if attempt <= fail_on_attempts:
            error = str(mock_cfg.get("error", "mock failure"))
            return ExecutionSummary(False, "aider", attempt, None, "mock_forced_failure", error, 0.0)

        if task.actions:
            return CommandExecutor().run(task, Path(config["_repo_cache"]), config, [], attempt, accounting, context_circuit=context_circuit)

        fallback_cmd = task.command or ""
        if not fallback_cmd:
            return ExecutionSummary(False, "aider", attempt, None, "mock_no_fallback_command", "mock path requires command fallback", 0.0)
        return CommandExecutor().run(task, Path(config["_repo_cache"]), config, [], attempt, accounting, context_circuit=context_circuit)


def _parse_codex_jsonl(raw: str) -> Dict[str, Optional[int]]:
    """Extract aggregate usage from the JSONL stream emitted by `codex exec --json`."""
    totals: Dict[str, int] = {"input": 0, "output": 0, "cache": 0, "reasoning_output": 0}
    seen = False
    for line in (raw or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        seen = True
        for source, target in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cached_input_tokens", "cache"),
            ("reasoning_output_tokens", "reasoning_output"),
        ):
            value = usage.get(source)
            if isinstance(value, int) and value >= 0:
                totals[target] += value
    if not seen:
        return {key: None for key in totals}
    return totals


def _reported_usage_total(usage: Dict[str, Optional[int]]) -> Optional[int]:
    if usage.get("input") is None or usage.get("output") is None:
        return None
    return int(usage["input"] or 0) + int(usage["output"] or 0)


class CodexExecutor(_BaseExecutor):
    """Run the real Codex CLI and retain its authoritative usage events."""

    name = "codex"

    def run(
        self,
        task: TaskInput,
        repo: Path,
        config: Dict[str, Any],
        selected_files: List[str],
        attempt: int,
        accounting: TokenAccounting,
        context_circuit: Optional[ContextCircuitState] = None,
    ) -> ExecutionSummary:
        codex_cfg = config["executors"].get("codex", {})
        binary = str(codex_cfg.get("binary") or "codex")
        timeout = int(codex_cfg.get("command_timeout_seconds", config["controls"]["execution"].get("command_timeout_seconds", 120)))
        sandbox = str(codex_cfg.get("sandbox") or "workspace-write")
        model = str(codex_cfg.get("model") or task.target_model or "").strip()
        fixed_overhead_tokens = int(codex_cfg.get("preflight_overhead_tokens", 48_000))

        scope_note = ""
        if selected_files:
            scope_note = (
                "\n\nInitial repository scope selected by the orchestrator:\n"
                + "\n".join(f"- {path}" for path in selected_files)
                + "\nInspect these first. Do not modify files outside the task unless necessary."
            )
        prompt = task.task_text + scope_note
        accounting.budget_limit_tokens = task.max_budget
        estimated_prompt_tokens = _estimate_tokens(len(prompt.encode("utf-8", errors="ignore")))
        estimated_minimum_tokens = fixed_overhead_tokens + estimated_prompt_tokens
        if task.max_budget is not None and estimated_minimum_tokens > task.max_budget:
            return ExecutionSummary(
                False,
                "codex",
                attempt,
                None,
                "budget_preflight_blocked",
                "estimated_minimum_tokens="
                f"{estimated_minimum_tokens} (fixed_overhead={fixed_overhead_tokens}; prompt={estimated_prompt_tokens}) "
                f"exceeds max_budget={task.max_budget}",
                0.0,
            )
        cmd = [binary, "exec", "--json", "--sandbox", sandbox]
        if model:
            cmd.extend(["--model", model])
        extra_args = codex_cfg.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend(map(str, extra_args))
        cmd.append(prompt)

        start = time.perf_counter()
        stdout_lines: List[str] = []
        stderr_text = ""
        budget_exceeded = False
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(repo),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                stdout_lines.append(line)
                usage_now = _parse_codex_jsonl("".join(stdout_lines))
                total_now = _reported_usage_total(usage_now)
                if task.max_budget is not None and total_now is not None and total_now > task.max_budget:
                    budget_exceeded = True
                    accounting.budget_exceeded = True
                    process.terminate()
                    break
            remaining_stdout, stderr_text = process.communicate(timeout=timeout)
            if remaining_stdout:
                stdout_lines.append(remaining_stdout)
            cp = subprocess.CompletedProcess(cmd, process.returncode, "".join(stdout_lines), stderr_text or "")
        except FileNotFoundError:
            return ExecutionSummary(False, "codex", attempt, None, "codex_not_found", f"binary not found: {binary}", 0.0)
        except subprocess.TimeoutExpired as exc:
            raw = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, str) else ""
            accounting.add_tool_output(raw)
            return ExecutionSummary(False, "codex", attempt, None, "timeout", raw[:12000], (time.perf_counter() - start) * 1000.0)

        elapsed = (time.perf_counter() - start) * 1000.0
        raw_output = (cp.stdout or "") + (cp.stderr or "")
        output_limit = int(config["controls"]["context_circuit"].get("tool_output_warning_bytes", 80_000))
        trimmed, _, trimmed_bytes = _truncate_text(raw_output, output_limit)
        accounting.add_tool_output(trimmed, truncated=(trimmed_bytes > 0), truncated_bytes=trimmed_bytes, measured_bytes=len(raw_output.encode("utf-8", errors="ignore")))

        usage = _parse_codex_jsonl(cp.stdout or "")
        accounting.real_input_tokens = usage["input"]
        accounting.real_output_tokens = usage["output"]
        accounting.real_cached_tokens = usage["cache"]
        accounting.real_reasoning_output_tokens = usage["reasoning_output"]
        actual_total = _reported_usage_total(usage)
        budget_detail = ""
        if task.max_budget is not None and actual_total is not None:
            budget_detail = f"; reported_total={actual_total}; max_budget={task.max_budget}"
        if budget_exceeded:
            detail = f"budget_exceeded; returncode={cp.returncode}; usage=actual{budget_detail}"
        else:
            detail = f"returncode={cp.returncode}; usage={'actual' if usage['input'] is not None else 'unavailable'}{budget_detail}"
        return ExecutionSummary((cp.returncode == 0) and not budget_exceeded, "codex", attempt, cp.returncode, detail, trimmed[:12000], elapsed)


def _parse_aider_tokens(raw: str) -> Dict[str, Optional[int]]:
    text = raw or ""
    inp_match = re.search(r"input tokens:\s*(\d+)", text, flags=re.IGNORECASE)
    out_match = re.search(r"output tokens:\s*(\d+)", text, flags=re.IGNORECASE)
    cache_match = re.search(r"cache.*tokens:\s*(\d+)", text, flags=re.IGNORECASE)
    return {
        "input": int(inp_match.group(1)) if inp_match else None,
        "output": int(out_match.group(1)) if out_match else None,
        "cache": int(cache_match.group(1)) if cache_match else None,
    }


def _filter_log(raw: str) -> str:
    lines = []
    for line in (raw or "").splitlines():
        low = line.lower()
        if any(keyword in low for keyword in ["error", "failed", "timeout", "traceback", "oom", "out of memory"]):
            lines.append(line)
            continue
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _load_yaml_like(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(text)
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config {path} must be a mapping")
        return loaded

    # ultra-light fallback parser for key/value / nested maps
    lines = [line.rstrip("\n") for line in text.splitlines()]
    stack: List[Any] = []
    indent_stack: List[int] = []
    root: Dict[str, Any] = {}
    stack.append(root)
    indent_stack.append(-1)

    def current_container(indent: int) -> Any:
        while indent_stack and indent <= indent_stack[-1]:
            stack.pop()
            indent_stack.pop()
        return stack[-1] if stack else root

    def parse_scalar(raw_value: str) -> Any:
        value = raw_value.strip()
        if value == "[]":
            return []
        if value.startswith("[") and value.endswith("]"):
            body = value[1:-1].strip()
            if not body:
                return []
            return [parse_scalar(item.strip()) for item in body.split(",") if item.strip()]
        if value.lower() in {"true", "yes"}:
            return True
        if value.lower() in {"false", "no"}:
            return False
        if value.lower() == "null":
            return None
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        try:
            if "." in value:
                return float(value)
            return int(value)
        except Exception:
            return value

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        container = current_container(indent)

        if isinstance(container, list):
            if not stripped.startswith("- "):
                raise ValueError(f"list item expected at {path}:{idx+1}")
            container.append(parse_scalar(stripped[2:].strip()))
            continue

        if ":" not in stripped:
            raise ValueError(f"bad yaml line {path}:{idx+1}")
        key, rest = stripped.split(":", 1)
        key = key.strip()
        rest = rest.strip()

        if rest:
            container[key] = parse_scalar(rest)
            continue

        nxt = None
        for j in range(idx + 1, len(lines)):
            candidate = lines[j].strip()
            if candidate and not candidate.startswith("#"):
                nxt = candidate
                break

        child: Any
        if nxt is not None and nxt.startswith("- "):
            child = []
        else:
            child = {}
        container[key] = child
        stack.append(child)
        indent_stack.append(indent)

    return root


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int)
    if expected == "number":
        return isinstance(value, (int, float))
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _validate_node(schema: Dict[str, Any], value: Any, path: str, errors: List[str]) -> None:
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        errors.append(f"{path}: expected {expected}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} not in {schema['enum']}")
    if expected == "object" and isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}.{required}: required")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                _validate_node(child, value[key], f"{path}.{key}", errors)


def _validate_config(config: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    _validate_node(schema, config, "config", errors)
    return errors


def _load_config(path: Optional[Path]) -> Dict[str, Any]:
    schema = _load_yaml_like(Path(__file__).resolve().parents[1] / "config.schema.yaml")
    defaults = schema.get("defaults", {}) or {}
    user = {}
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"config not found: {path}")
        user = _load_yaml_like(path)
    config = _deep_merge(defaults, user)
    errors = _validate_config(config, schema.get("schema", {}))
    if errors:
        raise ValueError("Config validation failed:\n" + "\n".join(errors))
    return config


def _parse_task(raw: str) -> TaskInput:
    payload: Any = {"task": raw}
    clean = (raw or "").strip()
    if clean.startswith("@"):
        task_path = Path(clean[1:]).expanduser()
        if not task_path.exists():
            raise FileNotFoundError(f"task payload not found: {task_path}")
        payload = json.loads(task_path.read_text(encoding="utf-8"))
    elif clean.startswith("{") and clean.endswith("}"):
        payload = json.loads(clean)
    if not isinstance(payload, dict):
        payload = {"task": raw}
    return TaskInput(raw=raw, payload=payload)


def _safe_read_text(path: Path, max_bytes: int = 200_000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    return raw.decode("utf-8", errors="replace")


def _is_text_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix not in {
        ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".bin", ".so", ".dll", ".pyc", ".woff", ".woff2", ".exe"
    }


def _iter_candidate_files(repo: Path, max_depth: int, max_files: int) -> List[Path]:
    skip_dirs = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
    files: List[Path] = []

    def allowed_depth(rel: Path) -> bool:
        return len(rel.parts) <= max_depth

    for path in repo.rglob("*"):
        if any(part in skip_dirs for part in path.relative_to(repo).parts):
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if rel.name.startswith("."):
            continue
        if not _is_text_file(path):
            continue
        if not allowed_depth(rel):
            continue
        files.append(rel)
        if len(files) >= max_files:
            break

    return sorted(files, key=lambda x: str(x).lower())


def _collect_context(
    repo: Path,
    files: Sequence[Path],
    max_bytes: int,
    accounting: Optional[TokenAccounting] = None,
    circuit: Optional[ContextCircuitState] = None,
) -> ContextPayload:
    selected: List[tuple[str, str]] = []
    total = 0
    lines = 0

    for rel in files:
        content = _safe_read_text(repo / rel)
        if not content:
            continue
        size = len(content.encode("utf-8"))
        if total + size > max_bytes:
            remain = max(0, max_bytes - total)
            if remain <= 0:
                break
            snippet = content.encode("utf-8", errors="ignore")[:remain].decode("utf-8", errors="ignore")
            selected.append((str(rel), snippet))
            truncated_bytes = max(0, size - len(snippet.encode("utf-8")))
            total += len(snippet.encode("utf-8"))
            lines += snippet.count("\n") + (1 if snippet else 0)
            if accounting is not None:
                accounting.source_truncated_bytes += truncated_bytes
            if circuit is not None:
                circuit.note(
                    "context_collect",
                    "source_file_snippet_truncated",
                    size,
                    max_bytes,
                    "warning",
                    f"file={rel}",
                )
            break

        selected.append((str(rel), content))
        total += size
        lines += content.count("\n") + 1

    prompt = "\n\n".join(f"# {path}\n{snippet}" for path, snippet in selected)
    return ContextPayload(
        files=[item[0] for item in selected],
        prompt=prompt,
        file_count=len(selected),
        line_count=lines,
        byte_count=total,
    )


def _select_files_for_orchestrated(repo: Path, task: TaskInput, config: Dict[str, Any]) -> List[Path]:
    explicit = [Path(p) for p in task.scope_files if str(p).strip()]
    selected: List[Path] = []

    if explicit:
        for rel in explicit:
            abs_path = rel if rel.is_absolute() else repo / rel
            if abs_path.exists() and abs_path.is_file():
                try:
                    selected.append(abs_path.relative_to(repo))
                except ValueError:
                    pass
        if selected:
            return selected

    keywords = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", task.task_text.lower()):
        if token not in keywords and token not in {"the", "and", "for", "with", "from", "that", "this", "into"}:
            keywords.append(token)

    candidates = _iter_candidate_files(
        repo,
        max_depth=int(config["context"].get("max_depth", 7)),
        max_files=int(config["context"].get("max_file_candidates", 120)),
    )

    ranked: List[tuple[int, Path]] = []
    for rel in candidates:
        rel_text = str(rel).lower()
        score = 0
        for kw in keywords[:20]:
            if kw and kw in rel_text:
                score += 2
        if score:
            ranked.append((score, rel))

    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked:
        candidates = [r[1] for r in ranked]

    max_files = int(config["context"].get("max_selected_files", 6))
    return candidates[:max_files]


def _build_context(
    repo: Path,
    task: TaskInput,
    config: Dict[str, Any],
    mode: str,
    accounting: TokenAccounting,
    circuit: Optional[ContextCircuitState] = None,
) -> List[str]:
    max_candidate_bytes = int(config["context"].get("max_candidate_bytes", 200_000))
    all_files = _iter_candidate_files(
        repo,
        max_depth=int(config["context"].get("max_depth", 7)),
        max_files=int(config["context"].get("max_file_candidates", 180)),
    )
    base = _collect_context(
        repo,
        all_files,
        max_candidate_bytes,
        accounting=accounting,
        circuit=circuit,
    )

    if mode == "baseline":
        accounting.candidate_bytes = base.byte_count
        accounting.candidate_file_count = base.file_count
        accounting.candidate_line_count = base.line_count
        accounting.file_count_read = base.file_count
        accounting.lines_read = base.line_count
        selected_prompt = base.prompt
    else:
        selected = _select_files_for_orchestrated(repo, task, config)
        after_limit = int(config["context"].get("max_after_bytes", 80_000))
        if task.max_budget is not None:
            task_bytes = len(task.task_text.encode("utf-8", errors="ignore"))
            budget_bytes = max(0, (task.max_budget * 4) - task_bytes - 512)
            after_limit = min(after_limit, budget_bytes)
            accounting.budget_limit_tokens = task.max_budget
            accounting.budget_preflight_context_bytes = after_limit
        sel = _collect_context(
            repo,
            selected,
            after_limit,
            accounting=accounting,
            circuit=circuit,
        )
        accounting.candidate_bytes = base.byte_count
        accounting.candidate_file_count = base.file_count
        accounting.candidate_line_count = base.line_count
        accounting.file_count_read = sel.file_count
        accounting.lines_read = sel.line_count
        selected_prompt = sel.prompt

    before = task.task_text + "\n\n" + base.prompt
    after = task.task_text + "\n\n" + selected_prompt
    accounting.set_context(len(before.encode("utf-8", errors="ignore")), len(after.encode("utf-8", errors="ignore")))
    accounting.input_task_bytes = len(task.task_text.encode("utf-8", errors="ignore"))
    return selected if mode != "baseline" else base.files


def _route(task: TaskInput, config: Dict[str, Any], mode: str, force_mock: bool = False) -> RoutingDecision:
    if mode == "baseline":
        if task.executor == "command":
            return RoutingDecision(
                route="command",
                reason="baseline_scripted_command",
                executor_mode="command",
                local_preferred=False,
            )
        return RoutingDecision(
            route="codex_direct",
            reason="baseline_mode_direct_codex",
            executor_mode="codex",
            local_preferred=False,
        )

    if task.executor == "codex":
        return RoutingDecision(
            route="codex_direct",
            reason="codex_executor_requested",
            executor_mode="codex",
            local_preferred=False,
        )

    if task.executor != "aider":
        return RoutingDecision(
            route="command",
            reason="command_executor_requested",
            executor_mode="command",
            local_preferred=False,
        )

    routing_cfg = config["routing"]
    if not bool(routing_cfg.get("prefer_local", True)):
        return RoutingDecision(
            route="codex_direct",
            reason="local_disabled",
            executor_mode="codex",
            local_preferred=False,
        )

    if len(task.task_text) > int(routing_cfg.get("max_task_chars_for_local", 1800)):
        return RoutingDecision(
            route="codex_direct",
            reason="task_too_long_for_local",
            executor_mode="codex",
            local_preferred=False,
        )

    provider = config["local_worker"].get("provider", "mock")
    provider = "mock" if force_mock else str(provider)

    probe = run_probe(
        provider=provider,
        model=str(config["local_worker"].get("model", "")),
        base_url=str(config["local_worker"].get("base_url", "http://127.0.0.1:11434")),
        num_ctx=int(config["local_worker"].get("num_ctx", 1024)),
        timeout_seconds=int(config["local_worker"].get("timeout_seconds", 8)),
        mock=(provider == "mock"),
        mock_available=bool(config["local_worker"].get("mock", {}).get("available", False)),
        mock_error=str(config["local_worker"].get("mock", {}).get("error", "")),
    )

    if not probe.available:
        return RoutingDecision(
            route="codex_direct",
            reason=f"local_probe_failed:{probe.status}",
            executor_mode="codex",
            local_preferred=True,
            probe=probe.to_json(),
        )

    exec_mode = "mock" if provider == "mock" else "real"
    return RoutingDecision(
        route="local",
        reason="local_available",
        executor_mode=exec_mode,
        local_preferred=True,
        probe=probe.to_json(),
    )


def _build_aider_model(config: Dict[str, Any]) -> str:
    explicit = config["executors"].get("aider", {}).get("model")
    if explicit:
        return str(explicit)
    local_model = str(config["local_worker"].get("model", ""))
    if "/" in local_model:
        return local_model
    provider = str(config["local_worker"].get("provider", "ollama_chat"))
    return f"{provider}/{local_model}" if local_model else provider


def _run_tests(
    repo: Path,
    config: Dict[str, Any],
    test_env: Dict[str, str],
    accounting: Optional[TokenAccounting] = None,
    context_circuit: Optional[ContextCircuitState] = None,
) -> Dict[str, Any]:
    project = _detect_project_type(repo, config)
    cmd = _find_test_command(project, repo, config)
    if not cmd:
        return {"status": "unknown", "reason": "test_command_not_found", "project": project}

    env = os.environ.copy()
    env.update(test_env)
    env["PYTHONUNBUFFERED"] = "1"

    start = time.perf_counter()
    cp = subprocess.run(
        cmd,
        cwd=str(repo),
        shell=True,
        text=True,
        capture_output=True,
        timeout=int(config["controls"]["execution"]["command_timeout_seconds"]),
        env=env,
    )
    elapsed = (time.perf_counter() - start) * 1000.0
    raw_output = (cp.stdout or "") + (cp.stderr or "")
    output_limit = int(config["controls"]["context_circuit"].get("test_output_warning_bytes", 120_000))
    output, _, trimmed_bytes = _truncate_text(raw_output, output_limit, "[test-output-trimmed-by-circuit]")
    raw_output_bytes = len(raw_output.encode("utf-8", errors="ignore"))
    if accounting is not None:
        accounting.add_log(raw_output, truncated=(trimmed_bytes > 0), truncated_bytes=trimmed_bytes)
        accounting.add_tool_output(
            output,
            truncated=(trimmed_bytes > 0),
            truncated_bytes=trimmed_bytes,
            measured_bytes=raw_output_bytes,
        )
    if trimmed_bytes > 0 and context_circuit is not None:
        context_circuit.truncated_due_circuit["test_output"] = context_circuit.truncated_due_circuit.get("test_output", 0) + 1
        context_circuit.note(
            "tests",
            "test_output_trimmed",
            len(raw_output.encode("utf-8", errors="ignore")),
            output_limit,
            "warning",
            "test output clipped by context circuit",
        )

    return {
        "status": "passed" if cp.returncode == 0 else "failed",
        "project": project,
        "command": cmd,
        "exit_code": cp.returncode,
        "duration_ms": elapsed,
        "output": output[:12000],
    }


def _detect_project_type(repo: Path, config: Dict[str, Any]) -> str:
    if (repo / "go.mod").exists():
        return "go"
    if (repo / "package.json").exists():
        return "node"
    if any((repo / path).exists() for path in config["test"].get("project_detectors", {}).get("python", ["pyproject.toml", "requirements.txt", "setup.py", "pytest.ini"])):
        return "python"
    return "unknown"


def _find_test_command(project: str, repo: Path, config: Dict[str, Any]) -> Optional[str]:
    commands = config["test"].get("commands", {})
    for item in commands.get(project, []):
        if item == "auto":
            if project == "python":
                if shutil.which("pytest"):
                    return "pytest -q"
                if shutil.which("python"):
                    return "python -m unittest discover"
            if project == "node":
                if (repo / "package.json").exists() and (shutil.which("npm") or shutil.which("pnpm")):
                    return "npm run test"
            if project == "go":
                if shutil.which("go"):
                    return "go test ./..."
            if project == "unknown":
                return ""
        elif isinstance(item, str):
            return item

    if project == "python":
        if shutil.which("pytest"):
            return "pytest -q"
        if shutil.which("python"):
            return "python -m unittest discover"
    if project == "node":
        if (repo / "package.json").exists() and shutil.which("npm"):
            return "npm run test"
    if project == "go" and shutil.which("go"):
        return "go test ./..."

    return None


def _run_git_diff(repo: Path, context_circuit: Optional[ContextCircuitState] = None) -> Dict[str, Any]:
    status = subprocess.run(["git", "status", "--short"], cwd=str(repo), text=True, capture_output=True)
    diff = subprocess.run(["git", "diff"], cwd=str(repo), text=True, capture_output=True)
    files: List[str] = []
    for line in status.stdout.splitlines():
        if len(line) > 3:
            path = line[3:].strip()
            parts = path.split()
            if parts:
                path = parts[0]
            if "__pycache__" in path:
                continue
            files.append(path)
    files = sorted({item for item in files if item})
    diff_text = diff.stdout or ""
    raw_diff_bytes = len(diff_text.encode("utf-8", errors="ignore"))
    if context_circuit is not None:
        limit = int(context_circuit.diff_hard_bytes)
        excerpt, _, trimmed_bytes = _truncate_text(diff_text, limit, "[git-diff-trimmed-by-circuit]")
        if trimmed_bytes > 0:
            context_circuit.truncated_due_circuit["git_diff"] = context_circuit.truncated_due_circuit.get("git_diff", 0) + 1
            context_circuit.note(
                "git_diff",
                "git_diff_trimmed",
                len(diff_text.encode("utf-8", errors="ignore")),
                limit,
                "warning",
                "git diff clipped by context circuit",
            )
        diff_bytes = len(excerpt.encode("utf-8", errors="ignore"))
    else:
        excerpt = diff_text
        diff_bytes = raw_diff_bytes

    return {
        # The handoff and quality gate consume `changed_files`.  Keep this
        # public field stable so unexpected-file detection cannot silently
        # report a clean diff after a task changed files outside its scope.
        "changed_files": files,
        "files_changed_count": len(files),
        "diff_excerpt": excerpt[:12_000],
        "diff_bytes": diff_bytes,
        "diff_raw_bytes": raw_diff_bytes,
    }


def _run_executor(
    name: str,
    config: Dict[str, Any],
    task: TaskInput,
    repo: Path,
    selected_files: List[str],
    attempt: int,
    accounting: TokenAccounting,
    context_circuit: Optional[ContextCircuitState] = None,
) -> ExecutionSummary:
    if name == "command":
        return CommandExecutor().run(task, repo, config, selected_files, attempt, accounting, context_circuit=context_circuit)
    if name == "codex":
        return CodexExecutor().run(task, repo, config, selected_files, attempt, accounting, context_circuit=context_circuit)
    return AiderExecutor().run(task, repo, config, selected_files, attempt, accounting, context_circuit=context_circuit)


def orchestrate(
    task_raw: str,
    repo: str,
    config_path: Optional[str],
    use_mock: bool,
    mode: str = "orchestrated",
) -> Dict[str, Any]:
    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.exists():
        raise FileNotFoundError(f"repo not found: {repo_path}")
    if not (repo_path / ".git").exists():
        raise RuntimeError(f"repo is not a git repo: {repo_path}")

    config = _load_config(Path(config_path) if config_path else None)
    config["_repo_cache"] = str(repo_path)

    task = _parse_task(task_raw)
    accounting = TokenAccounting(mode=mode)
    context_circuit = _build_context_circuit(config)
    control_cfg = config["controls"].get("context_circuit", {})
    handoff_checkpoint_limit = int(control_cfg.get("handoff_checkpoint_bytes", 20_000))

    stage_checkpoints: List[Dict[str, Any]] = []

    def add_checkpoint(stage: str, reason: str, stage_data: Optional[Dict[str, Any]] = None) -> None:
        stage_checkpoints.append(
            {
                "stage": stage,
                "reason": reason,
                "risk_events": len(context_circuit.risk_signals),
                "context_pressure_bytes": _estimate_context_pressure_bytes(accounting),
                "context_pressure_tokens_est": _estimate_tokens(_estimate_context_pressure_bytes(accounting)),
                "data": stage_data or {},
            }
        )

    def maybe_abort_after(stage: str, source_read: int = 0) -> bool:
        if _update_context_circuit(context_circuit, accounting, stage, source_read=source_read):
            accounting.add_log(_circuit_checkpoint_text(context_circuit, accounting))
            add_checkpoint(stage, f"context_risk:{context_circuit.trip_reason}")
            return True
        return False

    def build_handoff(
        execution_obj: ExecutionSummary,
        escalated_local: bool,
        test_result: Optional[Dict[str, Any]] = None,
        diff_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        final_route = {
            "mode": mode,
            "route": route.route,
            "reason": route.reason,
            "executor_mode": final_executor_mode,
            "local_preferred": route.local_preferred,
            "probe": route.probe,
            "selected_files": [str(item) for item in selected],
        }

        test_section = test_result or {"status": "not_run", "project": "", "command": "", "output": "", "duration_ms": 0}
        diff_section = diff_result or {"changed_files": [], "files_changed_count": 0, "unexpected_files": []}

        expected_files = sorted({Path(f).as_posix() for f in task.expected_files})
        changed_files = diff_section.get("changed_files", [])
        unexpected_files = []
        if expected_files:
            unexpected_files = [item for item in changed_files if item not in expected_files]

        no_test_command = test_section.get("status") == "unknown" and test_section.get("reason") == "test_command_not_found"
        handoff_risk = bool(context_circuit.tripped)
        final_task_success = (
            execution_obj.success
            and test_section.get("status") == "passed"
            and not unexpected_files
            and not handoff_risk
        )
        budget_blocked = execution_obj.detail.startswith("budget_preflight_blocked")
        budget_exceeded = execution_obj.detail.startswith("budget_exceeded")
        if budget_blocked or budget_exceeded:
            final_status = "budget_preflight_blocked" if budget_blocked else "budget_exceeded"
        elif context_circuit.tripped:
            final_status = "interrupted"
        elif no_test_command and execution_obj.success and not unexpected_files:
            final_status = "review_required"
        else:
            final_status = "success" if final_task_success else "failed"

        requires_codex_review = (
            context_circuit.tripped
            or escalated_local
            or not execution_obj.success
            or test_section.get("status") != "passed"
            or bool(unexpected_files)
        )

        if context_circuit.tripped:
            if "output" in test_section and isinstance(test_section["output"], str):
                test_section["output"] = test_section["output"][:512]
            if isinstance(changed_files, list):
                diff_section["changed_files"] = changed_files[:20]
            diff_section["unexpected_files"] = unexpected_files[:20]

        handoff_accounting = accounting.to_json()
        handoff_accounting = {
            "mode": handoff_accounting.get("mode"),
            "candidate_bytes": handoff_accounting.get("candidate_bytes"),
            "candidate_file_count": handoff_accounting.get("candidate_file_count"),
            "candidate_line_count": handoff_accounting.get("candidate_line_count"),
            "input_task_bytes": handoff_accounting.get("input_task_bytes"),
            "codex_context_before_bytes": handoff_accounting.get("codex_context_before_bytes"),
            "codex_context_after_bytes": handoff_accounting.get("codex_context_after_bytes"),
            "file_count_read": handoff_accounting.get("file_count_read"),
            "lines_read": handoff_accounting.get("lines_read"),
            "log_raw_bytes": handoff_accounting.get("log_raw_bytes"),
            "log_filtered_bytes": handoff_accounting.get("log_filtered_bytes"),
            "tool_output_bytes": handoff_accounting.get("tool_output_bytes"),
            "tool_output_truncated_bytes": handoff_accounting.get("tool_output_truncated_bytes"),
            "log_truncated_bytes": handoff_accounting.get("log_truncated_bytes"),
            "source_truncated_bytes": handoff_accounting.get("source_truncated_bytes"),
            "git_diff_bytes": handoff_accounting.get("git_diff_bytes"),
            "local_worker_calls": handoff_accounting.get("local_worker_calls"),
            "codex_direct_calls": handoff_accounting.get("codex_direct_calls"),
            "escalation_count": handoff_accounting.get("escalation_count"),
            "local_retry_count": handoff_accounting.get("local_retry_count"),
            "token_source": handoff_accounting.get("token_source"),
            "estimated_tokens": handoff_accounting.get("estimated_tokens"),
            "context_reduction_ratio": handoff_accounting.get("context_reduction_ratio"),
            "estimated_token_reduction_ratio": handoff_accounting.get("estimated_token_reduction_ratio"),
            "real_input_tokens": handoff_accounting.get("real_input_tokens"),
            "real_output_tokens": handoff_accounting.get("real_output_tokens"),
            "real_cached_tokens": handoff_accounting.get("real_cached_tokens"),
            "real_reasoning_output_tokens": handoff_accounting.get("real_reasoning_output_tokens"),
            "budget_limit_tokens": handoff_accounting.get("budget_limit_tokens"),
            "budget_preflight_context_bytes": handoff_accounting.get("budget_preflight_context_bytes"),
            "budget_exceeded": handoff_accounting.get("budget_exceeded"),
        }

        handoff = {
            "version": "1.0.0-dev",
            "task_id": hashlib.sha1((task.raw + str(start_all)).encode("utf-8", errors="ignore")).hexdigest()[:16],
            "route": final_route,
            "routing": final_route,
            "stage_checkpoints": stage_checkpoints[-6:],
            "handoff_checkpoint": {
                "requested": bool(context_circuit.tripped),
                "trip_stage": context_circuit.trip_stage,
                "trip_reason": context_circuit.trip_reason,
                "trip_metric": context_circuit.trip_metric,
                "trip_value": context_circuit.trip_value,
                "trip_threshold": context_circuit.trip_threshold,
                "context_pressure_bytes": _estimate_context_pressure_bytes(accounting),
                "context_pressure_tokens_est": _estimate_tokens(_estimate_context_pressure_bytes(accounting)),
                "risk_signals": context_circuit.risk_signals,
                "truncated_by_circuit": context_circuit.truncated_due_circuit,
                "checkpoint_text": _circuit_checkpoint_text(context_circuit, accounting),
            },
            "execution": {
                "executor": execution_obj.executor,
                "success": execution_obj.success,
                "attempts": execution_obj.attempts,
                "attempts_local": accounting.local_worker_calls,
                "attempts_codex_direct": accounting.codex_direct_calls,
                "exit_code": execution_obj.exit_code,
                "detail": execution_obj.detail,
                "duration_ms": execution_obj.duration_ms,
                "escalated_to_codex": escalated_local,
                "local_retries": accounting.local_retry_count,
            },
            "test": test_section,
            "diff": diff_section,
            "accounting": handoff_accounting,
            "context_circuit": context_circuit.to_json(),
            "final": {
                "status": final_status,
                "task_success": bool(final_task_success),
                "requires_codex_review": bool(requires_codex_review),
                "wall_clock_ms": (time.perf_counter() - start_all) * 1000.0,
                "interrupt": bool(context_circuit.tripped),
                "next_step": "run_tests_or_review" if final_status == "review_required" else "continue_from_handoff_if_needed",
            },
            "task": {
                "executor": task.executor,
                "task_type": task.task_type,
                "target_model": task.target_model,
                "max_budget": task.max_budget,
                "priority": task.priority,
                "success_criteria": task.success_criteria,
                "expected_files": expected_files,
            },
        }

        handoff_payload = json.dumps(handoff, ensure_ascii=False)
        handoff["handoff_bytes"] = len(handoff_payload.encode("utf-8", errors="ignore"))
        handoff["handoff_estimated_tokens"] = _estimate_tokens(handoff["handoff_bytes"])
        if handoff["handoff_bytes"] > handoff_checkpoint_limit:
            handoff["handoff_truncated_for_checkpoint"] = handoff_checkpoint_limit
            if isinstance(handoff.get("test"), dict) and isinstance(handoff["test"].get("output"), str):
                handoff["test"]["output"] = handoff["test"]["output"][:320]
            if isinstance(handoff.get("diff"), dict) and isinstance(handoff["diff"].get("changed_files"), list):
                handoff["diff"]["changed_files"] = handoff["diff"]["changed_files"][:20]
            if isinstance(handoff.get("stage_checkpoints"), list):
                handoff["stage_checkpoints"] = handoff["stage_checkpoints"][-2:]
            add_checkpoint("build_handoff", "handoff_size_exceeded")
            handoff_payload = json.dumps(handoff, ensure_ascii=False)
            handoff["handoff_bytes"] = len(handoff_payload.encode("utf-8", errors="ignore"))
            handoff["handoff_estimated_tokens"] = _estimate_tokens(handoff["handoff_bytes"])
        return handoff

    route = _route(task, config, mode, force_mock=use_mock)
    selected: List[str] = []
    final_executor_mode = route.executor_mode

    start_all = time.perf_counter()
    if route.route == "local":
        selected = _build_context(repo_path, task, config, mode, accounting, circuit=context_circuit)
        if maybe_abort_after("build_context", source_read=accounting.candidate_bytes):
            add_checkpoint("build_context", "context risk detected")
            handoff = build_handoff(ExecutionSummary(False, "context_circuit", 0, None, "context_risk", "", 0.0), False)
            accounting.finalize_tokens()
            return {"routing": {
                "mode": mode,
                "route": route.route,
                "reason": route.reason,
                "executor_mode": route.executor_mode,
                "local_preferred": route.local_preferred,
                "probe": route.probe,
                "selected_files": selected,
            }, "handoff": handoff}
    else:
        # Direct Codex still receives the selected scope as task guidance.
        selected = _build_context(repo_path, task, config, mode, accounting, circuit=context_circuit)
        if maybe_abort_after("build_context", source_read=accounting.candidate_bytes):
            handoff = build_handoff(ExecutionSummary(False, "context_circuit", 0, None, "context_risk", "", 0.0), False)
            accounting.finalize_tokens()
            return {"routing": {"mode": mode, "route": route.route, "reason": route.reason, "executor_mode": route.executor_mode, "local_preferred": route.local_preferred, "probe": route.probe, "selected_files": selected}, "handoff": handoff}

    execution = ExecutionSummary(False, "command", 1, None, "not_started", "", 0.0)
    escalated = False
    local_failures = 0

    if route.route == "local":
        config["executors"].setdefault("aider", {})["mode"] = route.executor_mode
        threshold = int(config["routing"].get("fail_threshold", 2))
        wait_seconds = int(config["routing"].get("local_retry_wait_seconds", 0))

        for attempt in range(1, threshold + 1):
            accounting.local_worker_calls += 1
            execution = _run_executor(
                "aider",
                config,
                task,
                repo_path,
                selected,
                attempt,
                accounting,
                context_circuit=context_circuit,
            )
            accounting.add_log(execution.output)
            if maybe_abort_after(f"execution_attempt_{attempt}"):
                escalated = True
                break
            if execution.success:
                break
            local_failures += 1
            if attempt < threshold:
                accounting.local_retry_count += 1
                if wait_seconds > 0:
                    time.sleep(wait_seconds)

        if not execution.success:
            escalated = True
            accounting.escalation_count += 1
            final_executor_mode = "codex"
            accounting.codex_direct_calls += 1
            if not context_circuit.tripped:
                execution = _run_executor(
                    "codex",
                    config,
                    task,
                    repo_path,
                    selected,
                    local_failures + 1,
                    accounting,
                    context_circuit=context_circuit,
                )
                maybe_abort_after("codex_escalation")
            if context_circuit.tripped:
                handoff = build_handoff(execution, escalated)
                accounting.finalize_tokens()
                return {"routing": {
                    "mode": mode,
                    "route": route.route,
                    "reason": route.reason,
                    "executor_mode": final_executor_mode,
                    "local_preferred": route.local_preferred,
                    "probe": route.probe,
                    "selected_files": selected,
                }, "handoff": handoff}

    else:
        executor_name = "codex" if route.route == "codex_direct" else "command"
        if executor_name == "codex":
            accounting.codex_direct_calls += 1
        execution = _run_executor(executor_name, config, task, repo_path, selected, 1, accounting, context_circuit=context_circuit)
        maybe_abort_after("codex_direct" if executor_name == "codex" else "command_execution")
        final_executor_mode = route.executor_mode

    if context_circuit.tripped:
        handoff = build_handoff(execution, escalated)
        accounting.finalize_tokens()
        return {"routing": {
            "mode": mode,
            "route": route.route,
            "reason": route.reason,
            "executor_mode": final_executor_mode,
            "local_preferred": route.local_preferred,
            "probe": route.probe,
            "selected_files": selected,
        }, "handoff": handoff}

    accounting.finalize_tokens()

    test_output = _run_tests(
        repo_path,
        config,
        task.test_env,
        accounting=accounting,
        context_circuit=context_circuit,
    )
    if maybe_abort_after("tests"):
        handoff = build_handoff(execution, escalated, test_output)
        accounting.finalize_tokens()
        return {"routing": {
            "mode": mode,
            "route": route.route,
            "reason": route.reason,
            "executor_mode": final_executor_mode,
            "local_preferred": route.local_preferred,
            "probe": route.probe,
            "selected_files": selected,
        }, "handoff": handoff}

    diff = _run_git_diff(repo_path, context_circuit=context_circuit)
    if maybe_abort_after("git_diff"):
        handoff = build_handoff(execution, escalated, test_output, diff)
        accounting.finalize_tokens()
        return {"routing": {
            "mode": mode,
            "route": route.route,
            "reason": route.reason,
            "executor_mode": final_executor_mode,
            "local_preferred": route.local_preferred,
            "probe": route.probe,
            "selected_files": selected,
        }, "handoff": handoff}

    accounting.git_diff_bytes = diff.get("diff_raw_bytes", diff.get("diff_bytes", 0))
    # keep diff bytes separate for dedicated risk signal and handoff visibility

    handoff = build_handoff(execution, escalated, test_output, diff)
    accounting.finalize_tokens()
    return {"routing": {
        "mode": mode,
        "route": route.route,
        "reason": route.reason,
        "executor_mode": final_executor_mode,
        "local_preferred": route.local_preferred,
        "probe": route.probe,
        "selected_files": selected,
    }, "handoff": handoff}


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Token-aware Orchestrator")
    parser.add_argument("--task", required=True, help="Task text or @json_file or JSON payload")
    parser.add_argument("--repo", required=True, help="git repository path")
    parser.add_argument("--config", default=None, help="config yaml path")
    parser.add_argument("--mode", choices=["baseline", "orchestrated"], default="orchestrated")
    parser.add_argument("--mock-local", action="store_true", help="force local worker mock mode")
    parser.add_argument("--output", default=None, help="write the complete routing and handoff payload to a JSON file")
    parser.add_argument("--print-routing", action="store_true", default=True)
    parser.add_argument("--print-handoff", action="store_true", default=True)
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        payload = orchestrate(args.task, args.repo, args.config, args.mock_local, mode=args.mode)
    except Exception as exc:
        _print_json({"error": str(exc), "mode": args.mode, "task": args.task})
        return 1

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if args.print_routing:
        _print_json({"kind": "routing", "payload": payload["routing"]})
    if args.print_handoff:
        _print_json({"kind": "handoff", "payload": payload["handoff"]})

    final_status = payload["handoff"].get("final", {}).get("status")
    return 0 if final_status in {"success", "review_required"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
