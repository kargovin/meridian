# Annotation workspace format

An eval set is cut from an **annotation workspace**: a directory of plain files that
annotators and an adjudicator write, and that `python -m eval.annotate` checks and cuts.
The tool never labels and never adjudicates. Agents or people write the label files, the
adjudicator writes the rulings, and the tool reports what is left and cuts the set once
nothing is.

Real workspaces hold corpus text and live under `eval/workspaces/`, which git ignores. The
hand-built fixture at `eval/fixtures/classification-v2/` is a complete worked example, and
`eval/sets/classification/v2` is its cut.

```
<workspace>/
  workspace.json        what is being labelled, against which definitions
  pool.jsonl            the items, each carrying its own text
  participants.json     who labels and who adjudicates
  assignments.jsonl     which annotators label which item
  labels/<who>.jsonl    one file per annotator, as given
  adjudication.jsonl    the rulings
```

Every file is UTF-8. A `.jsonl` file holds one JSON object per line. **Unknown keys are
refused, not ignored**: a misspelled key that is dropped means the file says something
other than what was meant, and nothing reports it.

## workspace.json

```json
{"format": 1, "task": "classification", "taxonomy": "v1", "rubric_rev": 2,
 "description": "optional; copied into the set's manifest"}
```

| Key | Meaning |
| --- | --- |
| `format` | The layout version. This document describes `1`. |
| `task` | `classification`. Faithfulness is the second task; its item and label shapes are its own. |
| `taxonomy` | Classification only. The topic set labels are drawn from; must be the harness's. |
| `rubric_rev` | Classification only. The labelling rubric revision annotators followed. |

## pool.jsonl

One item per line. For classification:

```json
{"id": "c-0001", "title": "…", "lede": "…", "body": null, "publisher": "NPR",
 "origin": {"article_id": 7277, "url": "https://…"}}
```

- `id` is the item's own, **never an article id**. An id made only of digits is refused.
  A set carries its own text, so it scores the same after the database changes.
- `title` and `publisher` are required. `lede` and `body` may be `null`, and
  whitespace-only text counts as absent.
- `origin` is optional and free-form: where the item came from, so the adjudicator can open
  the source. **It is stripped at the cut.** It never reaches a set.

## participants.json

```json
{"participants": [
  {"id": "agent-a", "kind": "agent", "model": "…", "instructions_sha256": "<64 hex>"},
  {"id": "adjudicator", "kind": "human"}
]}
```

- `id` is lower-case letters, digits, `-` and `_`. It names a file under `labels/`.
- An agent declares the model it ran and the SHA-256 of the instructions it was given.
  `check` warns when two agents share both: two copies of one configuration are one
  annotator run twice, and agreement between them measures nothing.
- Adjudicators are declared here too, because a ruling names who made it.

## assignments.jsonl

```json
{"id": "c-0001", "annotators": ["agent-a", "agent-b"]}
```

Every pool item appears exactly once, with one or more declared annotators. An item with
two annotators or more is double-labelled. `check` warns below the task's share: 10% for
classification (rubric §8.1).

## labels/&lt;who&gt;.jsonl

One file per annotator, named after their participant id. The labels are kept exactly as
given, and they are what agreement is computed on.

```json
{"id": "c-0001", "label": "business"}
{"id": "c-0002", "label": "unsure", "unsure_reason": "collision_without_rule",
 "detail": {"candidates": ["business", "technology"]}}
```

- `label` is a topic, or `unsure`. `unsure` is an annotator's answer, never a label a set
  can hold.
- `unsure_reason` is required with `unsure` and forbidden without it. For classification it
  is one of the three occasions rubric §7.2 gives:
  - `collision_without_rule`: two topics fit and no straddle rule decides.
  - `rule_does_not_fit`: a rule names the collision but the article does not fit its terms.
  - `unrecoverable_record`: the record has no content to read.
- `detail` is optional and task-defined. For classification it is any JSON object, carried
  into the raw labels as given.
- An annotator labels only the items assigned to them, and each item once.

## adjudication.jsonl

```json
{"id": "c-0002", "by": "adjudicator", "outcome": "gold", "gold": "technology",
 "note": "optional"}
{"id": "c-0003", "by": "adjudicator", "outcome": "drop", "cause": "genuine_disagreement"}
```

- `by` is a declared participant. One adjudicator is enough for any ruling, drops included.
- `outcome: gold` sets the gold label. It is never `unsure`.
- `outcome: drop` takes the item out of the set, with a `cause` from the task's closed set.
  For classification (rubric §7.5):
  - `genuine_disagreement`: the adjudicator read it and the definitions do not decide it.
    A rise in this means two topic definitions are blurring.
  - `unrecoverable_record`: there is nothing to read. A rise in this means acquisition is
    failing.
- One ruling per item.

## States

Each item's state is worked out from the files on every run. No state is stored, so none can
go stale.

| State | When |
| --- | --- |
| `unlabelled` | An assigned annotator has not labelled it. This comes first, whatever has been ruled. |
| `awaiting_ruling` | It is contested and has no ruling. Contested means an annotator said `unsure`, or the annotators disagree. |
| `agreed` | It is uncontested and has no ruling. The shared label is gold. |
| `adjudicated` | It is ruled to a gold label. |
| `dropped` | It is ruled out of the set, with a cause. |

A ruling on an uncontested item is an **override**. It is allowed, because the adjudicator
may see what every annotator missed. It is also counted, because a set where many agreed
labels were rewritten measures the adjudicator, not the annotators.

## Commands

```sh
python -m eval.annotate check <workspace>      # 0 ready · 2 work left · 1 malformed
python -m eval.annotate queue <workspace>      # labels and rulings owed; --json for one task per line
python -m eval.annotate cut   <workspace> classification/v3 --jira MER-30
python -m eval.annotate fetch classification/v3
```

`queue --json` emits two kinds of task. A `label` task carries the item, with no other
annotator's answer and no origin, so the annotator labels blind and from the record. An
`adjudicate` task carries the item, its origin, and every label given.

`cut` refuses while any item is `unlabelled` or `awaiting_ruling`. It writes
`eval/sets/<task>/vN/`, never over an existing version:

- `rows.jsonl`: the agreed and adjudicated items, with gold. The origin is stripped.
- `raw_labels.jsonl`: every candidate, dropped ones included, with its labels as given, its
  outcome, its gold or drop cause, its adjudicator, and whether the ruling was an override.
- `manifest.json`: the hashes of both files, the candidate count, drops by cause (every
  cause, zero included), adjudicated and override counts, the participants, and the MLflow
  run.

By default the cut also records an MLflow run with the two set files and the workspace, and
it needs `MLFLOW_TRACKING_URI` and `--jira`. Only the manifest is committed. On another
checkout, `fetch` downloads the rows and raw labels from that run, verifies each against the
manifest's hash, and puts them in place. `--no-upload` is for hand-built fixtures, which
ship whole.

## What the harness does with a cut set

`eval/run.py` refuses a set whose rows still carry `unsure`, a manifest without drop counts,
and a candidate count that does not equal the rows plus the drops. On every run it reports
and logs the drop rate by cause beside KR3, and the rows it excluded from KR3 by reason. A
cause above 3% is tagged `drop_rate_review` on the run. The run is not failed: the harness
reports, it never asserts.
