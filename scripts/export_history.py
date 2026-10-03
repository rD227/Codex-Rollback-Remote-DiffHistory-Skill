#!/usr/bin/env python3
"""Export recorded Codex FileChange events without exporting other log content."""

import argparse
from collections import OrderedDict
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def session_metadata(path):
    with path.open(encoding="utf-8") as stream:
        for _ in range(10):
            line = stream.readline()
            if not line:
                break
            row = json.loads(line)
            if row.get("type") == "session_meta":
                return row.get("payload", {})
    raise ValueError(f"Session metadata not found: {path}")


def log_files(codex_home):
    for name in ("sessions", "archived_sessions"):
        root = codex_home / name
        if root.is_dir():
            yield from root.rglob("rollout-*.jsonl")


def select_session(args, codex_home):
    if args.session:
        path = Path(args.session).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Session log does not exist: {path}")
        return path
    thread_id = args.thread_id or os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        raise ValueError("No current thread ID. Use --list-sessions, then --thread-id or --session.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        raise ValueError("Invalid thread ID.")
    matches = [p for p in log_files(codex_home) if p.name.endswith(f"-{thread_id}.jsonl")]
    if len(matches) != 1:
        raise ValueError(f"Expected one log for thread {thread_id}; found {len(matches)}. Use --session.")
    return matches[0].resolve()


def normalize_changes(changes):
    if isinstance(changes, dict):
        entries = changes.items()
    elif isinstance(changes, list):
        entries = ((entry.get("path"), entry) for entry in changes if isinstance(entry, dict))
    else:
        raise ValueError("Unsupported FileChange changes format.")
    result = []
    for path, change in entries:
        if not isinstance(path, str) or not isinstance(change, dict):
            raise ValueError("Malformed FileChange entry.")
        kind = change.get("type", change.get("kind", "unknown"))
        kind_data = kind if isinstance(kind, dict) else {}
        if kind_data:
            kind = kind_data.get("type", "unknown")
        result.append({
            "path": path,
            "kind": str(kind),
            "content": change.get("content"),
            "diff": change.get("unified_diff", change.get("diff")),
            "move_path": change.get("move_path", kind_data.get("move_path")),
        })
    return result


def read_history(path, export_roots=()):
    metadata = session_metadata(path)
    turns = OrderedDict()
    current_turn = None
    timezone_name = None
    seen = set()
    partial_tail = False
    skipped_exports = 0
    working_directory = Path(metadata.get("cwd") or str(Path.home()))

    def is_export(change):
        candidate = Path(change["path"]).expanduser()
        if not candidate.is_absolute():
            candidate = working_directory / candidate
        candidate = candidate.resolve()
        return (candidate.name.startswith("codex-diff-") and candidate.suffix == ".md"
                and any(candidate.is_relative_to(root) for root in export_roots))

    def get_turn(turn_id, stamp=None):
        return turns.setdefault(turn_id, {
            "id": turn_id, "started": stamp, "ended": None,
            "status": "in progress / not recorded", "events": [],
        })

    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                # A running daemon may not have finished appending its last line.
                if not line.endswith("\n"):
                    partial_tail = True
                    break
                raise ValueError(f"Invalid JSON at line {number} in {path}") from error
            payload = row.get("payload") or {}
            stamp = row.get("timestamp")
            if row.get("type") == "turn_context":
                current_turn = payload.get("turn_id") or current_turn
                timezone_name = payload.get("timezone") or timezone_name
                working_directory = Path(payload.get("cwd") or str(working_directory))
                if current_turn:
                    get_turn(current_turn, stamp)
                continue
            if row.get("type") != "event_msg":
                continue
            event_type = payload.get("type")
            turn_id = payload.get("turn_id") or current_turn
            if event_type == "task_started":
                current_turn = turn_id
                if turn_id:
                    get_turn(turn_id)["started"] = payload.get("started_at") or stamp
            elif event_type in ("task_complete", "turn_aborted"):
                if turn_id:
                    turn = get_turn(turn_id, stamp)
                    turn["ended"] = payload.get("completed_at") or stamp
                    turn["status"] = "completed" if event_type == "task_complete" else "aborted"
            elif event_type == "item_completed":
                item = payload.get("item") or {}
                if item.get("type") not in ("FileChange", "fileChange", "file_change"):
                    continue
                turn_id = turn_id or "unattributed"
                event_id = item.get("id")
                if event_id:
                    key = (turn_id, event_id)
                    if key in seen:
                        continue
                    seen.add(key)
                changes = normalize_changes(item.get("changes", {}))
                retained = [change for change in changes if not is_export(change)]
                skipped_exports += len(changes) - len(retained)
                if retained:
                    get_turn(turn_id, stamp)["events"].append({
                        "timestamp": stamp, "status": item.get("status", "not recorded"),
                        "changes": retained,
                    })
    return metadata, list(turns.values()), timezone_name, partial_tail, skipped_exports


def local_zone(name):
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def time_text(value, zone):
    parsed = parse_time(value)
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z") if parsed else "not recorded"


def inline(value):
    value = str(value).replace("\n", " ")
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    marker = "`" * (longest + 1)
    return f"{marker} {value} {marker}"


def diff_text(change):
    if isinstance(change["diff"], str):
        return change["diff"]
    content = change["content"]
    if not isinstance(content, str) or change["kind"] not in ("add", "delete"):
        return None
    lines = content.splitlines()
    path = change["path"]
    if change["kind"] == "add":
        header = ["--- /dev/null", f"+++ {path}", f"@@ -0,0 +1,{len(lines)} @@"]
        prefix = "+"
    else:
        header = [f"--- {path}", "+++ /dev/null", f"@@ -1,{len(lines)} +0,0 @@"]
        prefix = "-"
    result = "\n".join(header + [prefix + line for line in lines])
    if content and not content.endswith("\n"):
        result += "\n\\ No newline at end of file"
    return result


def path_stats(turn):
    kinds = OrderedDict()
    for event in turn["events"]:
        for change in event["changes"]:
            kinds.setdefault(change["path"], []).append(change["kind"])
    temporary = {path for path, operations in kinds.items()
                 if operations[0] == "add" and operations[-1] == "delete"}
    return kinds, temporary


def render(path, metadata, turns, zone, partial_tail, skipped_exports=0):
    changed_turns = [turn for turn in turns if turn["events"]]
    lines = [
        "# Codex Recorded File Changes", "",
        f"Session: {inline(metadata.get('id', metadata.get('session_id', 'unknown')))}",
        f"Session start: {time_text(metadata.get('timestamp'), zone)}",
        f"Exported: {datetime.now(zone).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Working directory: {inline(metadata.get('cwd', 'not recorded'))}",
        f"Source log: {inline(path)}", "",
        "This export contains recorded FileChange events grouped by turn.",
        "It excludes conversation messages, model reasoning and command output.",
        "Edits made through shell commands or deployment scripts may not have FileChange events.",
        "Intermediate edits and temporary files are retained; path counts may exceed the UI's net file count.",
        f"Previous history-export file changes were excluded to avoid recursive growth ({skipped_exports} paths skipped).",
        "The active turn is exported only up to the records available when this script reads the log.",
        "This is a historical record, not a patch to apply or a system backup.", "",
    ]
    if partial_tail:
        lines += ["An incomplete final log line was skipped; export again after it is written.", ""]
    lines += ["## Turn index", "",
              "| Turn | Start | Status | Edit events | Paths touched | Created then deleted |",
              "| --- | --- | --- | ---: | ---: | ---: |"]
    for index, turn in enumerate(changed_turns, 1):
        paths, temporary = path_stats(turn)
        lines.append(f"| {index} | {time_text(turn['started'], zone)} | {turn['status']} | "
                     f"{len(turn['events'])} | {len(paths)} | {len(temporary)} |")
    if not changed_turns:
        lines += ["", "No recorded FileChange events were found in this session."]
    for index, turn in enumerate(changed_turns, 1):
        paths, temporary = path_stats(turn)
        lines += ["", f"## Turn {index}: {time_text(turn['started'], zone)}", "",
                  f"Turn ID: {inline(turn['id'])}", f"Status: {turn['status']}",
                  f"Ended: {time_text(turn['ended'], zone)}", "", "### Files", ""]
        for file_path in paths:
            suffix = " (created and deleted in this turn)" if file_path in temporary else ""
            lines.append(f"- {inline(file_path)}{suffix}")
        for edit_index, event in enumerate(turn["events"], 1):
            lines += ["", f"### Edit {edit_index}: {time_text(event['timestamp'], zone)}", "",
                      f"Recorded execution status: {inline(event['status'])}"]
            for change in event["changes"]:
                lines += ["", f"#### {change['kind']}: {inline(change['path'])}", ""]
                if change["move_path"]:
                    lines += [f"Moved to: {inline(change['move_path'])}", ""]
                diff = diff_text(change)
                if diff is None:
                    lines += ["No textual diff was recorded for this change."]
                    continue
                marker = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", diff)), default=0))
                lines += [marker + "diff", diff.rstrip("\n"), marker]
    return "\n".join(lines) + "\n", changed_turns


def write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".codex-diff-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def prepare_panel_patch(target, content):
    """Stage a native apply_patch payload; do not write the final Markdown yet."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        old_content = target.read_text(encoding="utf-8")
        if old_content == content:
            return None, 0
        delta = list(difflib.unified_diff(old_content.splitlines(), content.splitlines(), n=3, lineterm=""))
        body = ["@@" if line.startswith("@@ ") else line for line in delta[2:]]
        patch = "\n".join(["*** Begin Patch", f"*** Update File: {target}", *body, "*** End Patch"]) + "\n"
    else:
        patch = "\n".join(["*** Begin Patch", f"*** Add File: {target}",
                           *("+" + line for line in content.splitlines()), "*** End Patch"]) + "\n"
    descriptor, name = tempfile.mkstemp(prefix="codex-diff-panel-", suffix=".patch")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(patch)
    return name, len(patch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--session", help="Explicit local rollout JSONL file")
    source.add_argument("--thread-id", help="Session ID; otherwise uses CODEX_THREAD_ID")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    parser.add_argument("--output-dir", default=str(Path.home() / "codexDiffHistory"))
    parser.add_argument("--list-sessions", action="store_true", help="List IDs and metadata without exporting")
    parser.add_argument("--prepare-panel", action="store_true", help="Stage a patch for the native file-edit tool instead of writing the final Markdown")
    args = parser.parse_args()
    codex_home = Path(args.codex_home).expanduser()
    if args.list_sessions:
        sessions = []
        for path in log_files(codex_home):
            meta = session_metadata(path)
            sessions.append({"id": meta.get("id", meta.get("session_id")),
                             "started": meta.get("timestamp"), "cwd": meta.get("cwd"), "path": str(path)})
        print(json.dumps(sorted(sessions, key=lambda entry: entry["started"] or "", reverse=True), ensure_ascii=False, indent=2))
        return
    path = select_session(args, codex_home)
    output_dir = Path(args.output_dir).expanduser().resolve()
    export_roots = {output_dir, (Path.home() / "codexDiffHistory").resolve()}
    metadata, turns, timezone_name, partial_tail, skipped_exports = read_history(path, export_roots)
    thread_id = metadata.get("id", metadata.get("session_id"))
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        raise ValueError("Session metadata has no valid ID.")
    if args.thread_id and args.thread_id != thread_id:
        raise ValueError("Requested thread ID does not match session metadata.")
    if not args.session and not args.thread_id and os.environ.get("CODEX_THREAD_ID") != thread_id:
        raise ValueError("Current thread ID does not match session metadata.")
    started = parse_time(metadata.get("timestamp"))
    if not started:
        raise ValueError("Session metadata has no valid start timestamp.")
    zone = local_zone(timezone_name)
    date = started.astimezone(zone).strftime("%Y%m%d")
    target = output_dir / f"codex-diff-{date}-{thread_id}.md"
    content, changed_turns = render(path, metadata, turns, zone, partial_tail, skipped_exports)
    result = {"output": str(target), "thread_id": thread_id,
              "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
              "turns_with_edits": len(changed_turns),
              "edit_events": sum(len(turn["events"]) for turn in changed_turns),
              "partial_tail_skipped": partial_tail,
              "export_file_changes_skipped": skipped_exports}
    if args.prepare_panel:
        patch_file, patch_characters = prepare_panel_patch(target, content)
        result.update({"patch_file": patch_file, "patch_characters": patch_characters})
    else:
        write_atomic(target, content)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
