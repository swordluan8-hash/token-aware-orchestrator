# Release Checklist (v0.4.0-alpha)

Use this checklist before publishing the GitHub Release Candidate.

- [ ] **No personal paths**
  - Search and remove/replace hard-coded paths containing local usernames or device paths.
  - Confirm docs/examples use generic placeholders (`/path/to/...`).
- [ ] **No API keys / credentials**
  - Confirm no API keys are present in tracked files.
  - Confirm no provider tokens or secrets are embedded in config examples.
- [ ] **No private/local config artifacts**
  - Remove install state files and machine-specific cache/handoff logs from distributable root.
- [ ] **README complete**
  - Title: `Token-Aware Orchestrator`
  - Tagline included.
  - Problem / Solution / Agent Compatibility / Architecture / Benchmark / Install / Usage / Limitations sections present.
- [ ] **Install test passed**
  - `python3 scripts/orchestratorctl.py install`
- [ ] **Status test passed**
  - `token-aware-orchestrator status`
- [ ] **Report test passed**
  - `token-aware-orchestrator report --json` or plain text.
- [ ] **Benchmark labeling**
  - Benchmark report indicates token data source as `REAL / ESTIMATED / MOCK` and does not claim exact-token precision.
- [ ] **License**
- [ ] Apache 2.0 license file exists in repo root.

