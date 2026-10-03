# Codex Diff History

A local Codex skill that exports recorded file edits by conversation turn to Markdown. It helps review earlier changes when the remote client's current diff panel shows only the latest turn.

## Install

Clone this repository into your personal skills directory:

```bash
git clone https://github.com/rD227/Codex-Rollback-Remote-DiffHistory-Skill.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/codex-diff-history"
```

Keep an existing installation until you have reviewed any local changes. The `.git` directory can stay alongside `SKILL.md` for version control.

## Use in Codex

Send `$codex-diff-history`, or ask "Export Codex diff history" / "导出 Codex 改动历史".

- Exports go to `~/codexDiffHistory/codex-diff-<session-start-date>-<session-id>.md`.
- Repeated exports update the same session's file; other sessions retain their own files.
- On a remote client, Codex attempts to publish the Markdown with its native file-edit tool so the current turn's changed-files panel can display it.
- If panel publication is unavailable, the file is still saved and the reply includes a link and summary. The client controls whether the panel appears.
- Changes to exported history files are excluded from subsequent exports to prevent recursive growth.

## Command line

Requires Python 3.9 or later; the exporter uses only the standard library.

```bash
python3 scripts/export_history.py                         # Current CODEX_THREAD_ID
python3 scripts/export_history.py --list-sessions
python3 scripts/export_history.py --thread-id SESSION_ID
python3 scripts/export_history.py --session /path/to/rollout.jsonl
python3 scripts/export_history.py --output-dir /path/to/history
```

`--prepare-panel` stages a temporary patch for Codex's native `apply_patch` tool. See [panel delivery](references/panel-delivery.md) for publication, verification, cleanup and fallback.

## Scope

The exporter reads local Codex session logs and extracts recorded `FileChange` events, preserving turn IDs, execution status, temporary edits and textual diffs. It excludes conversation messages, model reasoning and command output. Writes made by shell commands or deployment scripts may have no corresponding file-edit event.

An active session is exported up to the records available when the log is read. Export again to include later edits. The log format is a local implementation detail and can change between Codex versions.

Exports may contain intermediate or reversed edits. Review them as historical records; use a separate, deliberate restore procedure to roll back files. This skill only exports history and does not restore files or provide a system backup.

New export directories are created with mode `0700`; normal exports use mode `0600`. Panel delivery also sets the saved file to `0600` after verification.
