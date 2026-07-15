# Current State — CI on `dev` (measured 2026-07-14)

Mermaid source for [diagram-current-state.svg](diagram-current-state.svg).
Canonical context: [SPEC-01-ci-quality-gates.md](SPEC-01-ci-quality-gates.md).

```mermaid
flowchart LR
    subgraph NOW["Today (dev branch, measured 2026-07-14)"]
        direction TB
        PR1[PR into dev] --> N1["CodeQL ✅"]
        PR1 --> N2["Non-ASCII check ✅"]
        PR1 --> N3["Copilot review ✅"]
        PR1 -.->|"branch filter never matches"| N4["Backend CI (lint/type/test/security)<br/>💀 never runs"]
        N2 --> M1{{"Merge — ungated:<br/>no test, no lint signal"}}
    end
```
