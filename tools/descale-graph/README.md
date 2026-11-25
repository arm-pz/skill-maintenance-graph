# Descale Graph (Brew 10 · Maintain the skill family)

A typed, loop-backed, code-gated workflow for keeping every skill published on Skillsmith by Caffeinity healthy after release.

Built on the same three invariants as `arm-pz/pr-graph`:
1. **Typed JSON reports at every node** (`tools/descale-graph/schemas/`).
2. **A bounded fix-and-verify loop** (`tune-skill` ⇄ fresh `verify`, max 3 rounds).
3. **A deterministic Python gate** (`tools/descale-graph/descale_gate.py`) that makes the final pass/hold call from code, never from a model verdict.

## Graph

```
health.yml / health.py ─▶ triage ─▶ regressions? ─no──▶ gate (descale_gate.py) ─▶ clean
                            ▲            │ yes
                            │            ▼
                        verify ◀──── tune (tune-skill)
                        (fresh)       escalate? ─▶ needs-owner, stop
                      round >= 3 ─▶ needs-owner, stop
                            │
                          pass
                            │
                            ├─[behavior_changed]─▶ release (prompts-to-skill-release) ─┐
                            └─[no behavior change]─────────────────────────────────────┴─▶ upstream (upstream-check) ─▶ gate ─▶ ready_to_publish | hold
```

The machine-readable graph specification lives in `tools/descale-graph/graph.json`.

## Files

| Path | Role |
| --- | --- |
| `tools/descale-graph/graph.json` | Declarative graph spec (triggers, nodes, edges, loop cap, protected paths) |
| `tools/descale-graph/descale_gate.py` | Deterministic gate (`evaluate` + CLI) |
| `tools/descale-graph/schemas/*.schema.json` | Typed handoff contracts between nodes |
| `.claude/skills/descale-graph/SKILL.md` | Conductor instructions for running the graph |
| `.claude/agents/descale-tuner.md` | Fixer node (`tune-skill`) |
| `.claude/agents/descale-verifier.md` | Fresh-eyes verification node (`check_skill.py`, `regress.py`, evals) |
| `.claude/agents/descale-releaser.md` | Optional release node (`prompts-to-skill-release`) |
| `tools/agent-skills/tests/test_descale_gate.py` | Unit tests and `KNOWN_BAD` regression set |

## Running the gate

```sh
python tools/descale-graph/descale_gate.py \
  --run-dir .descale-graph/runs/<run-id> \
  --state .descale-graph/runs/<run-id>/state.json
```

Add `--require-evals` when running in an authenticated runner where `eval_check` must not be `skipped`.

Exit codes match `pr-graph/merge_gate.py`:
- `0` — pass (`clean` or `ready_to_publish`)
- `1` — hold (a deterministic check failed; `reasons` lists what the owner must resolve)
- `2` — gate error (missing/unreadable report, broken spec, or invalid CLI input)

## Guardrails

- **No auto-accepting baselines:** `site/health.json` is protected (`--accept-all` is human-only).
- **No direct pushes to default branches or force-pushes:** `tune` commits on a fix branch; `upstream-check` screens outgoing diffs for secrets and refuses `behind` or `diverged` repos.
- **No self-grading:** `verify` runs in a fresh subagent using `check_skill.py`, `regress.py`, and routing/trigger evals without the tuner's reasoning.
- **Protected paths hold for human review:** changes touching `.github/workflows/`, `.claude/`, `tools/descale-graph/`, `tools/pr-graph/`, `site/health.json`, `site/deny.json`, or `CLAUDE.md` never pass automatically.

## Tests

```sh
python -m unittest discover -s tools/agent-skills/tests -v
```
