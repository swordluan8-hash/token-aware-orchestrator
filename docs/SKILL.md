---
name: token-aware-orchestrator
description: Route a scoped coding task through Token-Aware Orchestrator and retain Codex usage evidence.
---

# Token-Aware Orchestrator

Use this skill when a coding task needs a bounded repository scope, a token budget, and a machine-readable handoff record.

## Workflow

1. Choose the model by task risk: `gpt-5.6-luna` for clear mechanical work, `gpt-5.6-terra` for normal multi-file implementation, and `gpt-5.6-sol` only for architecture or final review.
2. State the success criteria and an input/output token ceiling before execution.
3. Run `scripts/orchestrator.py` with `executor: "codex"`, `target_model`, `max_budget`, and `--output`.
4. Treat `turn.completed.usage` values in the output JSON as the token evidence. Do not present byte estimates or scripted-fixture results as real token savings.
5. Check `handoff.final.task_success`, `handoff.final.status`, the detected test result, `handoff.diff.unexpected_files`, and the reported token usage before calling a task complete. Treat `review_required` as unresolved validation, not as success.

## Task payload

```json
{
  "task": "Describe one concrete coding task.",
  "task_type": "bug_fix",
  "target_model": "gpt-5.6-terra",
  "max_budget": 25000,
  "priority": "high",
  "success_criteria": ["Tests pass", "Only intended files change"],
  "expected_files": ["src/example.py"]
}
```

`max_budget` is a guardrail for the task. The preflight narrows the initial context estimate; after each reported completed turn, the executor compares the cumulative input-plus-output total against the limit and stops before another turn when it is exceeded. The reported usage already spent remains in the handoff.

## Command

```bash
python3 scripts/orchestrator.py \
  --task '<JSON payload>' \
  --repo /path/to/repository \
  --mode orchestrated \
  --output /path/to/run.json
```

The current runtime targets OpenAI Codex CLI. Future adapters are intentionally not represented as working integrations.
