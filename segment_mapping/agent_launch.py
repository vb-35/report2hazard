"""Unattended free agent runs: hand the mapping prompt to Claude Code or Codex.

The agent works in the project folder with the same toolkit and prompt as a manual run. Everything the launcher
records goes to RUN/agent/: the prompt, the exact command, the event transcript, the final message and a summary.
Both CLIs use their own sign-in (a Claude or ChatGPT subscription, or an API key configured for the CLI).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"`{name}` was not found on PATH; install it and sign in first.")
    return path


def _outside_project(*paths: Path) -> list[Path]:
    folders = []
    for path in paths:
        if not path.is_relative_to(PROJECT) and path not in folders:
            folders.append(path)
    return folders


def build_command(a, run_dir: Path, report: Path, last_message: Path) -> tuple[list[str], dict[str, str]]:
    """The agent CLI command and the environment changes it needs. The prompt is sent on stdin."""
    env: dict[str, str] = {}
    if a.runner == "claude":
        command = [_executable("claude"), "-p", "--output-format", "stream-json", "--verbose",
                   "--no-session-persistence",
                   "--permission-mode", "bypassPermissions" if a.full_access else "auto"]
        # The run folder must be writable and the report readable. Only the run folder itself is granted, not its
        # parent, which may hold other runs. (Shell commands are governed by the permission mode, not by these.)
        for folder in _outside_project(run_dir, report.parent):
            command += ["--add-dir", str(folder)]
        if a.model:
            command += ["--model", a.model]
        if a.effort:
            command += ["--effort", a.effort]
        # Blind runs: no project memory or CLAUDE.md notes about earlier runs.
        env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", CLAUDE_CODE_DISABLE_CLAUDE_MDS="1")
    else:
        command = [_executable("codex"), "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                   "-C", str(PROJECT), "-o", str(last_message)]
        if a.full_access:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            # Writes stay inside the project (and the run folder); downloads need network access.
            command += ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
            for folder in _outside_project(run_dir):
                command += ["--add-dir", str(folder)]
        if a.model:
            command += ["-m", a.model]
        if a.effort:
            command += ["-c", f"model_reasoning_effort={a.effort}"]
        command.append("-")
    return command, env


def _final_text(runner: str, line: dict) -> str | None:
    if runner == "claude" and line.get("type") == "result":
        return line.get("result")
    return None


def _progress(runner: str, line: dict) -> str | None:
    """A short console line for an event, or None."""
    kind = line.get("type", "")
    if runner == "claude":
        if kind == "assistant":
            for block in (line.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    detail = block.get("input") or {}
                    return f"tool {block.get('name')}: {str(detail.get('command') or detail.get('file_path') or '')[:140]}"
                if block.get("type") == "text" and block.get("text", "").strip():
                    return "says: " + block["text"].strip().splitlines()[0][:140]
        if kind == "result":
            return f"finished: {line.get('subtype')} after {line.get('num_turns')} turns"
        return None
    item = line.get("item") or {}
    if kind == "item.completed" and item.get("type") == "command_execution":
        return f"command: {str(item.get('command', ''))[:140]} (exit {item.get('exit_code')})"
    if kind == "item.completed" and item.get("type") == "agent_message":
        return "says: " + str(item.get("text", "")).strip().splitlines()[0][:140] if item.get("text") else None
    if kind in {"turn.completed", "turn.failed", "error"}:
        return kind
    return None


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def stop_process(process) -> None:
    """Stop an agent and the processes it started (a shell command it is running, a Node child of codex.cmd)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.terminate()


def launch(a, prompt: str, *, log=_stderr, on_start=None) -> dict:
    """Run the agent; returns a summary (also saved as RUN/agent/agent_run.json) with exit_code.

    log receives short progress lines; on_start receives the process (for stop_process).
    """
    run_dir = Path(a.run).resolve()
    report = Path(a.report).resolve()
    if (run_dir / "run.json").exists():
        raise SystemExit(f"{run_dir} already contains a run; use a new folder per model/run.")
    log_dir = run_dir / "agent"
    if log_dir.exists() and any(log_dir.iterdir()) and not a.dry_run:
        raise SystemExit(f"{log_dir} already holds an agent run; use a new run folder.")
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    last_message = log_dir / "final_message.txt"
    command, env_changes = build_command(a, run_dir, report, last_message)
    summary = {"runner": a.runner, "model": a.model or "CLI default", "effort": a.effort or "CLI default",
               "full_access": bool(a.full_access), "cwd": str(PROJECT), "command": command, "env": env_changes,
               "prompt": str(log_dir / "prompt.txt")}
    if a.dry_run:
        _stderr(json.dumps(summary, indent=2))
        summary["exit_code"] = 0
        return summary
    if a.full_access:
        log("WARNING: full access: the agent runs without a sandbox or permission checks.")
    env = {k: v for k, v in os.environ.items() if k not in {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}}
    if a.runner == "claude":
        # Free runs use the Claude Code sign-in (subscription). An API key in the environment, e.g. the one the
        # interface uses for its Anthropic provider, would take precedence and bill the run to that key instead.
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(name, None)
    env.update(env_changes)
    summary["started"] = _now()
    transcript = log_dir / "transcript.jsonl"
    final = None
    with transcript.open("w", encoding="utf-8") as out:
        process = subprocess.Popen(command, cwd=PROJECT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            if on_start is not None:
                on_start(process)
            log(f"{a.runner} started (process {process.pid})")
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # the agent exited at once (bad flag, no sign-in): its output below says why
            for raw in process.stdout:
                out.write(raw)
                out.flush()
                try:
                    line = json.loads(raw)
                except ValueError:
                    if raw.strip():
                        log(raw.rstrip()[:200])
                    continue
                if not isinstance(line, dict):
                    continue
                final = _final_text(a.runner, line) or final
                note = _progress(a.runner, line)
                if note:
                    log(f"[{a.runner}] {note}")
        except KeyboardInterrupt:
            stop_process(process)
            summary["interrupted"] = True
        except BaseException:
            # Never leave an unsupervised agent behind when logging or recording fails.
            stop_process(process)
            process.wait()
            raise
        code = process.wait()
    if final and not last_message.exists():
        last_message.write_text(final, encoding="utf-8")
    summary.update(finished=_now(), exit_code=code, transcript=str(transcript),
                   run_created=(run_dir / "run.json").exists(),
                   delivered=(run_dir / "outputs" / "segments_map.html").exists())
    (log_dir / "agent_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"Agent finished with exit code {code}. Log: {log_dir}")
    return summary
