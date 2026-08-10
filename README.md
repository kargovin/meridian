# Meridian News

A news aggregator — ingest multi-source news, classify into topics, cluster related articles, summarize each cluster — built on a reusable **Summarization & Classification Platform**.

**All inference is local.** Self-hosted models only (HuggingFace transformers, ONNX Runtime, Triton); no external inference APIs. That constraint is deliberate and program-wide, not an implementation detail.

## Status

**Phase 4 — Build, sprint 1** (10–21 Aug 2026). Phases 0–3 are closed and their decisions are frozen.

There is **no product code yet**. This first commit is the design record and the Confluence toolchain that produced it. Code starts at MER-14 (relational schema + migrations).

## What this repository is

This repository holds **code**. The design record — business case, PRD, architecture RFC, ML approach & model-selection study, serving/infra sizing, delivery plan — lives in Confluence, where each phase was gated and signed off before the next opened. The decisions it produced are what the code is built against, and the table below is the short form of them.

## Design decisions

| Area | Decision |
|---|---|
| Language | Python 3.12, both deployables |
| Web | FastAPI + Uvicorn; the published contract is generated from Pydantic |
| Storage | PostgreSQL 16; SQLAlchemy 2.x + Alembic |
| Work queue | `pipeline_state` on `CanonicalRecord`, claimed with `SELECT … FOR UPDATE SKIP LOCKED`. No broker |
| Scheduling | APScheduler in-process; poll cadence is config, never a constant |
| Classification | Fine-tuned DeBERTa-v3-base, CPU, temperature scaling |
| Clustering | SimHash near-dup → BGE-small embeddings → online leader-follower + batch reconciliation. No vector DB, no ANN |
| Summarization | Not pre-selected — decided by a bake-off against our own eval set |
| Faithfulness | AlignScore vs SummaC; a failed check withholds the summary |
| Serving | Triton-class multi-backend, CPU-first. Production needs no GPU |
| Tooling | uv workspaces · ruff + mypy · pytest |

## Architecture

A **two-deployable split**: a stateless Platform service (classify / summarize / faithfulness), and the Meridian app as a modular-monolith consumer. The only cross-deployable hops are the two `/v1` calls, and the Digest team consumes the same endpoints.

![Meridian News end-to-end data flow](rfc-2.2-dataflow.svg)

The write path runs on a freshness budget measured in minutes. The read path is a pre-computed projection — **the reader surface triggers no model at all**, which is what keeps an interactive page off the inference plane.

## Layout

```text
meridian/              # the product app — modular monolith
  ingest/              # discovery, acquisition, normalization
  dedup/               # SimHash near-dup, collapse-not-drop
  cluster/             # leader-follower + reconciliation
  readmodel/           # inline projection + reader surface
  platform_client/     # generated from libs/contract
platform/              # the stateless Platform service
  api/                 # FastAPI app; the v0 → v1 contract surface
  models/              # classify / summarize / faithfulness
libs/
  contract/            # shared Pydantic schema — source of the published contract
  config/              # pinned embedder version, poll cadence, API limits
eval/                  # evaluation harness — deliberately outside both deployables
migrations/            # Alembic
tools/                 # Confluence authoring toolchain
```

The `eval/` harness is a **separate system from the tests**. It reports; it does not assert.

Deployment manifests live in a second repository (`kargovin/govindappa-k8s-config`) and are reconciled by Flux onto a single-node k3s cluster. This repository answers *what does the code do*; that one answers *what is running*.

## Tooling

`tools/confluence/` publishes phase documents to Confluence from Markdown — the renderer (`adf.py`) targets Atlassian Document Format directly, so document bodies are authored as Markdown rather than hand-written markup.
