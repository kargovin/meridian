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

Every file is UTF-8. A `.jsonl` file holds one JSON object per line, and lines end at `\n`
alone. U+2028, U+2029 and U+0085 inside a string are text, not line breaks, so a file
written with `ensure_ascii=False` is read correctly. **Unknown keys are
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
- Adjudicators are declared here too, as `human`, because a ruling names who made it.

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

- `by` is a declared `human` participant, and not one of the item's annotators. Agents
  annotate and a person adjudicates (rubric §7.3); an annotator ruling on its own item would
  settle every disagreement in its own favour. One adjudicator is enough for any ruling,
  drops included.
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

A ruling that changes the outcome of an uncontested item, a drop or a gold label other than
the agreed one, is an **override**. It is allowed, because the adjudicator may see what every
annotator missed. It is also counted, because a set where many agreed labels were rewritten
measures the adjudicator, not the annotators. A ruling that repeats the agreed label is a
confirmation and is not counted.

A ruling on an item that is still owed a label is kept, because an unrecoverable record may
be dropped early, but `check` warns: the ruling was made without that label, and it stands
whatever the label says.

## Commands

```sh
python -m eval.annotate check <workspace>      # 0 ready · 3 work left · 1 malformed
python -m eval.annotate queue <workspace>      # labels and rulings owed; --json for one task per line
python -m eval.annotate cut   <workspace> classification/v3 --jira MER-nn
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
manifest's hash, and moves them into place one file at a time. If the set then fails to load,
the fetched files are removed. `--no-upload` needs two things: a workspace under
`eval/fixtures/`, and a set name whose `rows.jsonl` and `raw_labels.jsonl` `.gitignore` both
exempts, which git is asked. Any
other set's rows are git-ignored, and without the upload they would exist nowhere else.

## What the harness does with a cut set

`eval/run.py` refuses a set whose rows still carry `unsure`, a manifest without drop counts,
a candidate count that does not equal the rows plus the drops, and raw labels that disagree
with either. That covers their hash, an item recorded twice, their drops by cause, the
items they record as kept, and each kept item's gold label. On every run it reports
and logs the drop rate by cause beside KR3, and the rows it excluded from KR3 by reason. A
cause above 3% is tagged `drop_rate_review` on the run. The run is not failed: the harness
reports, it never asserts.
