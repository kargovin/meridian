# Confluence authoring toolchain

Stop hand-writing XHTML/ADF. Author phase docs as Markdown in `docs/`, render
deterministically, push over REST. The page body never enters model context.

## Why

| Route | What the model emits for a 60k-char page | Notes |
|---|---|---|
| MCP `updateConfluencePage` with HTML | ~65k chars markup | inline in a tool call; the whole body is retyped for *any* edit |
| Raw ADF JSON | ~135k chars | worst case; JSON overhead is ~2.2× the prose |
| **This toolchain** | **~28k chars Markdown** | ~5× cheaper than ADF, and native nodes are guaranteed correct |
| **Editing an existing doc** | **~200 chars** | Edit tool touches only changed lines, then re-push |

That last row is the real win. Revisions stop costing a full re-emission.

## Use

```sh
python3 tools/confluence/push.py docs/phase-3-sprint-plan.md            # dry run: census + size
python3 tools/confluence/push.py docs/phase-3-sprint-plan.md --publish  # write + verify
```

Frontmatter picks the target: `page_id` updates, `parent_id` creates. After a
write, push re-fetches and diffs the node-type census against what was sent —
a mismatch means Confluence rewrote something and needs a look.

⚠️ **Quote any frontmatter value containing `: `** — most of our titles do
(`title: "Phase 3 — Delivery Plan: epic breakdown…"`). `parse()` splits on the
first colon and is happy either way, but editors and CI that run a real YAML
parser reject the unquoted form as a nested mapping. `parse()` strips one layer
of matching quotes, so quoting never reaches the page title or version message.

`adf.py` is also importable for structural patching of pages that are *not* yet
mirrored locally (`adf.census`, `adf.inline`, `adf.render`).

## Format

See `sample.md` — it exercises every supported node. Beyond CommonMark:

- `::doc-control` … `::` — the label/value document-control header
- `:::panel info|note|warning|success|error` … `:::` — panels, containing blocks
- `::decisions` … `::` — decision lists · `::tasks` … `::` with `[ ]`/`[x]`
- `{status:green|APPROVED}` — status lozenge · `@2026-07-21` — native date node

## Not covered

`layoutSection`, `expand`, media/attachments, inline comments. Pages using
those (the PRD has one `layoutSection`) should keep using structural ADF
patching for now — see the `reference-atlassian-rest` memory.

## Migration stance

New docs are authored here from the start. Existing Confluence pages stay
canonical and get patched structurally; convert one to a local `.md` only when
it needs a rewrite big enough to pay for the conversion, and verify by diffing
the rendered census against the live page first.
