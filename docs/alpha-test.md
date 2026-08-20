# V1 Alpha Test Guide

目标：验证真实 Codex 路径、usage 记录和质量门，而不是验证脚本化演示。

## 测试前提

- macOS 或 Linux。
- 已登录并可运行 `codex exec --json`。
- 一个可恢复的 Git 测试仓库。

## 步骤

1. 在 TOA 根目录运行 `python3 scripts/orchestratorctl.py install`，然后运行 `token-aware-orchestrator status`。
2. 确认 `Codex CLI` 和 `Codex Skill` 是 PASS。
3. 选一个可验证的真实 bug fix，填写 `task_type`、`target_model`、`max_budget`、`success_criteria`、`expected_files`。
4. 用 `--output outputs/run.json` 执行。
5. 记录 `handoff.execution`、`handoff.test`、`handoff.diff` 和 `handoff.accounting`。

## 通过标准

- `handoff.final.task_success` 为 `true`。
- 自动测试通过，或测试状态和原因明确。
- `unexpected_files` 为空，或变更经过人工确认。
- `real_input_tokens`、`real_output_tokens` 为非负整数。

## 反馈格式

- 操作系统和 Codex CLI 版本。
- 任务 payload（移除敏感信息）。
- 输出 JSON 中的 `handoff`。
- 是否完成、测试结果、意外修改文件、reported usage。
- 复现步骤与终端错误输出。
