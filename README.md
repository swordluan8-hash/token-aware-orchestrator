# Token-Aware Orchestrator

Make Codex read less, not think less.

`1.0.1`

Token-Aware Orchestrator (TOA) is a small, local control layer for OpenAI Codex CLI. It narrows the initial repository scope, applies a task token guardrail, runs Codex, and writes a JSON handoff containing test, diff, and usage evidence.

It does not replace Codex, guarantee a fixed saving percentage, or make a weak task specification reliable.

## What is real today

- Runs `codex exec --json` with a scoped task prompt.
- Reads Codex's `turn.completed.usage` JSONL event and stores reported input, output, cached-input, and reasoning-output tokens.
- Accepts structured task fields: task type, target model, maximum budget, priority, success criteria, and expected files.
- Records execution, auto-detected test status, changed files, unexpected-file detection, context-circuit signals, and the complete handoff JSON.
- Installs a Codex `SKILL.md` at `~/.codex/skills/token-aware-orchestrator/`.
- Includes regression tests and GitHub Actions CI.

## What is not a claim

- No universal “50% / 75.9% token saving” claim is made.
- The included scripted benchmark is a regression smoke test, not AI or token-saving evidence.
- A real benchmark requires two comparable Codex runs and their recorded usage fields.
- Claude Code, Gemini CLI, Cursor, and other adapters are future work, not current integrations.

## Install

```bash
git clone https://github.com/swordluan8-hash/token-aware-orchestrator.git
cd token-aware-orchestrator
python3 scripts/orchestratorctl.py install
export PATH="$HOME/.local/bin:$PATH"
token-aware-orchestrator status
```

The installer creates `~/.config/token-aware-orchestrator/config.yaml`, an executable shim in `~/.local/bin`, and the Codex skill file. It does not install the Codex CLI itself.

## Run one real task

Choose a model before starting:

| Task | Model |
| --- | --- |
| Clear rename, formatting, test adjustment | `gpt-5.6-luna` |
| Bug fix, normal multi-file implementation | `gpt-5.6-terra` |
| Architecture, difficult debugging, final audit | `gpt-5.6-sol` |

```bash
python3 scripts/orchestrator.py \
  --task '{
    "task":"Fix the zero-division behavior and add a regression test.",
    "task_type":"bug_fix",
    "target_model":"gpt-5.6-terra",
    "max_budget":25000,
    "priority":"high",
    "success_criteria":["Regression test passes","No unrelated files change"],
    "expected_files":["src/calculator.py","tests/test_calculator.py"]
  }' \
  --repo . \
  --mode orchestrated \
  --output outputs/first-real-run.json
```

Inspect the JSON output after each run:

```bash
python3 - <<'PY'
import json
p = json.load(open('outputs/first-real-run.json'))
h = p['handoff']
print(h['execution'])
print(h['accounting'])
print(h['final'])
PY
```

The authoritative usage values are `real_input_tokens`, `real_output_tokens`, `real_cached_tokens`, and `real_reasoning_output_tokens`. If they are `null`, the run did not provide usage data and must not be used as token evidence.

`max_budget` now limits the initial context estimate and is checked again against reported Codex usage. If a completed turn reports usage above the limit, the executor stops before requesting another turn and records `budget_exceeded: true`. A budget is a control guardrail, not a guarantee that billed usage can be retroactively undone.

Before launch, TOA also applies `executors.codex.preflight_overhead_tokens`. The default `48000` is a conservative baseline observed on the release-test Mac; if the requested budget is below this fixed Codex input overhead plus the task prompt, TOA returns `budget_preflight_blocked` without starting Codex. Calibrate this value on a different machine.

If no test command can be detected, a successful execution is reported as `final.status: "review_required"` rather than being mislabeled as a task failure. The task still needs human or project-specific validation.

## Benchmarking honestly

Scripted smoke test only:

```bash
python3 scripts/benchmark.py --runner scripted --mode both --output-prefix smoke
```

Real Codex benchmark:

```bash
python3 scripts/benchmark.py --runner codex --mode both --output-prefix codex-real
python3 scripts/orchestratorctl.py report --results outputs/codex-real-results.json
```

Use the same task set, repository fixture, Codex model, and configuration for the baseline and orchestrated runs. Compare recorded actual input and output tokens, success rate, tests, and unexpected changes. Do not publish a savings number until that comparison exists.

## Recording checkpoints

For a concise build video, record only these milestones:

1. Baseline issue and task payload.
2. First `turn.completed.usage` values from a real Codex run.
3. Baseline versus orchestrated result JSON.
4. CI passing on the pull request.
5. Final release tag and README.

## Development verification

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -v
git diff --check
```

## Limits

- The runtime needs a working, authenticated Codex CLI on the machine running the task.
- The budget is a guardrail, not a billing system; usage is only known after Codex reports it.
- Automatic file localization is an initial scope hint. Codex may inspect more files when the task requires it.
- macOS and Linux are the intended command-line environments for this release.
