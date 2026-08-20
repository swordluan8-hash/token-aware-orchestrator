# Quick Start（V1）

目标：安装、确认 Codex CLI、运行一次有真实 usage 记录的任务。

## 1. 安装

```bash
git clone https://github.com/swordluan8-hash/token-aware-orchestrator.git
cd token-aware-orchestrator
python3 scripts/orchestratorctl.py install
export PATH="$HOME/.local/bin:$PATH"
token-aware-orchestrator status
```

`status` 的 `Codex CLI` 和 `Codex Skill` 都应为 PASS。Ollama/Aider 是可选项，WARN 不阻止 Codex 路径运行。

## 2. 定义一个有边界的任务

在任务开始前明确模型、预算、成功条件和预期修改文件。

```bash
cd /path/to/your/repository
python3 /path/to/token-aware-orchestrator/scripts/orchestrator.py \
  --task '{
    "task":"Fix the input validation bug and add a regression test.",
    "task_type":"bug_fix",
    "target_model":"gpt-5.6-terra",
    "max_budget":25000,
    "priority":"high",
    "success_criteria":["Tests pass","No unrelated files change"],
    "expected_files":["src/validator.py","tests/test_validator.py"]
  }' \
  --repo . \
  --mode orchestrated \
  --output outputs/run-001.json
```

模型选择：明确、机械的小改动用 Luna；普通 bug fix / 多文件实现用 Terra；架构和最终审计才用 Sol。

## 3. 检查结果

```bash
python3 - <<'PY'
import json
h = json.load(open('outputs/run-001.json'))['handoff']
print('final:', h['final'])
print('test:', h['test'])
print('diff:', h['diff'])
print('usage:', h['accounting'])
PY
```

只有 `real_input_tokens` 和 `real_output_tokens` 是整数时，才可把该次运行用于 token 统计。

## 4. 真实对照实验

```bash
python3 scripts/benchmark.py --runner codex --mode both --output-prefix codex-real
python3 scripts/orchestratorctl.py report --results outputs/codex-real-results.json
```

`--runner scripted` 仅用于回归 smoke test，不代表 AI 任务成功或 token 节省。
