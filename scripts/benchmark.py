#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs"


@dataclass
class BenchmarkTask:
    task_id: str
    category: str
    payload: Dict[str, Any]


@dataclass
class BenchmarkMode:
    key: str
    label: str
    orchestrator_mode: str
    description: str
    mock_local: bool
    config_overrides: Dict[str, Any]
    strip_scope_files: bool = False
    worker_scope: str = "CONFIG"


@dataclass
class ThreadBudgetLimits:
    tool_calls: int = 16
    tool_output_bytes: int = 180_000
    files_read: int = 80
    file_bytes_read: int = 240_000
    file_lines_read: int = 24_000
    command_log_bytes: int = 120_000
    diff_bytes: int = 240_000
    stage_count: int = 8
    handoff_estimated_tokens: int = 500
    handoff_estimated_tokens_hard: int = 800
    source: str = "proxy"


@dataclass
class ThreadBudgetState:
    limits: ThreadBudgetLimits = field(default_factory=ThreadBudgetLimits)
    tool_calls: int = 0
    tool_output_bytes: int = 0
    files_read: int = 0
    file_bytes_read: int = 0
    file_lines_read: int = 0
    command_log_bytes: int = 0
    diff_bytes: int = 0
    stage_count: int = 0
    handoff_estimated_tokens: int = 0
    last_stage: Optional[str] = None
    last_task: Optional[str] = None
    last_mode: Optional[str] = None

    def add_stage(self, mode: str, task_id: str, run_payload: Dict[str, Any]) -> Tuple[bool, str]:
        self.last_mode = mode
        self.last_task = task_id
        self.last_stage = f"{mode}::{task_id}"
        self.stage_count += 1
        self.tool_calls += 1
        self.tool_output_bytes += _safe_int(run_payload.get("tool_output_bytes"))
        self.files_read += _safe_int(run_payload.get("files_actually_read"))
        self.file_bytes_read += _safe_int(run_payload.get("bytes_read_proxy", run_payload.get("candidate_context_bytes")))
        self.file_lines_read += _safe_int(run_payload.get("code_lines_actually_read"))
        self.command_log_bytes += _safe_int(run_payload.get("command_output_bytes"))
        self.diff_bytes += _safe_int(run_payload.get("diff_bytes"))
        stage_handoff_tokens = _safe_int(run_payload.get("handoff_estimated_tokens"))
        if stage_handoff_tokens > self.handoff_estimated_tokens:
            self.handoff_estimated_tokens = stage_handoff_tokens
        return self._is_exceeded()

    def _is_exceeded(self) -> Tuple[bool, str]:
        if self.tool_calls > self.limits.tool_calls:
            return True, "tool_calls"
        if self.tool_output_bytes > self.limits.tool_output_bytes:
            return True, "tool_output_bytes"
        if self.files_read > self.limits.files_read:
            return True, "files_read"
        if self.file_bytes_read > self.limits.file_bytes_read:
            return True, "file_bytes_read"
        if self.file_lines_read > self.limits.file_lines_read:
            return True, "file_lines_read"
        if self.command_log_bytes > self.limits.command_log_bytes:
            return True, "command_log_bytes"
        if self.diff_bytes > self.limits.diff_bytes:
            return True, "diff_bytes"
        if self.stage_count > self.limits.stage_count:
            return True, "stage_count"
        if self.handoff_estimated_tokens > self.limits.handoff_estimated_tokens:
            return True, "handoff_estimated_tokens"
        return False, ""

    def as_proxy_report(self) -> Dict[str, Any]:
        return {
            "source": self.limits.source,
            "soft_limits": {
                "tool_calls": self.limits.tool_calls,
                "tool_output_bytes": self.limits.tool_output_bytes,
                "files_read": self.limits.files_read,
                "file_bytes_read": self.limits.file_bytes_read,
                "file_lines_read": self.limits.file_lines_read,
                "command_log_bytes": self.limits.command_log_bytes,
                "diff_bytes": self.limits.diff_bytes,
                "stage_count": self.limits.stage_count,
                "handoff_estimated_tokens": self.limits.handoff_estimated_tokens,
            },
            "soft_progress": {
                "tool_calls": self.tool_calls,
                "tool_output_bytes": self.tool_output_bytes,
                "files_read": self.files_read,
                "file_bytes_read": self.file_bytes_read,
                "file_lines_read": self.file_lines_read,
                "command_log_bytes": self.command_log_bytes,
                "diff_bytes": self.diff_bytes,
                "stage_count": self.stage_count,
                "handoff_estimated_tokens": self.handoff_estimated_tokens,
            },
            "last_stage": self.last_stage,
        }


def _safe_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")



def _parse_ablation_modes(raw: str) -> List[str]:
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _ablation_mode_catalog() -> Dict[str, BenchmarkMode]:
    return {
        "BASELINE": BenchmarkMode(
            key="BASELINE",
            label="baseline_control",
            orchestrator_mode="baseline",
            description="基线，不走本地worker",
            mock_local=False,
            config_overrides={},
        ),
        "FULL": BenchmarkMode(
            key="FULL",
            label="full_orchestrated",
            orchestrator_mode="orchestrated",
            description="完整orchestrated，开启本地worker与上下文裁剪",
            mock_local=False,
            config_overrides={},
        ),
        "NO_LOCAL_WORKER": BenchmarkMode(
            key="NO_LOCAL_WORKER",
            label="no_local_worker",
            orchestrator_mode="orchestrated",
            description="禁用本地worker调度",
            mock_local=False,
            config_overrides={"routing": {"prefer_local": False}},
        ),
        "NO_CONTEXT_FILTERING": BenchmarkMode(
            key="NO_CONTEXT_FILTERING",
            label="no_context_filtering",
            orchestrator_mode="orchestrated",
            description="关闭上下文risk检测以做对照",
            mock_local=False,
            config_overrides={"controls": {"context_circuit": {"enabled": False}}},
        ),
        "NO_PRECISE_LOCALIZATION": BenchmarkMode(
            key="NO_PRECISE_LOCALIZATION",
            label="no_precise_localization",
            orchestrator_mode="orchestrated",
            description="放宽定位范围到更多候选文件",
            mock_local=False,
            config_overrides={"context": {"max_selected_files": 60, "max_after_bytes": 200_000}},
        ),
        "MINIMAL_CORE": BenchmarkMode(
            key="MINIMAL_CORE",
            label="minimal_core",
            orchestrator_mode="orchestrated",
            description="最小核心路径，仅保留最短上下文候选",
            mock_local=False,
            config_overrides={
                "context": {"max_candidate_bytes": 40_000, "max_after_bytes": 10_000, "max_selected_files": 2},
                "controls": {"context_circuit": {"warning_token_bytes": 80_000, "hard_token_bytes": 160_000}},
            },
        ),
    }


def _merge_config_bases(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_config_bases(result[key], value)
        else:
            result[key] = value
    return result


def _parse_json_stream(raw: str) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    depth = 0
    start = None

    for idx, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
            continue
        if ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                fragment = raw[start : idx + 1]
                try:
                    data = json.loads(fragment)
                    objects.append(data)
                except Exception:
                    pass
                start = None
    return objects


def _safe_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) else default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _ratio(before: int, after: int) -> float:
    if before <= 0:
        return 0.0
    return round(1.0 - (after / float(before)), 6)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_yaml_like(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
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
        low = value.lower()
        if low in {"true", "yes"}:
            return True
        if low in {"false", "no"}:
            return False
        if low == "null":
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


def _dump_yaml(value: Any, indent: int = 0) -> str:
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(" " * indent + f"{key}:")
                dumped_child = _dump_yaml(item, indent + 2)
                if dumped_child:
                    lines.append(dumped_child)
            else:
                if isinstance(item, str):
                    encoded = json.dumps(item)
                elif isinstance(item, bool):
                    encoded = "true" if item else "false"
                elif item is None:
                    encoded = "null"
                else:
                    encoded = str(item)
                lines.append(" " * indent + f"{key}: {encoded}")
        return "\n".join(lines)

    if isinstance(value, list):
        if not value:
            return "".join([" " * indent, "[]"])
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(" " * indent + "-")
                child = _dump_yaml(item, indent + 2)
                if child:
                    lines.append(child)
            elif isinstance(item, str):
                lines.append(" " * indent + f"- {json.dumps(item)}")
            elif isinstance(item, bool):
                lines.append(" " * indent + f"- {'true' if item else 'false'}")
            elif item is None:
                lines.append(" " * indent + "- null")
            else:
                lines.append(" " * indent + f"- {item}")
        return "\n".join(lines)

    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _estimate_tokens(byte_count: int, chars_per_token: int = 4) -> int:
    if byte_count <= 0:
        return 0
    return max(1, math.ceil(byte_count / chars_per_token))


def _build_base_template(dst: Path) -> None:
    log_lines = list(range(1, 18000))
    files = {
        "pyproject.toml": "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
        "src/calculator.py": (
            "# Calculator utilities\n"
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n\n"
            "def safe_div(numerator: int, denominator: int) -> float:\n"
            "    return numerator / denominator\n"
        ),
        "src/math_utils.py": (
            "# Math helpers\n"
            "def label_level(level: int) -> str:\n"
            "    if level == 0:\n"
            "        return 'zero'\n"
            "    if level == 1:\n"
            "        return 'low'\n"
            "    return 'high'\n"
        ),
        "src/parser.py": (
            "# Simple parser helpers\n"
            "RETRY_LIMIT = 2\n\n"
            "def parse_retry_count(raw: str) -> int:\n"
            "    text = raw.strip()\n"
            "    return RETRY_LIMIT if text else 0\n"
        ),
        "src/formatter.py": (
            "# Output formatter\n"
            "RETRY_LIMIT = 2\n\n"
            "def decorate(message: str, level: int) -> str:\n"
            "    return f\"[{level}] {message}\"\n"
        ),
        "src/worker.py": (
            "# Background worker\n"
            "TIMEOUT_MS = 1000\n\n"
            "def run(timeout_ms: int = TIMEOUT_MS) -> bool:\n"
            "    return timeout_ms > 0\n"
        ),
        "src/config.py": "# Local config\nRETRY_STRATEGY = \"legacy\"\n",
        "src/notes.py": "# Notes utilities\nKNOWN_ERROR = None\n",
        "src/logger.py": (
            "# Log utilities\n"
            "def extract_first_error_line(log_path: str, needle: str = \"E-5001\") -> str:\n"
            "    with open(log_path, 'r', encoding='utf-8') as handle:\n"
            "        for idx, line in enumerate(handle, start=1):\n"
            "            if needle in line:\\n"
            "                return f'{idx}:{line.strip()}'\n"
            "    return 'not-found'\n"
        ),
        "logs/service.log": (
            "\n".join([f"INFO startup line {i}" for i in log_lines])
            + "\nWARN heartbeat E-5001 critical path mismatch\n"
            + "\n".join([f"INFO keep-alive {i}" for i in range(18000, 36000)])
            + "\n"
        ),
        "notes.md": "# Task notes\n",
        "tests/test_task_truth.py": (
            "import os\n"
            "import unittest\n"
            "from pathlib import Path\n\n"
            "def _read(path: str) -> str:\n"
            "    return Path(path).read_text(encoding='utf-8')\n\n"
            "class TestTaskTruth(unittest.TestCase):\n"
            "    def test_truth(self):\n"
            "        task_id = os.getenv('TOA_TASK_ID', '')\n"
            "        calc = _read('src/calculator.py')\n"
            "        tests = _read('tests/test_task.py')\n"
            "        if task_id == 'task_a_single_file':\n"
            "            self.assertIn('def add_numbers', calc)\n"
            "            self.assertNotIn('def add(', calc)\n"
            "            self.assertIn('def test_add_numbers', tests)\n"
            "            return\n"
            "        if task_id == 'task_b_bug_fix':\n"
            "            self.assertIn('if denominator == 0', calc)\n"
            "            self.assertIn('return 0.0', calc)\n"
            "            self.assertIn('test_safe_div_zero', tests)\n"
            "            return\n"
            "        if task_id == 'task_c_repeat':\n"
            "            self.assertIn('MAX_ATTEMPTS', _read('src/parser.py'))\n"
            "            self.assertIn('MAX_ATTEMPTS', _read('src/formatter.py'))\n"
            "            self.assertNotIn('RETRY_LIMIT =', _read('src/parser.py'))\n"
            "            self.assertNotIn('RETRY_LIMIT =', _read('src/formatter.py'))\n"
            "            return\n"
            "        if task_id == 'task_d_log_locate':\n"
            "            self.assertIn('E-5001', _read('notes.md'))\n"
            "            return\n"
            "        if task_id == 'task_e_locate_then_modify':\n"
            "            self.assertIn('RETRY_STRATEGY = \\\"modern\\\"', _read('src/config.py'))\n"
            "            self.assertIn('TIMEOUT_MS = 1500', _read('src/worker.py'))\n"
            "            return\n"
            "        raise AssertionError(f\"unknown TOA_TASK_ID={task_id}\")\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        ),
        "tests/test_task.py": "from src import calculator  # noqa: F401\n",
        "src/__init__.py": "# generated fixture\n",
        "tests/__init__.py": "# generated fixture\n",
    }

    for rel, content in files.items():
        _safe_write(dst / rel, content)


def initialize_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _build_base_template(path)
    for remove in [".git", ".venv", "node_modules"]:
        rm = path / remove
        if rm.exists():
            shutil.rmtree(rm)
    subprocess.run(["git", "init"], cwd=str(path), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "orchestrator@local"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "token-orchestrator"], cwd=str(path), check=True)
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "fixture init"], cwd=str(path), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def benchmark_tasks() -> List[BenchmarkTask]:
    def cmd_task_a() -> str:
        return (
            "python3 -c \"from pathlib import Path; p=Path('src/calculator.py'); t=p.read_text(encoding='utf-8'); "
            "t=t.replace('def add(a: int, b: int) -> int:\\\\n    return a + b', 'def add_numbers(a: int, b: int) -> int:\\\\n    return a + b'); "
            "p.write_text(t, encoding='utf-8'); p2=Path('tests/test_task.py'); c=p2.read_text(encoding='utf-8'); "
            "p2.write_text(c + '\\\\n\\\\ndef test_add_numbers():\\\\n    assert calculator.add_numbers(2, 3) == 5\\\\n', encoding='utf-8')\""
        )

    def cmd_task_b() -> str:
        return (
            "python3 -c \"from pathlib import Path; p=Path('src/calculator.py'); t=p.read_text(encoding='utf-8'); "
            "t=t.replace('def safe_div(numerator: int, denominator: int) -> float:\\\\n    return numerator / denominator', "
            "'def safe_div(numerator: int, denominator: int) -> float:\\\\n    if denominator == 0:\\\\n        return 0.0\\\\n    return numerator / denominator'); p.write_text(t, encoding='utf-8'); "
            "p2=Path('tests/test_task.py'); c=p2.read_text(encoding='utf-8'); "
            "p2.write_text(c + '\\\\n\\\\ndef test_safe_div_zero():\\\\n    assert calculator.safe_div(6, 0) == 0.0\\\\n', encoding='utf-8')\""
        )

    def cmd_task_c() -> str:
        return (
            "python3 -c \"from pathlib import Path; import pathlib; [pathlib.Path(rel).write_text(pathlib.Path(rel).read_text(encoding='utf-8').replace('RETRY_LIMIT', 'MAX_ATTEMPTS'), encoding='utf-8') for rel in ['src/parser.py', 'src/formatter.py']]\""
        )

    def cmd_task_d() -> str:
        return (
            "python3 -c \"from pathlib import Path; rows=Path('logs/service.log').read_text(encoding='utf-8').splitlines(); "
            "result=next((f'{i}:{row.strip()}' for i,row in enumerate(rows,start=1) if 'E-5001' in row), 'not-found'); "
            "Path('notes.md').write_text(f'First E-5001 line: {result}\\\\n', encoding='utf-8')\""
        )

    def cmd_task_e() -> str:
        return (
            "python3 -c \"from pathlib import Path; Path('src/config.py').write_text(Path('src/config.py').read_text(encoding='utf-8').replace('legacy', 'modern'), encoding='utf-8'); "
            "Path('src/worker.py').write_text(Path('src/worker.py').read_text(encoding='utf-8').replace('TIMEOUT_MS = 1000', 'TIMEOUT_MS = 1500'), encoding='utf-8')\""
        )

    return [
        BenchmarkTask(
            task_id="task_a_single_file",
            category="simple_single_file_modification",
            payload={
                "executor": "aider",
                "task": "将 src/calculator.py 的 add 函数重命名为 add_numbers，并更新 tests/test_task.py。",
                "scope_files": ["src/calculator.py"],
                "command": cmd_task_a(),
                "expected_files": ["src/calculator.py", "tests/test_task.py"],
                "test_env": {"TOA_TASK_ID": "task_a_single_file"},
            },
        ),
        BenchmarkTask(
            task_id="task_b_bug_fix",
            category="clear_bug_fix",
            payload={
                "executor": "aider",
                "task": "修复 safe_div 除零缺陷：denominator 为 0 时返回 0.0。",
                "command": cmd_task_b(),
                "expected_files": ["src/calculator.py", "tests/test_task.py"],
                "test_env": {"TOA_TASK_ID": "task_b_bug_fix"},
            },
        ),
        BenchmarkTask(
            task_id="task_c_repeat",
            category="multi_file_repeated_modification",
            payload={
                "executor": "aider",
                "task": "将 src/parser.py 与 src/formatter.py 中 RETRY_LIMIT 改为 MAX_ATTEMPTS。",
                "scope_files": ["src/parser.py", "src/formatter.py"],
                "command": cmd_task_c(),
                "expected_files": ["src/parser.py", "src/formatter.py"],
                "test_env": {"TOA_TASK_ID": "task_c_repeat"},
            },
        ),
        BenchmarkTask(
            task_id="task_d_log_locate",
            category="large_log_locate",
            payload={
                "executor": "aider",
                "task": "读取 logs/service.log，找到首个 E-5001 并记录到 notes.md。",
                "command": cmd_task_d(),
                "expected_files": ["notes.md"],
                "test_env": {"TOA_TASK_ID": "task_d_log_locate"},
            },
        ),
        BenchmarkTask(
            task_id="task_e_locate_then_modify",
            category="locate_then_modify",
            payload={
                "executor": "aider",
                "task": "定位并调整重试策略：RETRY_STRATEGY 改为 modern，TIMEOUT_MS 改为 1500。",
                "command": cmd_task_e(),
                "expected_files": ["src/config.py", "src/worker.py"],
                "test_env": {"TOA_TASK_ID": "task_e_locate_then_modify"},
            },
        ),
    ]


def run_orchestrator(
    task_payload: Dict[str, Any],
    repo: Path,
    config_path: Path,
    mode: str,
    mock_local: bool,
    raw_output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "orchestrate"),
        "--task",
        json.dumps(task_payload, ensure_ascii=False),
        "--repo",
        str(repo),
        "--config",
        str(config_path),
        "--mode",
        mode,
    ]
    if mock_local:
        cmd.append("--mock-local")

    proc = subprocess.run(
        cmd,
        cwd=str(repo),
        text=True,
        capture_output=True,
        timeout=300,
    )

    raw_out = (proc.stdout or "") + (proc.stderr or "")
    command_output_bytes = len(raw_out.encode("utf-8", errors="ignore"))
    if raw_output_path is not None:
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output_path.write_text(raw_out, encoding="utf-8", errors="ignore")

    routing_payload: Dict[str, Any] = {}
    handoff_payload: Dict[str, Any] = {}
    for data in _parse_json_stream(proc.stdout or ""):
        if not isinstance(data, Mapping):
            continue
        if data.get("kind") == "routing":
            routing_payload = data.get("payload", {})
        elif data.get("kind") == "handoff":
            handoff_payload = data.get("payload", {})

    return {
        "mode": mode,
        "exit_code": proc.returncode,
        "routing": routing_payload,
        "handoff": handoff_payload,
        "command_output_bytes": command_output_bytes,
        "command_output_file": str(raw_output_path) if raw_output_path is not None else None,
    }


def run_one(
    task: BenchmarkTask,
    mode: str,
    template: Path,
    config_path: Path,
    mock_local: bool,
    runner: str = "scripted",
    raw_output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        shutil.copytree(template, repo)
        task_payload = copy.deepcopy(task.payload)
        if runner == "codex":
            # The former fixture command made the benchmark a test of its own
            # shell scripts, not a test of an AI agent.  Real mode delegates
            # the task to Codex and keeps the same success assertions.
            task_payload["executor"] = "codex"
            task_payload.pop("command", None)
            task_payload.pop("actions", None)
        else:
            # Legacy fixture mode intentionally runs the deterministic command
            # in both modes. It is retained only as a smoke test and is never
            # evidence of AI task success or token savings.
            task_payload["executor"] = "command"
        result = run_orchestrator(
            task_payload,
            repo,
            config_path,
            mode,
            mock_local=mock_local and mode == "orchestrated",
            raw_output_path=raw_output_path,
        )

        handoff = result["handoff"]
        accounting = handoff.get("accounting", {})
        final = handoff.get("final", {})
        routing = handoff.get("route", result["routing"] if isinstance(result["routing"], dict) else {})
        test = handoff.get("test", {})
        diff = handoff.get("diff", {})
        context_circuit = handoff.get("context_circuit", {})
        execution = handoff.get("execution", {})

        unexpected = _safe_list(diff.get("unexpected_files", []))

        context_before = _safe_int(accounting.get("codex_context_before_bytes"))
        context_after = _safe_int(accounting.get("codex_context_after_bytes"))
        candidate_context_bytes = _safe_int(accounting.get("candidate_bytes"))
        estimated_input_tokens = _estimate_tokens(context_before)
        estimated_output_tokens = _estimate_tokens(context_after)

        real_input = accounting.get("real_input_tokens")
        real_output = accounting.get("real_output_tokens")
        token_count_mode = "ACTUAL" if isinstance(real_input, int) and isinstance(real_output, int) else "ESTIMATED"

        task_success = bool(final.get("task_success", False))
        test_pass = test.get("status") == "passed"

        return {
            "task_id": task.task_id,
            "category": task.category,
            "mode": mode,
            "baseline_comparable": True,
            "routing": routing,
            "execution_mode": routing.get("executor_mode", "unknown"),
            "status": final.get("status", "failed"),
            "task_success": task_success,
            "test_pass": test_pass,
            "test_status": test.get("status", "unknown"),
            "execution_success": bool(execution.get("success", False)),
            "execution_detail": execution.get("detail", ""),
            "requires_codex_review": bool(final.get("requires_codex_review", True)),
            "interrupted": bool(final.get("interrupt", False)),
            "candidate_context_bytes": candidate_context_bytes,
            "bytes_read_proxy": _safe_int(accounting.get("candidate_bytes")),
            "actual_codex_context_bytes": context_after,
            "files_considered": _safe_int(accounting.get("candidate_file_count")),
            "files_actually_read": _safe_int(accounting.get("file_count_read")),
            "code_lines_considered": _safe_int(accounting.get("candidate_line_count")),
            "code_lines_actually_read": _safe_int(accounting.get("lines_read")),
            "raw_log_bytes": _safe_int(accounting.get("log_raw_bytes")),
            "retained_log_bytes": _safe_int(accounting.get("log_filtered_bytes")),
            "tool_output_bytes": _safe_int(accounting.get("tool_output_bytes")),
            "diff_bytes": _safe_int(diff.get("diff_raw_bytes", diff.get("diff_bytes", 0))),
            "execution_time_ms": _safe_float(final.get("wall_clock_ms")),
            "local_worker_calls": _safe_int(accounting.get("local_worker_calls")),
            "codex_direct_calls": _safe_int(accounting.get("codex_direct_calls")),
            "escalation_count": _safe_int(accounting.get("escalation_count")),
            "retry_count": _safe_int(accounting.get("local_retry_count")),
            "unexpected_files_changed": len(unexpected),
            "unexpected_files": unexpected,
            "diff_changed_file_count": _safe_int(diff.get("files_changed_count")),
            "handoff_bytes": _safe_int(handoff.get("handoff_bytes")),
            "handoff_estimated_tokens": _safe_int(handoff.get("handoff_estimated_tokens")),
            "command_output_bytes": _safe_int(result.get("command_output_bytes")),
            "context_reduction_ratio": _safe_float(accounting.get("context_reduction_ratio")),
            "estimated_token_reduction_ratio": _safe_float(accounting.get("estimated_token_reduction_ratio")),
            "token_count_mode": token_count_mode,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_total_tokens": estimated_input_tokens + estimated_output_tokens,
            "actual_input_tokens": real_input,
            "actual_output_tokens": real_output,
            "actual_cached_tokens": accounting.get("real_cached_tokens"),
            "actual_reasoning_output_tokens": accounting.get("real_reasoning_output_tokens"),
            "source_truncated_bytes": _safe_int(accounting.get("source_truncated_bytes")),
            "context_circuit_trip": bool(context_circuit.get("tripped")),
            "context_circuit_trip_stage": context_circuit.get("trip_stage"),
            "context_circuit_trip_reason": context_circuit.get("trip_reason"),
            "context_circuit_trip_metric": context_circuit.get("trip_metric"),
            "execution_exit_code": _safe_int(execution.get("exit_code", -1)),
            "test_exit_code": _safe_int(test.get("exit_code", -1)),
            "candidate_context_before_bytes": context_before,
            "candidate_context_after_bytes": context_after,
            "runner": runner,
            "worker_mode": "REAL_CODEX" if runner == "codex" else (
                "MOCK" if (mock_local and mode == "orchestrated") else
                ("SCRIPTED_ORCHESTRATED" if mode == "orchestrated" else "SCRIPTED_BASELINE")
            ),
        }


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_mode: Dict[str, List[Dict[str, Any]]] = {}
    for row in records:
        by_mode.setdefault(row["mode"], []).append(row)

    def summarize_mode(mode_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(mode_rows)
        task_success = sum(1 for r in mode_rows if bool(r["task_success"]))
        tests_ok = sum(1 for r in mode_rows if bool(r["test_pass"]))
        escalations = sum(1 for r in mode_rows if _safe_int(r["escalation_count"]) > 0)
        unexpected_changes = sum(1 for r in mode_rows if _safe_int(r["unexpected_files_changed"]) > 0)
        local_calls = sum(_safe_int(r["local_worker_calls"]) for r in mode_rows)
        codex_calls = sum(_safe_int(r["codex_direct_calls"]) for r in mode_rows)
        context_before_total = sum(_safe_int(r["candidate_context_bytes"]) for r in mode_rows)
        context_after_total = sum(_safe_int(r["actual_codex_context_bytes"]) for r in mode_rows)
        execution_time = sum(_safe_float(r["execution_time_ms"]) for r in mode_rows)
        estimated_input_total = sum(_safe_int(r["estimated_input_tokens"]) for r in mode_rows)
        estimated_output_total = sum(_safe_int(r["estimated_output_tokens"]) for r in mode_rows)
        actual_input_rows = [r for r in mode_rows if isinstance(r.get("actual_input_tokens"), int)]
        actual_output_rows = [r for r in mode_rows if isinstance(r.get("actual_output_tokens"), int)]
        actual_input_total = sum(_safe_int(r["actual_input_tokens"]) for r in actual_input_rows)
        actual_output_total = sum(_safe_int(r["actual_output_tokens"]) for r in actual_output_rows)
        retry_count = sum(_safe_int(r["retry_count"]) for r in mode_rows)

        execution_calls = local_calls + codex_calls
        local_ratio = 0.0 if execution_calls == 0 else local_calls / execution_calls
        codex_ratio = 0.0 if execution_calls == 0 else codex_calls / execution_calls
        return {
            "count": n,
            "task_success": task_success,
            "task_success_rate": task_success / n if n else 0.0,
            "tests_pass": tests_ok,
            "test_pass_rate": tests_ok / n if n else 0.0,
            "context_reduction_ratio": _ratio(context_before_total, context_after_total),
            "estimated_token_reduction_ratio": _ratio(estimated_input_total, estimated_output_total),
            "context_before_bytes_total": context_before_total,
            "context_after_bytes_total": context_after_total,
            "estimated_context_tokens_before_total": estimated_input_total,
            "estimated_context_tokens_after_total": estimated_output_total,
            "actual_input_tokens_total": actual_input_total if len(actual_input_rows) == n else None,
            "actual_output_tokens_total": actual_output_total if len(actual_output_rows) == n else None,
            "actual_total_tokens": (
                actual_input_total + actual_output_total
                if len(actual_input_rows) == n and len(actual_output_rows) == n
                else None
            ),
            "escalation_rate": escalations / n if n else 0.0,
            "unexpected_change_rate": unexpected_changes / n if n else 0.0,
            "local_execution_ratio": local_ratio,
            "codex_execution_ratio": codex_ratio,
            "avg_execution_time_ms": execution_time / n if n else 0.0,
            "total_execution_time_ms": execution_time,
            "local_execution_calls": local_calls,
            "codex_direct_calls": codex_calls,
            "escalation_count": sum(_safe_int(r["escalation_count"]) for r in mode_rows),
            "retry_count": retry_count,
            "token_count_modes": sorted({str(r["token_count_mode"]) for r in mode_rows}),
            "candidate_context_bytes_total": context_before_total,
            "raw_log_bytes_total": sum(_safe_int(r["raw_log_bytes"]) for r in mode_rows),
            "retained_log_bytes_total": sum(_safe_int(r["retained_log_bytes"]) for r in mode_rows),
        }

    summary = {mode: summarize_mode(rows) for mode, rows in by_mode.items()}
    return {
        "total_runs": len(records),
        "task_count": len({row["task_id"] for row in records}) if records else 0,
        "runs_by_mode": summary,
        "token_count_coverage": sorted({str(row["token_count_mode"]) for row in records}) if records else [],
    }


def _format_ms(value: float) -> str:
    return f"{value:.1f}"


def _format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def write_summary_md(summary: Dict[str, Any], rows: List[Dict[str, Any]], path: Path) -> None:
    modes = list(summary.get("runs_by_mode", {}).keys())
    by_task: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], {})[row["mode"]] = row

    lines = [
        "# V1 Baseline vs Orchestrated Benchmark",
        "",
        f"modes={', '.join(modes)}",
        f"task_count={summary.get('task_count', 0)}",
        f"record_count={summary.get('total_runs', 0)}",
        f"token_count_mode={','.join(summary.get('token_count_coverage', [])) or 'UNKNOWN'}",
        "",
        "## 任务级对照（Baseline vs Orchestrated）",
        "",
        "| Task | Baseline Success | Orchestrated Success | Baseline Tests | Orchestrated Tests | Context bytes(Before->After) | Estimated tokens(Before) | Time(ms) B/O | Escalations (B/O) | Unexpected changes (B/O) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for task_id, rows_by_mode in by_task.items():
        baseline = rows_by_mode.get("baseline", {})
        orchestrated = rows_by_mode.get("orchestrated", {})
        lines.append(
            "| {task} | {bs} | {os} | {bt} | {ot} | {bc}->{ba} | {btk}/{otk} | {btm}/{otm} | {be}/{oe} | {bu}/{ou} |".format(
                task=task_id,
                bs="pass" if baseline.get("task_success") else "fail",
                os="pass" if orchestrated.get("task_success") else "fail",
                bt="pass" if baseline.get("test_pass") else "fail",
                ot="pass" if orchestrated.get("test_pass") else "fail",
                bc=baseline.get("candidate_context_bytes", 0),
                ba=baseline.get("actual_codex_context_bytes", 0),
                btk=baseline.get("estimated_input_tokens", 0),
                otk=orchestrated.get("estimated_input_tokens", 0),
                btm=_format_ms(baseline.get("execution_time_ms", 0.0)),
                otm=_format_ms(orchestrated.get("execution_time_ms", 0.0)),
                be=baseline.get("escalation_count", 0),
                oe=orchestrated.get("escalation_count", 0),
                bu=baseline.get("unexpected_files_changed", 0),
                ou=orchestrated.get("unexpected_files_changed", 0),
            )
        )

    lines.extend(
        [
            "",
            "## 聚合汇总",
            "",
            "| mode | task_success | test_pass | context_reduction | token_reduction | avg_time_ms | escalation_rate | unexpected_change_rate | local_ratio |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )

    for mode in modes:
        stats = summary["runs_by_mode"][mode]
        lines.append(
            "| {mode} | {success}/{count} ({sr:.1%}) | {tp}/{count} ({pr:.1%}) | {cr:.2%} | {tr:.2%} | {time:.1f} | {er:.1%} | {u:.1%} | {lr:.1%} |".format(
                mode=mode,
                success=stats["task_success"],
                count=stats["count"],
                sr=stats["task_success_rate"],
                tp=stats["tests_pass"],
                pr=stats["test_pass_rate"],
                cr=stats["context_reduction_ratio"],
                tr=stats["estimated_token_reduction_ratio"],
                time=stats["avg_execution_time_ms"],
                er=stats["escalation_rate"],
                u=stats["unexpected_change_rate"],
                lr=stats["local_execution_ratio"],
            )
        )

    baseline_stats = summary["runs_by_mode"].get("baseline", {})
    orchestrated_stats = summary["runs_by_mode"].get("orchestrated", {})
    if baseline_stats and orchestrated_stats:
        baseline_context_after = baseline_stats.get("context_after_bytes_total", 0) or 1
        baseline_token_after = baseline_stats.get("estimated_context_tokens_after_total", 0) or 1
        lines.extend(
            [
                "",
                "## Baseline vs Orchestrated 总计",
                "",
                f"Baseline total context bytes: {baseline_stats.get('context_after_bytes_total', 0):.0f}",
                f"Orchestrated total context bytes: {orchestrated_stats.get('context_after_bytes_total', 0):.0f}",
                f"context_reduction%(baseline->orchestrated): {_format_pct(1 - (orchestrated_stats.get('context_after_bytes_total', 0) / baseline_context_after))}",
                f"estimated_token_reduction%(baseline->orchestrated): {_format_pct(1 - (orchestrated_stats.get('estimated_context_tokens_after_total', 0) / baseline_token_after))}",
                f"Baseline actual input tokens: {baseline_stats.get('actual_input_tokens_total')}",
                f"Orchestrated actual input tokens: {orchestrated_stats.get('actual_input_tokens_total')}",
                f"Baseline actual total tokens: {baseline_stats.get('actual_total_tokens')}",
                f"Orchestrated actual total tokens: {orchestrated_stats.get('actual_total_tokens')}",
                f"avg execution_time_delta_ms: {(orchestrated_stats.get('avg_execution_time_ms', 0.0) - baseline_stats.get('avg_execution_time_ms', 0.0)):.3f}",
            ]
        )

    path.write_text("\n".join(lines), encoding="utf-8")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _build_thread_budget_handoff(
    budget: ThreadBudgetState,
    reason: str,
    next_modes: List[str],
    completed_modes: List[str],
) -> Dict[str, Any]:
    payload = {
        "kind": "thread_budget_handoff",
        "status": "NEED_NEW_THREAD",
        "reason": reason,
        "thread_budget_source": budget.limits.source,
        "proxy_metrics_tracked": {
            "tool_calls": budget.tool_calls,
            "tool_output_bytes": budget.tool_output_bytes,
            "files_read": budget.files_read,
            "file_bytes_read": budget.file_bytes_read,
            "file_lines_read": budget.file_lines_read,
            "command_log_bytes": budget.command_log_bytes,
            "diff_bytes": budget.diff_bytes,
            "stage_count": budget.stage_count,
            "handoff_estimated_tokens": budget.handoff_estimated_tokens,
        },
        "thread_budget_limits": {
            "tool_calls": budget.limits.tool_calls,
            "tool_output_bytes": budget.limits.tool_output_bytes,
            "files_read": budget.limits.files_read,
            "file_bytes_read": budget.limits.file_bytes_read,
            "file_lines_read": budget.limits.file_lines_read,
            "command_log_bytes": budget.limits.command_log_bytes,
            "diff_bytes": budget.limits.diff_bytes,
            "stage_count": budget.limits.stage_count,
            "handoff_estimated_tokens": budget.limits.handoff_estimated_tokens,
        },
        "resume": {
            "next_modes": next_modes[:3],
            "completed_modes": completed_modes[-3:],
            "last_stage": budget.last_stage,
            "stage_count": budget.stage_count,
        },
        "next_action": "start_new_thread_and_resume_using_progress_file",
    }
    if _estimate_tokens(len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))) > budget.limits.handoff_estimated_tokens_hard:
        payload = {
            "kind": "thread_budget_handoff",
            "status": "NEED_NEW_THREAD",
            "reason": reason[:120],
            "thread_budget_source": budget.limits.source,
            "stage_count": budget.stage_count,
            "next_mode": next_modes[0] if next_modes else None,
            "last_stage": budget.last_stage,
            "next_action": "start_new_thread_and_resume_using_progress_file",
        }
    return payload


def _run_ablation(
    template: Path,
    base_config_path: Path,
    args: argparse.Namespace,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool, Dict[str, Any], List[str], str]:
    catalog = _ablation_mode_catalog()
    selected = _parse_ablation_modes(args.ablation_modes)
    unknown = [mode for mode in selected if mode not in catalog]
    if unknown:
        raise ValueError(f"Unknown ablation mode(s): {', '.join(unknown)}")
    if not selected:
        selected = list(catalog.keys())

    all_tasks = benchmark_tasks()
    tasks = all_tasks[: args.ablation_task_limit] if args.ablation_task_limit > 0 else all_tasks

    limits = ThreadBudgetLimits(
        tool_calls=args.thread_budget_tool_calls,
        tool_output_bytes=args.thread_budget_tool_output_bytes,
        files_read=args.thread_budget_files_read,
        file_bytes_read=args.thread_budget_file_bytes,
        file_lines_read=args.thread_budget_file_lines,
        command_log_bytes=args.thread_budget_command_log_bytes,
        diff_bytes=args.thread_budget_diff_bytes,
        stage_count=args.thread_budget_stage_count,
        handoff_estimated_tokens=args.thread_budget_handoff_tokens,
        handoff_estimated_tokens_hard=args.thread_budget_handoff_hard_tokens,
        source="proxy",
    )
    budget = ThreadBudgetState(limits=limits)

    progress_path = Path(args.ablation_state_path) if args.ablation_state_path else OUT_DIR / f"{args.output_prefix}-ablation-state.json"
    raw_output_dir = OUT_DIR / f"{args.output_prefix}-orchestrator-raw"
    raw_output_dir.mkdir(parents=True, exist_ok=True)
    base_cfg = _parse_yaml_like(base_config_path)

    progress = _load_json(progress_path, {"modes": {}, "thread_budget": {}, "status": "new"})
    if not isinstance(progress, dict):
        progress = {"modes": {}, "thread_budget": {}, "status": "new"}

    records: List[Dict[str, Any]] = []
    completed_modes: List[str] = []
    previous_soft_progress = (progress.get("thread_budget") or {}).get("soft_progress")
    if isinstance(previous_soft_progress, dict):
        budget.tool_calls = _safe_int(previous_soft_progress.get("tool_calls"))
        budget.tool_output_bytes = _safe_int(previous_soft_progress.get("tool_output_bytes"))
        budget.files_read = _safe_int(previous_soft_progress.get("files_read"))
        budget.file_bytes_read = _safe_int(previous_soft_progress.get("file_bytes_read"))
        budget.file_lines_read = _safe_int(previous_soft_progress.get("file_lines_read"))
        budget.command_log_bytes = _safe_int(previous_soft_progress.get("command_log_bytes"))
        budget.diff_bytes = _safe_int(previous_soft_progress.get("diff_bytes"))
        budget.stage_count = _safe_int(previous_soft_progress.get("stage_count"))
        budget.handoff_estimated_tokens = _safe_int(previous_soft_progress.get("handoff_estimated_tokens"))

    for mode_key, mode_progress in (progress.get("modes") or {}).items():
        if not isinstance(mode_progress, dict):
            continue
        mode_records = mode_progress.get("records", [])
        if isinstance(mode_records, list) and len(mode_records) >= len(tasks):
            completed_modes.append(str(mode_key))
        if isinstance(mode_records, list):
            records.extend(mode_records)

    halted = False
    stop_reason = ""
    for mode_key in selected:
        if budget.tool_calls >= budget.limits.tool_calls:
            halted = True
            stop_reason = "thread_budget_tool_calls_limit"
            break

        mode = catalog[mode_key]
        mode_progress = progress.get("modes", {}).get(mode_key, {})
        if not isinstance(mode_progress, dict):
            mode_progress = {}
        mode_records = mode_progress.get("records", [])
        if not isinstance(mode_records, list):
            mode_records = []

        done_task_ids = {str(item.get("task_id", "")) for item in mode_records if isinstance(item, dict)}
        if mode_progress.get("status") == "completed" and len(mode_records) >= len(tasks):
            continue

        mode_cfg_path = base_config_path
        if mode.config_overrides:
            mode_cfg = _merge_config_bases(base_cfg, mode.config_overrides)
            mode_cfg_path = base_config_path.parent / f".ablation-{args.output_prefix}-{mode_key}.yaml"
            mode_cfg_path.write_text(_dump_yaml(mode_cfg), encoding="utf-8")

        for task in tasks:
            if task.task_id in done_task_ids:
                continue
            if budget.stage_count >= budget.limits.stage_count:
                halted = True
                stop_reason = "thread_budget_stage_count_limit"
                break

            task_raw_output = raw_output_dir / f"{mode_key}-{task.task_id}.jsonlog"
            result = run_one(
                task,
                mode.orchestrator_mode,
                template,
                mode_cfg_path,
                args.mock_local if mode.mock_local else False,
                raw_output_path=task_raw_output,
            )
            mode_records.append(result)
            records.append(result)
            exceeded, reason = budget.add_stage(mode_key, task.task_id, result)
            if exceeded:
                halted = True
                stop_reason = reason
                break

        if len(mode_records) >= len(tasks):
            mode_progress["status"] = "completed"
        else:
            mode_progress["status"] = "partial"
        mode_progress["record_count"] = len(mode_records)
        mode_progress["mode"] = mode_key
        mode_progress["records"] = mode_records

        progress.setdefault("modes", {})[mode_key] = mode_progress
        progress["thread_budget"] = budget.as_proxy_report()
        progress["status"] = "interrupted" if halted else "running"
        progress["last_reason"] = stop_reason if stop_reason else "running"
        progress["updated_at"] = str(time.time())
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

        if mode_progress["status"] == "completed" and mode_key not in completed_modes:
            completed_modes.append(mode_key)

        if halted:
            break

    summary = summarize(records)
    handoff: Dict[str, Any] = {}
    if halted:
        next_modes = [mode for mode in selected if mode not in completed_modes]
        handoff = _build_thread_budget_handoff(
            budget,
            stop_reason or "thread_budget_soft_limit",
            next_modes,
            completed_modes,
        )
    else:
        progress["status"] = "completed"
        progress["thread_budget"] = budget.as_proxy_report()
        progress["updated_at"] = str(time.time())
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")

    results = {
        "task_count": len(tasks),
        "modes": selected,
        "records": records,
        "summary": summary,
        "thread_budget": budget.as_proxy_report(),
        "thread_budget_source": "proxy",
        "thread_budget_handoff": handoff,
        "thread_budget_halted": halted,
        "bench_env": {
            "mode": "ablation",
            "mock_local_enabled": bool(args.mock_local),
            "mock_local_for_orchestrated": bool(args.mock_local),
            "token_count_mode": "ESTIMATED",
            "worker_scope": "MOCK" if args.mock_local else "CONFIG_UNSPECIFIED",
            "ablation_modes": selected,
            "ablation_task_limit": args.ablation_task_limit,
        },
    }
    return records, summary, halted, results, handoff, completed_modes, progress_path.as_posix()


def parse_args() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Token-aware orchestrator benchmark")
    p.add_argument("--config", default=str(ROOT / "config.example.yaml"))
    p.add_argument("--mode", choices=["both", "baseline", "orchestrated"], default="both")
    p.add_argument("--mock-local", action="store_true")
    p.add_argument("--runner", choices=["scripted", "codex"], default="scripted", help="scripted runs fixture commands; codex runs real Codex tasks")
    p.add_argument("--output-prefix", default="benchmark", help="output filename prefix under outputs/")
    p.add_argument("--ablation", action="store_true", help="run ablation mode matrix and persist mode-level progress")
    p.add_argument("--ablation-modes", default="BASELINE,FULL,NO_LOCAL_WORKER,NO_CONTEXT_FILTERING,NO_PRECISE_LOCALIZATION,MINIMAL_CORE")
    p.add_argument("--ablation-state-path", default="")
    p.add_argument("--ablation-task-limit", type=int, default=0, help="run only first N benchmark tasks; 0=all")
    p.add_argument("--thread-budget-tool-calls", type=int, default=16)
    p.add_argument("--thread-budget-tool-output-bytes", type=int, default=180_000)
    p.add_argument("--thread-budget-files-read", type=int, default=80)
    p.add_argument("--thread-budget-file-bytes", type=int, default=240_000)
    p.add_argument("--thread-budget-file-lines", type=int, default=24_000)
    p.add_argument("--thread-budget-command-log-bytes", type=int, default=120_000)
    p.add_argument("--thread-budget-diff-bytes", type=int, default=240_000)
    p.add_argument("--thread-budget-stage-count", type=int, default=8)
    p.add_argument("--thread-budget-handoff-tokens", type=int, default=500)
    p.add_argument("--thread-budget-handoff-hard-tokens", type=int, default=800)
    return p


def main(argv=None) -> int:
    args = parse_args().parse_args(argv)
    if args.ablation and args.runner == "codex":
        raise SystemExit("--ablation currently supports only --runner scripted")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        template = Path(td) / "template"
        initialize_repo(template)

        tasks = benchmark_tasks()
        records: List[Dict[str, Any]] = []

        if args.ablation:
            records, summary, halted, results, handoff, completed_modes, state_file = _run_ablation(
                template,
                Path(args.config),
                args,
            )
            if halted and handoff:
                print(f"thread_budget_status=NEED_NEW_THREAD")
                print(f"thread_budget_handoff_tokens={_estimate_tokens(len(json.dumps(handoff, ensure_ascii=False).encode('utf-8')))}")
                print(f"thread_budget_handoff_file={state_file}")
                print(json.dumps(handoff, ensure_ascii=False))
            print(f"completed_modes={','.join(completed_modes)}")
        else:
            modes = ["baseline", "orchestrated"] if args.mode == "both" else [args.mode]
            for mode in modes:
                for task in tasks:
                    records.append(run_one(task, mode, template, Path(args.config), args.mock_local, runner=args.runner))

            summary = summarize(records)
            results = {
                "task_count": len(tasks),
                "modes": modes,
                "records": records,
                "summary": summary,
                "bench_env": {
                    "mock_local_enabled": bool(args.mock_local),
                    "mock_local_for_orchestrated": bool(args.mock_local),
                    "token_count_mode": "ACTUAL" if args.runner == "codex" else "ESTIMATED",
                    "runner": args.runner,
                    "worker_scope": "REAL_CODEX" if args.runner == "codex" else ("MOCK" if args.mock_local else "CONFIG_UNSPECIFIED"),
                },
                "methodology": {
                    "template_reset": "temporary git repo per task/mode",
                    "same_task_per_mode": True,
                    "same_initial_commit": True,
                    "real_agent_execution": args.runner == "codex",
                    "usage_source": "codex exec --json turn.completed events" if args.runner == "codex" else "scripted fixture / estimated context proxy",
                },
            }
            print(f"completed_modes={','.join(modes)}")

        res_file = OUT_DIR / f"{args.output_prefix}-results.json"
        sm_file = OUT_DIR / f"{args.output_prefix}-summary.md"
        res_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        write_summary_md(summary, records, sm_file)

        print(f"results_file={res_file}")
        print(f"summary_file={sm_file}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
