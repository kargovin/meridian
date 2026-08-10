---
parent_id: 524290
title: Sandbox — ADF renderer smoke test
version_message: smoke test
---

::doc-control
Document type: Renderer smoke test
Author: Anaya Rao
Status: {status:green|APPROVED}
Reviewers: Elena Petrova, Jordan Osei
Date: @2026-07-21
::

## 1. Prose and marks

Plain text with **strong**, *em*, `inline_code`, a [link](https://example.com),
a {status:red|BLOCKED} lozenge and a @2026-07-21 date node, all in one paragraph
that wraps across source lines.

:::panel warning
Panels nest **blocks**, not just text.

- including lists
- like this one
:::

## 2. Lists

- top level
- second item
  - nested child
  - another child
- third

1. ordered one
2. ordered two

## 3. Table

| Rev | Date | Author | Change |
| --- | --- | --- | --- |
| 18 | @2026-07-18 | Marcus Chen | Post-2.2 carry-back |
| **19** | @2026-07-21 | Marcus Chen | New requirement **FR-S6** |

## 4. Governance blocks

::decisions
J1 — Triton-class multi-backend serving stack.
J2 — ONNX Runtime for the three encoders on CPU.
::

::tasks
[ ] Marcus to date the KR3 coverage floor X
[x] Confirm A-L1 retention assumption at the 2.2 sub-gate
::

```python
def faithfulness(summary, sources):
    return score(summary, sources) >= THRESHOLD
```
