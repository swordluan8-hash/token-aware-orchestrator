# Troubleshooting（Alpha）

## PATH 问题

**现象**：`token-aware-orchestrator: command not found`

1. 检查是否安装到默认目录：
   - `ls -l ~/.local/bin/token-aware-orchestrator`
2. 检查 PATH：
   - `echo $PATH | grep -q "$HOME/.local/bin" && echo ok`
3. 未通过则添加：
   - `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`（按当前 shell 调整）
   - `source ~/.zshrc`

## Codex 未检测

**现象**：`status` 显示 `Codex: WARN`

1. 确认运行目录是否包含项目源码（包含 `scripts/orchestrator.py`）。  
2. 检查 `~/.codex/skills` 下是否有 `token-aware-orchestrator` 目录且包含 `SKILL.md`（若不存在，仍可手动运行 install）。  
3. 重新执行：
   - `python3 scripts/orchestratorctl.py install`

## Skill 未加载

**现象**：`status` Core 或 System 中 `Skill` 报 warn/fail

1. 确认文件完整：
   - `scripts/orchestrator.py`
   - `scripts/benchmark.py`
   - `scripts/orchestrate`
2. 重新执行 install 覆盖：
   - `python3 scripts/orchestratorctl.py install --force`

## Local Worker 不可用

**现象**：`OPTIONAL` 或 Status 显示 local model 不可用

1. 检查 Ollama 进程与端口：
   - `curl http://127.0.0.1:11434/api/version`
2. 检查模型：
   - 在配置里检查 `--model` 对应条目
3. 允许不可用（期望行为）：
   - 系统会 fallback 到 `Codex Direct`
   - 报告中会显示 `Fallback: Codex Direct`

## Context Guard 触发

**现象**：某些任务变慢或回退率上升

1. 用 `status` 检查 Context Guard 是否开启。  
2. 关注 `report` 中 Context 字段变化（Before / After / Reduction）。  
3. 典型处理：
   - 降低单次任务复杂度
   - 缩减任务输入（先给明确文件边界）
   - 检查任务是否可分解为多个小任务再执行

## 如何查看 report

### 快速查看

```bash
token-aware-orchestrator report
```

### JSON 导出

```bash
token-aware-orchestrator report --json > "$TMPDIR/report.json"
```

### 保存 Markdown

```bash
token-aware-orchestrator report --output outputs/efficiency-report.md
```

`outputs` 默认目录可读可留存本次结果。
