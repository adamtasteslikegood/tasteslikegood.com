# CI/CD Refresh — 2026-07

Proposal to restore the CI/CD gates this repo lost between April and July 2026:
`ci.yml` stopped matching the `dev` branch (so Black/Flake8/mypy/pytest never run),
the Gemini dispatch pipeline was deleted after months of failures, and the tree
rotted without gates.

| File | What it is |
|---|---|
| [SPEC-01-ci-quality-gates.md](SPEC-01-ci-quality-gates.md) | Main proposal: audit findings (with measured baseline), target pipeline, phased rollout, branch protection |
| [SPEC-02-ai-triage-and-review.md](SPEC-02-ai-triage-and-review.md) | Post-mortem of the Gemini dispatch pipeline and a standalone, opt-in, non-blocking replacement |
| [TODO.md](TODO.md) | Phase-by-phase execution checklist; every item carries its verify command |
| [PROMPT.md](PROMPT.md) | Agent-harness / loop goal definition: task table with verifies, retry caps, escalation rules, close condition |
| `diagram-*.svg` | Pre-rendered exports of the specs' Mermaid diagrams: [current state](diagram-current-state.svg) · [target pipeline](diagram-target-pipeline.svg) · [AI workflows](diagram-ai-workflows.svg) · [harness loop](diagram-harness-loop.svg) |

Predecessor: [../CI-AUDIT-REPORT.md](../CI-AUDIT-REPORT.md) (April 2026 audit — its
branch-protection recommendation was never implemented; SPEC-01 finishes the job).
