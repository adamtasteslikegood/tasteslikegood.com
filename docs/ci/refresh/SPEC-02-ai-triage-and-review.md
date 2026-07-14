# SPEC-02 — AI Triage & Review Workflows Refresh

**Repository:** `adamtasteslikegood/tasteslikegood.com`
**Status:** Proposal
**Date:** 2026-07-14
**Depends on:** [SPEC-01](SPEC-01-ci-quality-gates.md) (core gates come first)

---

## 1. History — what died and why

The repo ran a four-workflow Gemini pipeline:

| Workflow | Role | Fate |
|---|---|---|
| `gemini-dispatch.yml` | Entry point — listened to PR/issue/comment events, routed to the three below via `workflow_call` | Deleted `9ef54fc` (2026-07-04): **failed on every PR/issue event from April to July**. Three parallel fix attempts (#122/#123/#124) all stalled |
| `gemini-invoke.yml` | `@gemini-cli` on-demand agent | Deleted (unreachable without dispatch) |
| `gemini-review.yml` | PR code review | Deleted (unreachable without dispatch) |
| `gemini-triage.yml` | Issue labeling on open | Deleted (unreachable without dispatch) |
| `gemini-scheduled-triage.yml` | Hourly cron: triages **all** unlabeled issues in batch | **Alive and green** — kept |

The command definitions in `.github/commands/gemini-*.toml` were left in place, so the
prompt/tooling content of the old pipeline is preserved and reusable.

Structural lessons from the failure:

1. **The dispatch hub was a single point of failure.** One broken router took all four
   functions down, and because callees were `workflow_call`-only, nothing could run
   standalone.
2. **It ran on every event with no off-switch**, so its failure spammed every PR with a
   red ❌, training everyone to ignore CI status — corrosive to the real gates.
3. **Nobody was alerted** that it had been failing for two months. Red checks on merged
   PRs were normalized.

## 2. Goals

- Restore the two AI functions that earned their keep: **issue triage** (scheduled one
  already works) and **on-demand PR review/agent invocation**.
- No component may ever again fail on every PR: AI workflows must be **opt-in per
  event** and **non-blocking** (`continue-on-error` at job level, never a required
  check).
- A silently-failing scheduled workflow must page someone (issue creation on failure).

### Non-goals

- Making AI review a merge gate. Copilot review already runs on PRs; Gemini review is
  additive advisory signal.
- Rebuilding the dispatch hub. Standalone workflows only.

## 3. Design

```mermaid
flowchart TD
    subgraph EVENTS
        E1["cron (every 6h)"]
        E2["issue opened"]
        E3["PR labeled 'gemini-review'"]
        E4["comment '@gemini-cli ...'<br/>from OWNER/MEMBER/COLLABORATOR only"]
    end
    E1 --> W1["gemini-scheduled-triage.yml<br/>(exists — keep, retune cadence)"]
    E2 --> W2["gemini-triage.yml<br/>standalone, reuses commands/gemini-triage.toml"]
    E3 --> W3["gemini-review.yml<br/>standalone, reuses commands/gemini-review.toml"]
    E4 --> W4["gemini-invoke.yml<br/>standalone, reuses commands/gemini-invoke.toml"]
    W1 & W2 & W3 & W4 --> GUARD["Shared guardrails:<br/>timeout-minutes: 10<br/>concurrency per ref<br/>continue-on-error (never required)<br/>pinned action SHAs"]
    W1 -->|on failure of either job| ALERT["dedicated alert job<br/>needs: [triage, label], issues: write<br/>auto-open issue, label: ci-health"]
    style GUARD fill:#7c3aed,color:#fff
    style ALERT fill:#b91c1c,color:#fff
```

Key choices, mapped to the failure lessons:

1. **Standalone workflows, no router.** Each `.yml` owns its own trigger. One breaking
   leaves the others running.
2. **Opt-in triggers.** Review runs only when a human applies the `gemini-review`
   label (or on `workflow_dispatch`). Invoke runs only on `@gemini-cli` comments from
   users with `OWNER`/`MEMBER`/`COLLABORATOR` association — this is also the prompt-
   injection guard: never execute instructions from untrusted comment bodies.
3. **Failure hygiene.** Every workflow: `timeout-minutes`, `concurrency` group,
   job-level `continue-on-error: true`. The scheduled triage gets alerting that
   opens/updates a `ci-health` issue, so a two-month silent failure cannot recur.
   Note the workflow has **two jobs** (`triage`, which only has `issues: read`, and
   `label`, which has `issues: write`) — a failure step inside one job cannot see the
   other's result. Implement alerting as a **dedicated final job**:
   `needs: [triage, label]`, `if: failure()`, with its own `issues: write` permission,
   so a failure in either stage raises the alarm and issue creation is never denied.
4. **Retune the cron.** Hourly (24 runs/day) is over-provisioned for current issue
   volume — the April audit flagged this. Move to every 6 hours.
5. **Secrets/auth**: reuse whatever `gemini-scheduled-triage.yml` uses today
   (`GEMINI_API_KEY` / Vertex WIF) — it is the working reference implementation.
   Pin `google-github-actions/run-gemini-cli` to a commit SHA.

## 4. Rollout

1. Retune + alerting on `gemini-scheduled-triage.yml` (lowest risk, already green).
2. Add standalone `gemini-triage.yml` (issue-opened). Validate on a test issue.
3. Add `gemini-review.yml` (label-triggered). Validate on a Dependabot PR.
4. Add `gemini-invoke.yml` (comment-triggered) **last** — it has the largest injection
   surface; ship only with the association allowlist verified.

Each step is its own PR; a step that misbehaves gets reverted without touching the
others.

## 5. Acceptance criteria

1. `gh run list --workflow=gemini-scheduled-triage.yml` shows green runs at the 6-hour
   cadence, and a forced failure (temporarily bad API key on a test branch) opens a
   `ci-health` issue.
2. Opening an issue gets it labeled within one run of `gemini-triage.yml`.
3. Applying `gemini-review` to a PR produces review comments; **no** Gemini check ever
   appears in branch-protection required checks.
4. An `@gemini-cli` comment from a non-collaborator does nothing (verify with a test
   account or association simulation).
5. Two weeks post-rollout: zero PRs show a failing Gemini check.
