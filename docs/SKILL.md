---
name: token-aware-orchestrator
description: >
  Context optimization layer for AI coding agents. Focus on minimizing unnecessary
  context reads, enforcing guarded execution, and preserving quality signals.
---

# Token-Aware Orchestrator

AI Agent Context Optimization Layer.

## Scope

- Works as an adapter layer that can route to different agent runtimes.
- Keeps context read volume low and execution quality verifiable.
- Enables controlled fallback when optimization is unavailable.
- Local Worker is optional and non-blocking.

## Compatibility

Current target:
- OpenAI Codex

Planned adapters:
- Claude Code
- Gemini CLI
- Grok-related coding workflows
- Cursor
- Other mainstream AI coding agents
