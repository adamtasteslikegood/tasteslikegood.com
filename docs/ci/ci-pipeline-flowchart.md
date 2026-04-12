# CI/CD Pipeline Flow Chart

This flowchart shows the decision-based flow of the entire CI/CD pipeline from code push to merge.

```mermaid
flowchart TD
    A[Developer pushes code / opens PR] --> B{Event Type?}

    B -->|push to main/develop| C[Backend CI Workflow]
    B -->|pull_request to main/develop| C
    B -->|pull_request opened| D[Gemini Dispatch]
    B -->|push to any branch| E[Check Non-ASCII Filenames]
    B -->|pull_request by dependabot| F[Dependabot Auto-Merge]
    B -->|schedule hourly| G[Gemini Scheduled Triage]
    B -->|issue opened/reopened| D

    C --> C1[Lint: Black + Flake8]
    C --> C2[Type Check: mypy]
    C --> C3[Test: pytest + coverage]
    C --> C4[Security: Safety scan]

    C1 --> C1a{Black formatting OK?}
    C1a -->|Yes| C1b{Flake8 clean?}
    C1a -->|No| FAIL1[❌ Lint Failed]
    C1b -->|Yes| LINT_OK[✅ Lint Passed]
    C1b -->|No| FAIL1

    C2 --> C2a{mypy type check OK?}
    C2a -->|Yes| TYPE_OK[✅ Type Check Passed]
    C2a -->|No| FAIL2[❌ Type Check Failed]

    C3 --> C3a{All tests pass?}
    C3a -->|Yes| C3b[Upload coverage artifact]
    C3a -->|No| FAIL3[❌ Tests Failed]
    C3b --> TEST_OK[✅ Tests Passed]

    C4 --> C4a{Security issues found?}
    C4a -->|Yes| C4b[⚠️ Advisory - continue-on-error]
    C4a -->|No| SEC_OK[✅ Security Passed]
    C4b --> SEC_OK

    D --> D1{Extract Command}
    D1 -->|PR opened| D2[🔎 Gemini Review]
    D1 -->|Issue opened| D3[🔀 Gemini Triage]
    D1 -->|@gemini-cli /review| D2
    D1 -->|@gemini-cli /triage| D3
    D1 -->|@gemini-cli other| D4[▶️ Gemini Invoke]
    D1 -->|unrecognized| D5[Fallthrough - error comment]

    D2 --> D2a[AI code review on PR]
    D3 --> D3a[Classify + label issue]
    D4 --> D4a[Run Gemini CLI task]

    E --> E1{Non-ASCII filenames found?}
    E1 -->|No| E2[✅ Filenames OK]
    E1 -->|Yes| E3[Upload log artifact]
    E3 --> E4[❌ Check Failed]

    F --> F1{PR by dependabot?}
    F1 -->|Yes| F2[Auto-merge via squash]
    F1 -->|No| F3[Skip]

    LINT_OK --> GATE{All required checks pass?}
    TYPE_OK --> GATE
    TEST_OK --> GATE
    SEC_OK --> GATE
    E2 --> GATE

    GATE -->|Yes| MERGE[✅ Ready to merge]
    GATE -->|No| BLOCK[🚫 PR blocked]

    style FAIL1 fill:#ff6b6b,color:#fff
    style FAIL2 fill:#ff6b6b,color:#fff
    style FAIL3 fill:#ff6b6b,color:#fff
    style E4 fill:#ff6b6b,color:#fff
    style BLOCK fill:#ff6b6b,color:#fff
    style MERGE fill:#51cf66,color:#fff
    style LINT_OK fill:#51cf66,color:#fff
    style TYPE_OK fill:#51cf66,color:#fff
    style TEST_OK fill:#51cf66,color:#fff
    style SEC_OK fill:#51cf66,color:#fff
    style E2 fill:#51cf66,color:#fff
```
