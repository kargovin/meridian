"""The annotation command line: check a workspace, list what is left, cut a set, fetch one.

    python -m eval.annotate check  <workspace>
    python -m eval.annotate queue  <workspace> [--json]
    python -m eval.annotate cut    <workspace> <task>/v<N> --jira MER-nn [--no-upload]
    python -m eval.annotate fetch  <task>/v<N>

``check`` exits 0 when the workspace is ready to cut, 3 when it is valid but labelling or
adjudication is left, and 1 when a file is malformed. (2 is argparse's usage error, so a
mistyped command never reads as work left.)

A real set's rows and raw labels are annotated corpus text: ``cut`` uploads them to MLflow
and only the manifest is committed. ``fetch`` restores them on another checkout, verified
against the manifest's hashes. ``--no-upload`` is for a hand-built fixture under
``eval/fixtures/``, which ships whole; any other workspace is refused it, because its rows
would be git-ignored and recorded nowhere else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from eval.annotation import (
    Cut,
    CutRefused,
    Item,
    State,
    Workspace,
    WorkspaceError,
    cut,
    open_workspace,
    workspace_files,
    write_cut,
)
from eval.evalset import (
    DEFAULT_ROOT,
    MANIFEST_FILE,
    RAW_LABELS_FILE,
    ROWS_FILE,
    EvalSetError,
    load,
)
from eval.run import git_ignores, provenance, require_tracking_uri, without_credentials

DEFAULT_EXPERIMENT = "meridian-eval-sets"

#: Workspaces whose cut may skip MLflow: hand-built fixtures, whose set is committed whole.
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

#: ``check``'s exit status for a valid workspace with labelling or adjudication left.
EXIT_WORK_LEFT = 3

#: Where a cut set's files sit inside its MLflow run.
SET_ARTIFACTS = "set"
WORKSPACE_ARTIFACTS = "workspace"


def summary(workspace: Workspace) -> str:
    items = workspace.items
    counts = {state: len(workspace.in_state(state)) for state in State}
    drops = ", ".join(f"{cause} {n}" for cause, n in workspace.drops.items())
    share = workspace.double_labelled / len(items)
    meta = ", ".join(f"{key} {workspace.meta[key]}" for key in workspace.task.meta_keys)
    lines = [
        f"workspace        {workspace.path}  ({workspace.task.name}; {meta})",
        f"items            {len(items)}  ({workspace.double_labelled} double-labelled, "
        f"{share:.1%})",
        f"unlabelled       {counts[State.UNLABELLED]}",
        f"awaiting ruling  {counts[State.AWAITING_RULING]}",
        f"agreed           {counts[State.AGREED]}",
        f"adjudicated      {counts[State.ADJUDICATED]}",
        f"dropped          {counts[State.DROPPED]}  ({drops})",
        f"overrides        {workspace.overrides}",
        "",
        "ready to cut" if workspace.ready else "not ready to cut — `queue` lists what is left",
    ]
    lines.extend(f"warning: {warning}" for warning in workspace.warnings())
    return "\n".join(lines)


def _item_json(item: Item, *, with_origin: bool) -> dict[str, object]:
    out: dict[str, object] = {"id": item.id, **item.fields}
    if with_origin:
        out["origin"] = item.origin
    return out


def queue_entries(workspace: Workspace) -> list[dict[str, object]]:
    """What is left, as tasks: labels owed by each annotator, then rulings owed.

    A labelling task carries the item and nothing about other annotators' answers, so an
    annotator labels blind; nor its origin, since an annotator labels from the record. A
    ruling task carries both, because the adjudicator reads the disagreement and may open
    the source (rubric §7.3).
    """
    out: list[dict[str, object]] = []
    for item in workspace.in_state(State.UNLABELLED):
        answered = {label.annotator for label in item.labels}
        for annotator in item.annotators:
            if annotator not in answered:
                out.append(
                    {
                        "task": "label",
                        "annotator": annotator,
                        "item": _item_json(item, with_origin=False),
                    }
                )
    for item in workspace.in_state(State.AWAITING_RULING):
        out.append(
            {
                "task": "adjudicate",
                "item": _item_json(item, with_origin=True),
                "labels": [label.as_json() for label in item.labels],
            }
        )
    return out


def _excerpt(value: object, limit: int = 240) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(raw.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_queue(entries: Sequence[Mapping[str, object]]) -> str:
    if not entries:
        return "nothing left: every item is labelled and every contested item is ruled on"
    owed: dict[str, list[str]] = {}
    rulings: list[Mapping[str, object]] = []
    for entry in entries:
        item = entry["item"]
        assert isinstance(item, dict)
        if entry["task"] == "label":
            owed.setdefault(str(entry["annotator"]), []).append(str(item["id"]))
        else:
            rulings.append(entry)
    lines: list[str] = []
    if owed:
        lines.append(f"labelling — {sum(len(ids) for ids in owed.values())} label(s) owed")
        lines.extend(f"  {who:<16} {' '.join(ids)}" for who, ids in sorted(owed.items()))
    if rulings:
        if lines:
            lines.append("")
        lines.append(f"adjudication — {len(rulings)} item(s) awaiting a ruling")
        for entry in rulings:
            item = entry["item"]
            labels = entry["labels"]
            assert isinstance(item, dict) and isinstance(labels, list)
            lines.append(f"  {item['id']}  {item.get('title', '')}")
            for key, value in item.items():
                if key not in ("id", "title") and value is not None:
                    lines.append(f"      {key}: {_excerpt(value)}")
            for label in labels:
                reason = f" ({label['unsure_reason']})" if label["unsure_reason"] else ""
                lines.append(f"      {label['annotator']:<16} {label['label']}{reason}")
                if label["detail"] is not None:
                    lines.append(f"      {'':<16} detail: {_excerpt(label['detail'])}")
    return "\n".join(lines)


def upload(result: Cut, workspace: Workspace, *, experiment: str, jira: str) -> dict[str, str]:
    """Record the cut as an MLflow run: the set's files, the workspace it came from, and
    the drop counts. Returns what the manifest records to find the run again."""
    import mlflow

    require_tracking_uri()
    mlflow.set_experiment(experiment)
    with tempfile.TemporaryDirectory() as tmp, mlflow.start_run() as active:
        staged = Path(tmp)
        (staged / ROWS_FILE).write_bytes(result.rows)
        (staged / RAW_LABELS_FILE).write_bytes(result.raw_labels)
        mlflow.set_tags({**provenance(), "jira": jira, "eval_set": result.name})
        manifest = result.manifest
        mlflow.log_params(
            {
                key: str(manifest[key])
                for key in ("set", "format", "task", "sha256", "raw_labels_sha256")
            }
            | {"tracking_uri": without_credentials(mlflow.get_tracking_uri())}
        )
        drops = manifest["drops"]
        assert isinstance(drops, dict)
        mlflow.log_metrics(
            {
                "candidates": float(str(manifest["candidates"])),
                "row_count": float(str(manifest["row_count"])),
                "overrides": float(str(manifest["overrides"])),
                **{f"drops.{cause}": float(n) for cause, n in drops.items()},
            }
        )
        mlflow.log_artifact(str(staged / ROWS_FILE), artifact_path=SET_ARTIFACTS)
        mlflow.log_artifact(str(staged / RAW_LABELS_FILE), artifact_path=SET_ARTIFACTS)
        # The workspace too: it is the only record of a dropped item's text and origin, and
        # the input a later re-adjudication would start from.
        for path in workspace_files(workspace):
            relative = path.relative_to(workspace.path).parent
            target = WORKSPACE_ARTIFACTS + ("" if relative == Path() else f"/{relative}")
            mlflow.log_artifact(str(path), artifact_path=target)
        return {"experiment": experiment, "run_id": active.info.run_id}


def fetch(name: str, root: Path) -> Path:
    """Restore a set's rows and raw labels from the MLflow run its manifest names.

    Each file is verified against the manifest's hash before it is put in place, so a run
    whose artifacts were replaced cannot restore a set that then loads under the old name.
    """
    import mlflow

    base = root / name
    manifest_path = base / MANIFEST_FILE
    if not manifest_path.is_file():
        raise EvalSetError(f"{name}: {manifest_path} does not exist")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvalSetError(f"{name}: {MANIFEST_FILE} is not valid JSON — {exc}") from exc
    run = manifest.get("mlflow") if isinstance(manifest, dict) else None
    if not isinstance(run, dict) or not isinstance(run.get("run_id"), str):
        raise EvalSetError(
            f"{name}: the manifest names no MLflow run, so there is nothing to fetch. Only a "
            "hand-built fixture is cut that way, and its rows are committed."
        )
    require_tracking_uri()
    expected = {
        ROWS_FILE: manifest.get("sha256"),
        RAW_LABELS_FILE: manifest.get("raw_labels_sha256"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        fetched: dict[str, Path] = {}
        placed: list[Path] = []
        for filename, digest in expected.items():
            try:
                local = Path(
                    mlflow.artifacts.download_artifacts(
                        run_id=run["run_id"],
                        artifact_path=f"{SET_ARTIFACTS}/{filename}",
                        dst_path=tmp,
                    )
                )
            except mlflow.exceptions.MlflowException as exc:
                raise EvalSetError(
                    f"{name}: could not download {filename} from run {run['run_id']} — {exc}"
                ) from exc
            actual = hashlib.sha256(local.read_bytes()).hexdigest()
            if actual != digest:
                raise EvalSetError(
                    f"{name}: {filename} in run {run['run_id']} does not match the manifest "
                    f"(manifest {digest}, run {actual})"
                )
            present = base / filename
            if present.exists() and hashlib.sha256(present.read_bytes()).hexdigest() != digest:
                raise EvalSetError(
                    f"{name}: a different {filename} is already in {base}; move it aside first"
                )
            fetched[filename] = local
        try:
            for filename, local in fetched.items():
                target = base / filename
                if target.exists():
                    continue
                # Copied beside the target and renamed into place, so an interrupted fetch
                # never leaves a partial file that the next fetch refuses as "different".
                partial = base / f".{filename}.partial"
                shutil.copyfile(local, partial)
                os.replace(partial, target)
                placed.append(target)
            load(name, root=root)
        except BaseException:
            for path in (*placed, *(base / f".{f}.partial" for f in fetched)):
                path.unlink(missing_ok=True)
            raise
    return base


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check, queue, cut and fetch annotation sets.")
    commands = parser.add_subparsers(dest="command", required=True)

    check_cmd = commands.add_parser("check", help="validate a workspace and report its state")
    check_cmd.add_argument("workspace", type=Path)

    queue_cmd = commands.add_parser("queue", help="list the labels and rulings still owed")
    queue_cmd.add_argument("workspace", type=Path)
    queue_cmd.add_argument("--json", action="store_true", help="one JSON task per line")

    cut_cmd = commands.add_parser("cut", help="cut a finished workspace into a versioned set")
    cut_cmd.add_argument("workspace", type=Path)
    cut_cmd.add_argument("set", help="the set's name, <task>/v<N>")
    cut_cmd.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="where sets live")
    cut_cmd.add_argument("--jira", help="the ticket the set is cut for; required to upload")
    cut_cmd.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    cut_cmd.add_argument(
        "--no-upload",
        action="store_true",
        help="write the set without recording it in MLflow (hand-built fixtures only)",
    )

    fetch_cmd = commands.add_parser("fetch", help="restore a set's rows from MLflow")
    fetch_cmd.add_argument("set")
    fetch_cmd.add_argument("--root", type=Path, default=DEFAULT_ROOT)

    args = parser.parse_args(argv)

    try:
        if args.command == "fetch":
            print(f"restored {fetch(args.set, args.root)}")
            return 0

        workspace = open_workspace(args.workspace)

        if args.command == "check":
            print(summary(workspace))
            return 0 if workspace.ready else EXIT_WORK_LEFT

        if args.command == "queue":
            entries = queue_entries(workspace)
            if args.json:
                for entry in entries:
                    print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            else:
                print(format_queue(entries))
            return 0

        result = cut(workspace, args.set)
        if (args.root / args.set).exists():
            # Checked before the upload as well as at the write, so a refused cut leaves no
            # orphan run behind in MLflow.
            raise CutRefused(f"{args.root / args.set} already exists; cut a new version")
        run = None
        if args.no_upload:
            # Both, because the workspace decides what the set holds and the set's name
            # decides whether git keeps its rows: a fixture cut to an ignored name would
            # exist nowhere but this disk.
            if not args.workspace.resolve().is_relative_to(FIXTURES_ROOT):
                raise CutRefused(
                    f"--no-upload is for hand-built fixtures under {FIXTURES_ROOT}; any other "
                    "set's rows would exist nowhere but this disk."
                )
            # Both files: the harness refuses a set without its raw labels, and with no
            # MLflow run there is nowhere to fetch either from.
            for filename in (ROWS_FILE, RAW_LABELS_FILE):
                path = (args.root / args.set / filename).resolve()
                if git_ignores(path) is not False:
                    raise CutRefused(
                        f"--no-upload needs a set whose {ROWS_FILE} and {RAW_LABELS_FILE} the "
                        f"repository commits, and {path} is git-ignored or outside this "
                        "checkout. Add both to .gitignore's fixture exceptions, or upload it."
                    )
        if not args.no_upload:
            if not args.jira:
                raise CutRefused("--jira is required to upload: a run nobody can attribute is lost")
            run = upload(result, workspace, experiment=args.experiment, jira=args.jira)
        target = write_cut(result, args.root, mlflow_run=run)
        manifest = result.manifest
        print(f"cut {target}  ({manifest['row_count']} rows of {manifest['candidates']})")
        if run is not None:
            print(f"recorded as MLflow run {run['run_id']} in {run['experiment']}")
        for warning in workspace.warnings():
            print(f"warning: {warning}", file=sys.stderr)
        return 0
    except WorkspaceError as exc:
        print(f"error: {len(exc.problems)} problem(s) in the workspace", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    except (CutRefused, EvalSetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
