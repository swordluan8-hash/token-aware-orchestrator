# Release Checklist (V1.0.1)

- [ ] `python3 -m py_compile scripts/*.py` passes.
- [ ] `python3 -m unittest discover -s tests -v` passes.
- [ ] `git diff --check` is clean.
- [ ] GitHub Actions CI passes on the release branch.
- [ ] `python3 scripts/orchestratorctl.py install` creates the CLI, config, and `SKILL.md`.
- [ ] `token-aware-orchestrator status` finds a working Codex CLI on the release-test Mac.
- [ ] A real `codex exec --json` task writes non-null `real_input_tokens` and `real_output_tokens`.
- [ ] A baseline and orchestrated real-Codex comparison uses the same fixture, model, and task set.
- [ ] README reports only measured results from that comparison; no proxy result is presented as real token savings.
- [ ] No personal paths, credentials, local install state, or run artifacts are tracked.
- [ ] License is present and correct.
