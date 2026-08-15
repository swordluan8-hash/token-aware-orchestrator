# Quick Start（5 分钟）

目标：5 分钟内完成安装、健康检查、运行一次任务、产出一份报表。

## 1. Install

```bash
cd /path/to/token-aware-orchestrator-v0-3-1
python3 scripts/orchestratorctl.py install
```

输出应包含：

- `READY:`
  - Codex detected
  - Skill installed
- `OPTIONAL:`
  - Local Worker availability（不可用会提示 fallback）
- PATH 建议（如未检测到）

## 2. Status

```bash
token-aware-orchestrator status
```

查看三部分：

- System：Codex / Skill / Config  
- Core：Localization / Context Guard / Thread Guard / State  
- Optional：Ollama / Aider / Local model  

结尾会看到：

- `System Status: READY / WARNING / ERROR`

## 3. Run task（示例）

```bash
cd /path/to/your/repo
python3 /path/to/token-aware-orchestrator-v0-3-1/scripts/orchestrate \
  --mode orchestrated \
  --task '{"task":"Replace constant with config value in one file"}' \
  --repo . \
  --executor command \
  --command "echo noop"
```

> 实际 `--task` 结构可使用你当前环境里的执行方式。  
> 以上只演示 CLI 可用性，不替代你的真实工作流参数。

## 4. Report

```bash
token-aware-orchestrator report
```

会显示：

- 任务数  
- Context Before / After / Reduction  
- Success / Tests / Unexpected changes  
- Local Worker 使用状态与 fallback 情况  

如需保存：

```bash
token-aware-orchestrator report --output outputs/report.md
```

## 5. 如果看到警告

- 先执行 `token-aware-orchestrator status --json` 拿完整日志  
- 重点关注：
  - PATH 是否包含 `~/.local/bin`
  - Ollama 是否运行（如启用本地模型）
  - Codex 是否能检测到 skill 目录
