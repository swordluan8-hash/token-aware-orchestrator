# Troubleshooting（V1）

## `token-aware-orchestrator: command not found`

```bash
export PATH="$HOME/.local/bin:$PATH"
token-aware-orchestrator status
```

将同一行加入你的 shell 配置文件后，重新打开终端。

## `Codex CLI: WARN`

TOA 不安装 Codex CLI。先在同一个终端确认：

```bash
codex --version
codex exec --json "Reply only: ready"
```

修复 Codex 登录或安装问题后再运行 `token-aware-orchestrator status`。

## `real_*_tokens` 是 `null`

该运行没有收到 `turn.completed.usage` 事件。不要用它计算 token 节省；保存输出 JSON，并先直接运行 `codex exec --json` 以确认 CLI 能提供 usage。

## 任务被 `budget_preflight_blocked` 阻止

任务 prompt 或初始范围的估算已超过 `max_budget`，或者 Codex 已报告累计 usage 超限。缩小任务范围、拆分任务，或明确提高预算后重试。超限前已经产生的 usage 不会被撤销；检查 `handoff.accounting.budget_exceeded` 和实际 token 字段。

如果 `budget_preflight_blocked` 显示固定开销过高，先查看 `executors.codex.preflight_overhead_tokens`。这是当前机器的保守 Codex 输入开销，不是项目文件大小；不要用 6,000 这样的预算启动一个固定开销已经接近 48,000 的 Codex 环境。

## `final.status: review_required`

Codex 执行本身成功，但仓库没有可自动检测的测试命令。补充项目测试命令或人工检查 `diff` 后，才能把任务视为完成。

## Local Worker / Ollama 显示 WARN

V1 的 Codex 路径不依赖它们。除非你正在专门测试本地 worker，否则可忽略该警告。

## Context circuit 触发

查看 JSON 中的 `handoff.context_circuit`。优先拆分任务、减少输入文件和日志量，并填写准确的 `expected_files`。
