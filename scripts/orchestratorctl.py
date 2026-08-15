#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import time


ROOT = Path(__file__).resolve().parents[1]
PROJECT_TITLE = "token-aware-orchestrator"
VERSION = "0.4.0-mvp"
STATE_FILE = ROOT / ".v0.4-install-state.json"

DEFAULT_INSTALL_BIN_NAME = PROJECT_TITLE
DEFAULT_INSTALL_BIN_DIR = Path.home() / ".local" / "bin"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / PROJECT_TITLE
DEFAULT_CONFIG_NAME = "config.yaml"
INSTALL_TEMPLATE = ROOT / "config.default.yaml"

LOCAL_RESULTS_CANDIDATES = (
    "outputs/benchmark-run3-results.json",
    "outputs/benchmark-results.json",
    "outputs/ablation-absolute-results.json",
    "outputs/ablation-results.json",
)
ARCHIVE_RESULTS_CANDIDATES = (
    "benchmark-run3-results.json",
    "benchmark-results.json",
    "ablation-absolute-results.json",
    "ablation-results.json",
)
HISTORIC_ROOT = Path.home() / ".codex" / "skills" / "token-aware-orchestrator"


@dataclass
class CheckItem:
    name: str
    status: str  # pass | warn | fail
    detail: str
    critical: bool = False


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _to_bool_icon(value: bool) -> str:
    return "PASS" if value else "WARN"


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _read_json_text(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except Exception:
        return None


def _run_json_command(cmd: Sequence[str], timeout: int = 10) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _first_match(path_patterns: Iterable[Path]) -> Optional[Path]:
    for p in path_patterns:
        if p.exists():
            return p
    return None


def _is_executable_file(path: Path) -> bool:
    return path.exists() and os.access(str(path), os.X_OK)


def _in_path(bin_dir: Path) -> bool:
    env_path = os.environ.get("PATH", "")
    return str(bin_dir) in env_path.split(":")


def _shell_name() -> str:
    return Path(os.environ.get("SHELL", "")).name.lower()


def _shell_profile() -> Optional[Path]:
    name = _shell_name()
    if "zsh" in name:
        return Path.home() / ".zshrc"
    if "fish" in name:
        return Path.home() / ".config" / "fish" / "config.fish"
    if "bash" in name:
        return Path.home() / ".bashrc"
    return None


def _detect_codex_skill_dir() -> Tuple[Optional[Path], bool]:
    candidates: List[Path] = []

    cwd = Path.cwd()
    candidates.append(cwd)
    candidates.append(ROOT)

    skill_root = Path.home() / ".codex" / "skills"
    if skill_root.exists():
        try:
            candidates.extend([p for p in skill_root.iterdir() if p.is_dir() and "token-aware-orchestrator" in p.name.lower()])
        except Exception:
            pass

    for candidate in candidates:
        if not candidate:
            continue
        has_skill = (candidate / "SKILL.md").exists()
        has_runtime = (candidate / "scripts" / "orchestrator.py").exists()
        if has_skill and has_runtime:
            return candidate, True
    for candidate in candidates:
        if not candidate:
            continue
        if (candidate / "scripts" / "orchestrator.py").exists():
            return candidate, False
    return None, False


def _default_config_path(path_arg: str = "") -> Path:
    if path_arg:
        p = Path(path_arg).expanduser()
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        return p
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            path_text = raw.get("config_path")
            if isinstance(path_text, str) and path_text:
                return Path(path_text).expanduser()
        except Exception:
            pass
    return (DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_NAME).expanduser()


def _config_feature_snapshot(path: Optional[Path]) -> Dict[str, Any]:
    text = ""
    if path and path.exists():
        try:
            text = path.read_text(encoding="utf-8").lower()
        except Exception:
            text = ""

    return {
        "present": bool(path and path.exists()),
        "localization": "context:" in text and "max_after_bytes" in text,
        "context_guard": "context_circuit:" in text and "enabled:" in text,
        "context_guard_enabled": "context_circuit:" in text and "enabled: true" in text,
        "thread_guard": "thread_budget" in (_safe_file_text(ROOT / "scripts/benchmark.py").lower() if (ROOT / "scripts/benchmark.py").exists() else ""),
        "aider_declared": "aider:" in text,
        "local_worker_declared": "local_worker:" in text,
        "local_model": re.search(r"model:\s*.+", text) is not None,
    }


def _safe_file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    return default


def _format_bytes(value: Any) -> str:
    try:
        num = float(value)
    except Exception:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024
        idx += 1
    return f"{num:.1f} {units[idx]}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _detect_resource_status(host: str, model: str) -> Tuple[Dict[str, Any], bool]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "detect_resources.py"),
        "--json",
        "--host",
        host,
        "--model",
        model,
    ]
    out = _run_json_command(cmd, timeout=8)
    if not out.get("ok"):
        return {"reachable": False, "reason": out.get("stderr", "")}, False
    parsed = _read_json_text(out.get("stdout", ""))
    if not isinstance(parsed, dict):
        return {"reachable": False, "reason": "unparseable output"}, False
    return parsed, True


def _required_files() -> Dict[str, str]:
    return {
        "scripts/orchestrator.py": "Core orchestrator entry (runtime)",
        "scripts/benchmark.py": "Benchmark CLI and metrics schema",
        "scripts/detect_resources.py": "Status resource checker",
        "scripts/local_worker_probe.py": "Local worker probe helper",
        "scripts/orchestrate": "Runtime wrapper",
        "config.example.yaml": "Reference config",
        "config.default.yaml": "Install config template",
        "README.md": "Product docs",
    }


def _to_status_icon(status: str) -> str:
    if status == "pass":
        return "[PASS]"
    if status == "warn":
        return "[WARN]"
    return "[FAIL]"


def _human_summary_for_checks(checks: Sequence[CheckItem]) -> List[str]:
    lines: List[str] = []
    for check in checks:
        lines.append(f"{_to_status_icon(check.status)} {check.name}: {check.detail}")
    return lines


def _status_from_checks(checks: Sequence[CheckItem]) -> str:
    any_fail = any(item.status == "fail" for item in checks)
    if any_fail:
        return "ERROR"
    any_warn = any(item.status == "warn" for item in checks)
    return "WARNING" if any_warn else "READY"


def _resolve_results_path(explicit: str = "") -> Optional[Path]:
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if explicit_path.exists():
            return explicit_path
        return None

    local = [ROOT / path for path in LOCAL_RESULTS_CANDIDATES]
    found = _first_match(local)
    if found:
        return found

    legacy = HISTORIC_ROOT / "outputs"
    if legacy.exists():
        found = _first_match([legacy / path for path in ARCHIVE_RESULTS_CANDIDATES])
        if found:
            return found
    return None


def _extract_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("results is not a JSON object")
    if isinstance(payload.get("summary"), dict) and payload["summary"]:
        summary = payload["summary"]
        if isinstance(summary, dict):
            has_mode_metrics = (
                isinstance(summary.get("runs_by_mode"), dict)
                or any(isinstance(summary.get(k), dict) for k in ("baseline", "orchestrated", "full", "lean", "lean_core", "minimal_core"))
            )
            if has_mode_metrics:
                return summary
    if isinstance(payload.get("runs_by_mode"), dict):
        return payload
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("results payload has no summary and no records")

    by_mode: Dict[str, List[Dict[str, Any]]] = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        mode = str(row.get("mode", "unknown")).lower()
        by_mode.setdefault(mode, []).append(row)

    summary_by_mode: Dict[str, Any] = {}
    for mode, rows in by_mode.items():
        n = len(rows)
        task_success = sum(1 for r in rows if bool(r.get("task_success")))
        tests_ok = sum(1 for r in rows if bool(r.get("test_pass")))
        escalations = sum(1 for r in rows if _safe_int(r.get("escalation_count")) > 0)
        unexpected = sum(1 for r in rows if _safe_int(r.get("unexpected_files_changed")) > 0)
        local_calls = sum(_safe_int(r.get("local_worker_calls")) for r in rows)
        codex_calls = sum(_safe_int(r.get("codex_direct_calls")) for r in rows)
        candidate_total = 0
        after_total = 0
        token_in = 0
        token_out = 0
        exec_ms = 0.0
        for r in rows:
            candidate_total += _safe_int(r.get("candidate_context_bytes")) or _safe_int(r.get("bytes_read_proxy"))
            after_total += _safe_int(r.get("actual_codex_context_bytes"))
            token_in += _safe_int(r.get("estimated_input_tokens"))
            token_out += _safe_int(r.get("estimated_output_tokens"))
            exec_ms += _safe_float(r.get("execution_time_ms"))

        calls = local_calls + codex_calls
        if candidate_total <= 0:
            context_ratio = 0.0
        else:
            context_ratio = max(0.0, min(1.0, 1.0 - (after_total / float(candidate_total))))
        if token_in <= 0:
            token_ratio = 0.0
        else:
            token_ratio = max(0.0, min(1.0, 1.0 - (token_out / float(token_in))))

        summary_by_mode[mode] = {
            "count": n,
            "task_success": task_success,
            "task_success_rate": task_success / n if n else 0.0,
            "tests_pass": tests_ok,
            "test_pass_rate": tests_ok / n if n else 0.0,
            "context_reduction_ratio": context_ratio,
            "estimated_token_reduction_ratio": token_ratio,
            "context_before_bytes_total": candidate_total,
            "context_after_bytes_total": after_total,
            "estimated_context_tokens_before_total": token_in,
            "estimated_context_tokens_after_total": token_out,
            "unexpected_change_rate": unexpected / n if n else 0.0,
            "local_execution_ratio": local_calls / calls if calls else 0.0,
            "codex_execution_ratio": codex_calls / calls if calls else 1.0,
            "avg_execution_time_ms": exec_ms / n if n else 0.0,
            "escalation_rate": escalations / n if n else 0.0,
            "token_count_modes": sorted({str(r.get("token_count_mode", "")) for r in rows if r.get("token_count_mode")}),
        }

    return {
        "runs_by_mode": summary_by_mode,
        "total_runs": len(records),
        "task_count": len({r.get("task_id") for r in records if isinstance(r, dict) and r.get("task_id")}),
        "token_count_coverage": sorted({str(r.get("token_count_mode")) for r in records if isinstance(r, dict) and r.get("token_count_mode")}),
    }


def _extract_mode_metrics(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(summary, dict):
        return {}

    modes = summary.get("runs_by_mode")
    if isinstance(modes, dict) and modes:
        return {str(k): v for k, v in modes.items() if isinstance(v, dict)}

    direct_candidates = {}
    for key in ("baseline", "full", "orchestrated", "lean", "lean_core", "minimal_core", "minimal"):
        value = summary.get(key)
        if isinstance(value, dict):
            direct_candidates[key] = value
    if direct_candidates:
        return direct_candidates

    modes_field = summary.get("modes")
    if isinstance(modes_field, dict):
        candidates = {str(k): v for k, v in modes_field.items() if isinstance(v, dict)}
        if candidates:
            return candidates

    return {}


def _resolve_mode_modes(summary: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    modes = _extract_mode_metrics(summary)
    if not modes:
        available = sorted([k for k in summary.keys()]) if isinstance(summary, dict) else []
        raise ValueError(f"summary has no mode metrics, keys={available}")

    if not isinstance(modes, dict):
        raise ValueError("summary has invalid mode metrics")

    if not modes:
        raise ValueError("summary has no mode metrics")

    baseline = modes.get("baseline") or modes.get("full") or {}
    orchestrated = modes.get("orchestrated") or modes.get("full") or {}
    if not baseline and orchestrated:
        baseline = orchestrated
    return modes, baseline, orchestrated


def command_install(args: argparse.Namespace) -> int:
    root = ROOT
    checks: List[CheckItem] = []

    required = _required_files()
    for rel in required:
        path = root / rel
        checks.append(CheckItem(rel, "pass" if path.exists() else "fail", f"found={path}"))
    codex_dir, codex_confirmed = _detect_codex_skill_dir()

    bin_dir = Path(args.bin_dir).expanduser()
    config_dir = Path(args.config_dir).expanduser()
    config_path = (config_dir / DEFAULT_CONFIG_NAME).resolve()
    config_template = INSTALL_TEMPLATE

    if not config_template.exists():
        checks.append(CheckItem("config.template", "fail", "template not found", True))
    else:
        checks.append(CheckItem("config.template", "pass", f"found={config_template}"))
        config_dir.mkdir(parents=True, exist_ok=True)
        if args.force or not config_path.exists():
            shutil.copy2(config_template, config_path)
            checks.append(CheckItem("config.path", "pass", f"written={config_path}"))
        else:
            checks.append(CheckItem("config.path", "warn", f"already exists={config_path}"))

    bin_dir.mkdir(parents=True, exist_ok=True)
    shim_path = bin_dir / args.bin_name
    shim_script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'ROOT="{root}"\n'
        'exec python3 "$ROOT/scripts/orchestratorctl.py" "$@"\n'
    )
    shim_path.write_text(shim_script, encoding="utf-8")
    shim_path.chmod(0o755)
    checks.append(CheckItem("bin.install", "pass", f"wrote={shim_path}"))

    profile = _shell_profile()
    profile_label = str(profile) if profile else "auto-detect-failed"
    shell = _shell_name() or "unknown"
    in_path = _in_path(bin_dir)
    checks.append(CheckItem("PATH", "pass" if in_path else "warn", f"{bin_dir} in PATH={in_path}", False))

    local_feature = _config_feature_snapshot(config_path)
    resource_data, resource_ok = _detect_resource_status(args.host, args.model)
    local_available = bool(resource_ok and isinstance(resource_data, dict) and resource_data.get("ollama", {}).get("model_available", False))

    optional_checks = [
        CheckItem("local_worker.available", "pass" if local_available else "warn", "local model ready" if local_available else "local model unavailable"),
        CheckItem("core.localization", "pass" if local_feature["localization"] else "warn", "context section present" if local_feature["localization"] else "context section missing"),
        CheckItem("core.context_guard", "pass" if local_feature["context_guard_enabled"] else "warn", "context guard enabled" if local_feature["context_guard_enabled"] else "context guard disabled"),
    ]

    state = {
        "installed_at": _utc_now(),
        "version": VERSION,
        "project_root": str(root),
        "project_skill_dir": str(codex_dir) if codex_dir else str(root),
        "profile": profile_label,
        "shell": shell,
        "bin": str(shim_path),
        "config_path": str(config_path),
        "in_path": in_path,
        "checks": [item.__dict__ for item in checks + optional_checks],
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        checks_payload = {
            "status": _status_from_checks(checks),
            "checks": checks + optional_checks,
            "state": state,
        }
        _print_json(
            {
                "status": checks_payload["status"],
                "ready": [item.__dict__ for item in checks + optional_checks],
                "state": state,
            }
        )
        return 1 if checks_payload["status"] == "ERROR" else 0

    print("[token-aware-orchestrator] install completed")
    print(f"ready_status={_status_from_checks(checks)}")
    print(f"skill_root={codex_dir or root}")
    print(f"profile={profile_label}")
    print()
    print("READY:")
    print(f"  {_to_status_icon('pass' if bool(codex_dir) else 'warn')} Codex detected ({codex_dir or 'not detected'})")
    print(f"  {_to_status_icon('pass')} Skill installed ({shim_path})")
    print(f"  {_to_status_icon('pass' if optional_checks[1].status == 'pass' else 'warn')} Context Guard enabled")
    print(f"  {_to_status_icon('pass' if _config_feature_snapshot(config_path).get('thread_guard') else 'warn')} Thread Guard enabled")

    print()
    print("OPTIONAL:")
    print(f"  {_to_status_icon(optional_checks[0].status)} Local Worker available ({args.model})")
    print(f"  {_to_status_icon(optional_checks[0].status)} fallback to Codex Direct ({'none' if local_available else 'enabled'})")

    if not in_path:
        print()
        print("PATH hint:")
        print(f"  export PATH=\"{bin_dir}:$PATH\"")
        if profile:
            print(f"  # add to {profile}")
        print(f"  source {profile if profile else '$PROFILE'}")
    return 1 if _status_from_checks(checks) == "ERROR" else 0


def command_status(args: argparse.Namespace) -> int:
    root = ROOT
    config_path = _default_config_path(args.config)
    codex_dir, _ = _detect_codex_skill_dir()
    config_snapshot = _config_feature_snapshot(config_path)

    system_checks = [
        CheckItem("Codex", "pass" if codex_dir else "warn", f"path={codex_dir or 'not detected'}", False),
        CheckItem("Skill", "pass" if root.exists() else "fail", f"path={root}", True),
        CheckItem("Config", "pass" if config_snapshot["present"] else "warn", f"path={config_path}", False),
    ]
    core_checks = [
        CheckItem("Localization", "pass" if config_snapshot.get("localization") else "warn", "context-aware candidate selection" if config_snapshot.get("localization") else "context config missing", False),
        CheckItem("Context Guard", "pass" if config_snapshot.get("context_guard_enabled") else "warn", "enabled" if config_snapshot.get("context_guard_enabled") else "disabled", False),
        CheckItem("Thread Guard", "pass" if bool(config_snapshot.get("thread_guard")) else "warn", "thread budget instrumentation", False),
        CheckItem("State", "pass" if STATE_FILE.exists() else "warn", "state file exists" if STATE_FILE.exists() else "state file missing", False),
    ]

    resource, resource_ok = _detect_resource_status(args.host, args.model)
    ollama_info = resource.get("ollama", {}) if isinstance(resource, dict) else {}
    optional_checks = [
        CheckItem("Ollama", "pass" if resource_ok and ollama_info.get("reachable") else "warn", f"host={args.host}", False),
        CheckItem("Aider", "pass" if shutil.which("aider") else "warn", f"binary={shutil.which('aider') or 'not found'}", False),
        CheckItem(
            "Local model",
            "pass" if bool(ollama_info.get("model_available")) else "warn",
            f"model={args.model} available={bool(ollama_info.get('model_available'))}",
            False,
        ),
    ]

    checks = system_checks + core_checks + optional_checks
    status = _status_from_checks(checks)

    if args.json:
        _print_json(
            {
                "status": status,
                "profile": str(_shell_profile() or "unknown"),
                "shell": _shell_name() or "unknown",
                "timestamp": _utc_now(),
                "version": VERSION,
                "config_path": str(config_path),
                "resource": resource,
                "system": {
                    "Codex": {item.name.lower(): item.__dict__ for item in system_checks},
                },
                "core": {item.name.lower(): item.__dict__ for item in core_checks},
                "optional": {item.name.lower(): item.__dict__ for item in optional_checks},
            }
        )
        return 1 if status == "ERROR" else 0

    print(f"System Status: {status}")
    print(f"Profile: {_shell_profile() or 'unknown'}")
    print(f"Shell: {_shell_name() or 'unknown'}")
    print(f"Project: {root}")
    print("System:")
    for item in system_checks:
        print(f"  {_to_status_icon(item.status)} {item.name}: {item.detail}")
    print("Core:")
    for item in core_checks:
        print(f"  {_to_status_icon(item.status)} {item.name}: {item.detail}")
    print("Optional:")
    for item in optional_checks:
        print(f"  {_to_status_icon(item.status)} {item.name}: {item.detail}")

    if optional_checks[-1].status == "warn":
        print("  fallback: Codex Direct")

    return 1 if status == "ERROR" else 0


def _load_report_payload(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = _extract_summary(payload)
    modes = _extract_mode_metrics(summary)
    if not modes:
        raise ValueError("results summary has no mode metrics")
    token_modes = (
        summary.get("token_count_coverage")
        or payload.get("token_count_coverage")
        or summary.get("token_count_modes")
        or []
    )
    return {
        "path": str(path),
        "timestamp": payload.get("timestamp") or _utc_now(),
        "task_count": payload.get("task_count", summary.get("task_count", 0)),
        "modes": modes,
        "token_count_mode": token_modes,
    }


def command_report(args: argparse.Namespace) -> int:
    results_path = _resolve_results_path(args.results.strip() if args.results else "")
    if not results_path:
        msg = {
            "status": "missing-results",
            "message": "requested results file not found" if args.results else "no local results found",
        }
        if args.results:
            msg["requested"] = str(Path(args.results).expanduser())
        else:
            msg["searched"] = [str(ROOT / path) for path in LOCAL_RESULTS_CANDIDATES] + [
                str(HISTORIC_ROOT / "outputs" / path) for path in ARCHIVE_RESULTS_CANDIDATES
            ]
        if args.json:
            _print_json(msg)
        else:
            print(json.dumps(msg, ensure_ascii=False, indent=2))
        return 1

    data = _load_report_payload(results_path)
    modes, baseline_mode, orchestrated_mode = _resolve_mode_modes(data["modes"])

    task_count = int(data["task_count"])

    base_before = _safe_int(baseline_mode.get("context_before_bytes_total"))
    orch_before = _safe_int(orchestrated_mode.get("context_before_bytes_total"))
    base_after = _safe_int(baseline_mode.get("context_after_bytes_total"))
    orch_after = _safe_int(orchestrated_mode.get("context_after_bytes_total"))
    before_bytes = base_before or orch_before or 0
    after_bytes = orch_after if orchestrated_mode else base_after or 0

    baseline_ratio = _safe_float(baseline_mode.get("context_reduction_ratio"))
    orch_ratio = _safe_float(orchestrated_mode.get("context_reduction_ratio")) if orchestrated_mode else baseline_ratio
    token_ratio = _safe_float(orchestrated_mode.get("estimated_token_reduction_ratio")) if orchestrated_mode else 0.0
    unexpected_rate = _safe_float(orchestrated_mode.get("unexpected_change_rate") if orchestrated_mode else baseline_mode.get("unexpected_change_rate"))

    quality_mode = orchestrated_mode or baseline_mode or {}
    success = _safe_int(quality_mode.get("task_success"))
    tests = _safe_int(quality_mode.get("tests_pass"))
    unexpected_changes = int(round(_safe_float(unexpected_rate, 0.0) * max(task_count, 1)))

    config_snapshot = _config_feature_snapshot(_default_config_path(args.config) if hasattr(args, "config") else None)
    resource, ok = _detect_resource_status(args.host, args.model)
    local_model_available = bool(ok and isinstance(resource, dict) and resource.get("ollama", {}).get("model_available", False))
    fallback = "none" if local_model_available else "Codex Direct"

    lines: List[str] = [
        "# AI Efficiency Report",
        "",
        f"- generated: {data['timestamp']}",
        f"- results: {results_path}",
        f"- token_data: {', '.join(data['token_count_mode']) if data['token_count_mode'] else 'unknown'}",
        "",
        "## AI Efficiency Report",
        "",
        "### Tasks",
        f"{task_count}",
        "",
        "### Context",
        f"- Before: {_format_bytes(before_bytes)}",
        f"- After: {_format_bytes(after_bytes)}",
        f"- Reduction: {_pct(orch_ratio)}",
        "",
        "### Quality",
        f"- Success: {success}/{task_count}",
        f"- Tests: {tests}/{task_count}",
        f"- Unexpected changes: {unexpected_changes}",
        "",
        "### Execution",
        f"- Localization: {'enabled' if config_snapshot.get('localization') else 'disabled'}",
        f"- Local Worker: {'enabled' if local_model_available else 'disabled'}",
        f"- Fallback: {fallback}",
        "",
    ]

    if baseline_mode and orchestrated_mode:
        lines += [
            "### Baseline -> Orchestrated",
            f"- context_reduction: {_pct(orch_ratio if base_before else 0.0)}",
            f"- token_reduction: {_pct(token_ratio)}",
            f"- context_before_bytes: {base_before}",
            f"- context_after_bytes: {orch_after}",
            "",
        ]

    lines += [
        "| mode | count | success | tests | context_reduction | token_reduction | local_ratio | codex_ratio | avg_ms | unexpected_change |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode, row in modes.items():
        lines.append(
            "| {mode} | {count} | {success} | {tests} | {cr} | {tr} | {local} | {codex} | {ms} | {uc} |".format(
                mode=mode,
                count=_safe_int(row.get("count")),
                success=_pct(_safe_float(row.get("task_success_rate", 0.0))),
                tests=_pct(_safe_float(row.get("test_pass_rate", 0.0))),
                cr=_pct(_safe_float(row.get("context_reduction_ratio", 0.0))),
                tr=_pct(_safe_float(row.get("estimated_token_reduction_ratio", 0.0))),
                local=_pct(_safe_float(row.get("local_execution_ratio", 0.0))),
                codex=_pct(_safe_float(row.get("codex_execution_ratio", 0.0))),
                ms=round(_safe_float(row.get("avg_execution_time_ms", 0.0), 0.0), 3),
                uc=_pct(_safe_float(row.get("unexpected_change_rate", 0.0))),
            )
        )

    markdown = "\n".join(lines)
    report = {
        "status": "ok",
        "version": VERSION,
        "results_path": str(results_path),
        "task_count": task_count,
        "modes": {name: {
            "count": _safe_int(row.get("count")),
            "success": _pct(_safe_float(row.get("task_success_rate", 0.0))),
            "tests": _pct(_safe_float(row.get("test_pass_rate", 0.0))),
            "context_reduction": _pct(_safe_float(row.get("context_reduction_ratio", 0.0))),
            "token_reduction": _pct(_safe_float(row.get("estimated_token_reduction_ratio", 0.0))),
            "local_ratio": _pct(_safe_float(row.get("local_execution_ratio", 0.0))),
            "codex_ratio": _pct(_safe_float(row.get("codex_execution_ratio", 0.0))),
            "avg_ms": round(_safe_float(row.get("avg_execution_time_ms", 0.0), 0.0), 3),
            "unexpected_change_rate": _pct(_safe_float(row.get("unexpected_change_rate", 0.0))),
        } for name, row in modes.items()},
        "context": {
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "context_reduction": _pct(orch_ratio),
            "token_reduction": _pct(token_ratio),
        },
        "quality": {
            "success": f"{success}/{task_count}",
            "tests": f"{tests}/{task_count}",
            "unexpected_changes": unexpected_changes,
        },
        "execution": {
            "localization": config_snapshot.get("localization"),
            "local_worker": local_model_available,
            "fallback": fallback,
        },
        "markdown": markdown,
        "resource": resource if isinstance(resource, dict) else {},
    }

    if baseline_mode and orchestrated_mode:
        report["baseline_to_orchestrated"] = {
            "context_before_bytes": base_before,
            "context_after_bytes": orch_after,
            "context_reduction_vs_baseline": _pct(orch_ratio),
            "token_reduction_vs_baseline": _pct(token_ratio),
        }
        report["context"]["before_bytes"] = base_before
        report["context"]["after_bytes"] = orch_after

    if args.output:
        out = Path(args.output).resolve()
        out.write_text(markdown, encoding="utf-8")
        report["output_saved"] = str(out)

    if args.json:
        _print_json(report)
    else:
        print(markdown)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROJECT_TITLE,
        description="Token-aware Orchestrator CLI (v0.4 MVP): install / status / report",
    )
    parser.add_argument("--version", action="version", version=f"{PROJECT_TITLE} {VERSION}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_install = subparsers.add_parser("install", help="Install CLI entry and write lean config")
    p_install.add_argument("--bin-dir", default=str(DEFAULT_INSTALL_BIN_DIR), help="target bin directory")
    p_install.add_argument("--bin-name", default=DEFAULT_INSTALL_BIN_NAME, help="binary name")
    p_install.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR), help="config directory")
    p_install.add_argument("--host", default="http://127.0.0.1:11434", help="local model host")
    p_install.add_argument("--model", default="qwen2.5-coder:7b", help="local model name")
    p_install.add_argument("--force", action="store_true", help="overwrite existing config")
    p_install.add_argument("--json", action="store_true", help="emit JSON")
    p_install.set_defaults(func=command_install)

    p_status = subparsers.add_parser("status", help="Check installation and runtime health")
    p_status.add_argument("--config", default="", help="config path")
    p_status.add_argument("--host", default="http://127.0.0.1:11434", help="ollama host")
    p_status.add_argument("--model", default="qwen2.5-coder:7b", help="local model name")
    p_status.add_argument("--json", action="store_true", help="emit JSON")
    p_status.set_defaults(func=command_status)

    p_report = subparsers.add_parser("report", help="Generate readability-first efficiency report")
    p_report.add_argument("--results", default="", help="benchmark-results json path")
    p_report.add_argument("--output", default="", help="write markdown report")
    p_report.add_argument("--host", default="http://127.0.0.1:11434", help="local model host")
    p_report.add_argument("--model", default="qwen2.5-coder:7b", help="local model name")
    p_report.add_argument("--config", default="", help="config path used for feature labels")
    p_report.add_argument("--json", action="store_true", help="emit JSON only")
    p_report.set_defaults(func=command_report)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
