# AI Workflows — Gemini triage / review / invoke (standalone, non-blocking)

Mermaid source for [diagram-ai-workflows.svg](diagram-ai-workflows.svg).
Canonical context: [SPEC-02-ai-triage-and-review.md](SPEC-02-ai-triage-and-review.md).

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
    W1 & W2 & W3 & W4 --> GUARD["Shared guardrails:<br/>timeout-minutes: 10<br/>concurrency per ref<br/>continue-on-error on event-triggered jobs only<br/>(never on scheduled — it would mask the alert)<br/>never a required check · pinned action SHAs<br/>checkout jobs: GEMINI_CLI_TRUST_WORKSPACE=true"]
    W1 -->|on failure of either job| ALERT["dedicated alert job<br/>needs: [triage, label], issues: write<br/>auto-open issue, label: ci-health"]
    style GUARD fill:#7c3aed,color:#fff
    style ALERT fill:#b91c1c,color:#fff
```
