from __future__ import annotations

import sys
import tempfile
import unittest
import json
import contextlib
import io
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import detect_resources  # noqa: E402
import orchestrator  # noqa: E402
import orchestratorctl  # noqa: E402


class ResourceDetectionTests(unittest.TestCase):
    def test_detect_ollama_without_installed_binary(self) -> None:
        original_which = detect_resources.shutil.which
        try:
            detect_resources.shutil.which = lambda _: None
            result = detect_resources.detect_ollama("http://127.0.0.1:9", "qwen2.5-coder:7b")
        finally:
            detect_resources.shutil.which = original_which

        self.assertIsNone(result["binary"])
        self.assertFalse(result["reachable"])


class DiffQualityGateTests(unittest.TestCase):
    def test_changed_files_are_exposed_to_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            tracked = repo / "tracked.py"
            tracked.write_text("before\n", encoding="utf-8")

            import subprocess

            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)

            tracked.write_text("after\n", encoding="utf-8")
            result = orchestrator._run_git_diff(repo)

        self.assertEqual(result["changed_files"], ["tracked.py"])
        self.assertEqual(result["files_changed_count"], 1)
        self.assertNotIn("files", result)


class CodexUsageTests(unittest.TestCase):
    def test_parse_codex_jsonl_usage(self) -> None:
        raw = "\n".join(
            [
                '{"type":"thread.started","thread_id":"x"}',
                '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":80,"output_tokens":12,"reasoning_output_tokens":9}}',
                '{"type":"turn.completed","usage":{"input_tokens":30,"cached_input_tokens":20,"output_tokens":3}}',
            ]
        )
        self.assertEqual(
            orchestrator._parse_codex_jsonl(raw),
            {"input": 150, "cache": 100, "output": 15, "reasoning_output": 9},
        )

    def test_parse_codex_jsonl_without_usage(self) -> None:
        self.assertEqual(
            orchestrator._parse_codex_jsonl('{"type":"turn.started"}'),
            {"input": None, "cache": None, "output": None, "reasoning_output": None},
        )

    def test_codex_executor_records_reported_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "print('{\\\"type\\\":\\\"turn.completed\\\",\\\"usage\\\":{\\\"input_tokens\\\":21,\\\"cached_input_tokens\\\":13,\\\"output_tokens\\\":8,\\\"reasoning_output_tokens\\\":5}}')\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            config = orchestrator._load_config(None)
            config["executors"]["codex"]["binary"] = str(fake_codex)
            task = orchestrator.TaskInput(raw="inspect", payload={"task": "inspect", "executor": "codex"})
            accounting = orchestrator.TokenAccounting(mode="orchestrated")
            result = orchestrator.CodexExecutor().run(task, root, config, ["example.py"], 1, accounting)

        self.assertTrue(result.success)
        self.assertEqual(accounting.real_input_tokens, 21)
        self.assertEqual(accounting.real_cached_tokens, 13)
        self.assertEqual(accounting.real_output_tokens, 8)
        self.assertEqual(accounting.real_reasoning_output_tokens, 5)

    def test_codex_executor_blocks_over_budget_prompt_before_execution(self) -> None:
        config = orchestrator._load_config(None)
        task = orchestrator.TaskInput(
            raw="over budget",
            payload={"task": "a deliberately long task", "executor": "codex", "max_budget": 1},
        )
        accounting = orchestrator.TokenAccounting(mode="orchestrated")
        result = orchestrator.CodexExecutor().run(task, PROJECT_ROOT, config, [], 1, accounting)

        self.assertFalse(result.success)
        self.assertEqual(result.detail, "budget_preflight_blocked")

    def test_structured_task_fields_are_normalized(self) -> None:
        task = orchestrator.TaskInput(
            raw="x",
            payload={
                "task": "x",
                "task_type": "bug_fix",
                "target_model": "gpt-5.6-terra",
                "max_budget": "2000",
                "priority": "high",
                "success_criteria": ["tests pass", "", 42],
            },
        )

        self.assertEqual(task.task_type, "bug_fix")
        self.assertEqual(task.target_model, "gpt-5.6-terra")
        self.assertEqual(task.max_budget, 2000)
        self.assertEqual(task.priority, "high")
        self.assertEqual(task.success_criteria, ["tests pass", "42"])


class PersistenceTests(unittest.TestCase):
    def test_cli_writes_complete_handoff_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            import subprocess

            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            output = Path(tmp) / "runs" / "task.json"
            task = json.dumps({"task": "no-op", "executor": "command", "command": "true"})
            with contextlib.redirect_stdout(io.StringIO()):
                result = orchestrator.main(["--task", task, "--repo", str(repo), "--output", str(output)])
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 1)  # no test command exists, so quality gate correctly fails
        self.assertIn("routing", payload)
        self.assertIn("handoff", payload)
        self.assertEqual(payload["handoff"]["accounting"]["codex_direct_calls"], 0)


class InstallerTests(unittest.TestCase):
    def test_install_copies_a_codex_skill_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "install-state.json"
            original_state = orchestratorctl.STATE_FILE
            try:
                orchestratorctl.STATE_FILE = state
                with contextlib.redirect_stdout(io.StringIO()):
                    result = orchestratorctl.main(
                        [
                            "install",
                            "--bin-dir", str(root / "bin"),
                            "--config-dir", str(root / "config"),
                            "--skill-dir", str(root / "skills" / "token-aware-orchestrator"),
                            "--json",
                        ]
                    )
            finally:
                orchestratorctl.STATE_FILE = original_state

            self.assertEqual(result, 0)
            self.assertTrue((root / "skills" / "token-aware-orchestrator" / "SKILL.md").exists())
            self.assertTrue(state.exists())


if __name__ == "__main__":
    unittest.main()
