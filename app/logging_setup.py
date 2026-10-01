"""
Centralized logging configuration.

Per the 2026-05-22 call with the client: every long-running component
(daily sync, scheduler, trigger worker, ad-hoc scripts) needs to capture all
events -- successes, failures, API connections, errors -- to a persistent
on-disk folder so we can troubleshoot after the fact.

What this module gives you:

  configure_run_logging(component, run_id=None) -> RunLogPaths

    Creates a per-run set of log files under:
        logs/<component>/<YYYYMMDD>/

    Three files per run:
      - <run_id>.log         All events at the configured level (human-readable)
      - <run_id>_errors.log  WARNING and above only -- scan this first
      - <run_id>_api.jsonl   Structured JSON Lines for every API call (the
                             GoDaddyClient.audit_hook writes here)

    Also keeps a stdout handler so live runs / CI workflow logs still print
    everything in real time.

    Purges run subdirectories older than `retention_days` (default 30) on
    each invocation so we don't fill the disk.

Usage from a script:

    from app.logging_setup import configure_run_logging
    paths = configure_run_logging("sync")
    logger = logging.getLogger(__name__)
    logger.info("hello world")
    # ... paths.audit_jsonl is the path to feed into GoDaddyClient.audit_hook

The output looks like:

    logs/
      sync/
        20260522/
          sync_104412.log
          sync_104412_errors.log
          sync_104412_api.jsonl
      scheduler/
        20260522/
          scheduler_080000.log
          ...

Operational note: logs/ is gitignored, but the daily-sync GitHub Actions
workflow uploads the entire logs/ tree as an artifact on every run so we
have a forensic trail even when the run happened in CI.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# Default location for logs. Resolved relative to the repo root since most
# callers invoke scripts from there.
DEFAULT_LOG_ROOT = Path("logs")

# How long to keep per-run log subdirectories before deletion.
DEFAULT_RETENTION_DAYS = 30


@dataclass
class RunLogPaths:
    """File paths created by configure_run_logging(). Hand the audit_jsonl
    path to GoDaddyClient.audit_hook to wire the API trail."""

    run_id: str
    component: str
    base_dir: Path
    full_log: Path
    errors_log: Path
    audit_jsonl: Path


def configure_run_logging(
    component: str,
    run_id: Optional[str] = None,
    *,
    level: str = "INFO",
    log_root: Path = DEFAULT_LOG_ROOT,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    also_stdout: bool = True,
) -> RunLogPaths:
    """Set up logging for a long-running component.

    component:        short label used as the subdirectory, e.g. "sync",
                      "scheduler", "trigger". Use lowercase, no spaces.
    run_id:           identifier for this specific invocation. Default is
                      "<component>_<HHMMSS>" using the start time.
    level:            root logger level. DEBUG / INFO / WARNING / ERROR.
    log_root:         where to put the logs/ tree. Default: ./logs/
    retention_days:   purge run subdirectories older than this many days.
                      Set to 0 to disable purging (do that in tests).
    also_stdout:      keep a stdout handler. Default True so CI captures it.

    Returns RunLogPaths so the caller can hand audit_jsonl to a writer.
    """
    started = datetime.now(timezone.utc)
    date_dir = started.strftime("%Y%m%d")

    if run_id is None:
        run_id = f"{component}_{started.strftime('%H%M%S')}"

    base = Path(log_root) / component / date_dir
    base.mkdir(parents=True, exist_ok=True)

    full_log = base / f"{run_id}.log"
    errors_log = base / f"{run_id}_errors.log"
    audit_jsonl = base / f"{run_id}_api.jsonl"

    # Build handler list.
    handlers: list[logging.Handler] = []

    # Full-detail file handler.
    full_handler = logging.FileHandler(full_log, encoding="utf-8")
    full_handler.setLevel(getattr(logging, level.upper()))
    full_handler.setFormatter(_human_formatter())
    handlers.append(full_handler)

    # Errors-only file handler -- WARNING and above. The first place to
    # look when "something went wrong".
    err_handler = logging.FileHandler(errors_log, encoding="utf-8")
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(_human_formatter())
    handlers.append(err_handler)

    # Stdout. Default on so CI logs (GH Actions) still see live output.
    if also_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(getattr(logging, level.upper()))
        stream.setFormatter(_human_formatter())
        handlers.append(stream)

    # Install. force=True replaces any earlier basicConfig() in the process.
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    root = logging.getLogger()
    root.info(
        "logging configured | component=%s run_id=%s level=%s | "
        "full=%s errors=%s audit=%s",
        component, run_id, level, full_log, errors_log, audit_jsonl,
    )

    # Touch the audit JSONL so the file exists even if no API calls happen
    # in this run. Makes the artifact upload predictable.
    audit_jsonl.touch()

    # Retention purge AFTER we've created today's dir, so we don't whack it.
    if retention_days and retention_days > 0:
        _purge_old_runs(Path(log_root) / component, retention_days)

    return RunLogPaths(
        run_id=run_id,
        component=component,
        base_dir=base,
        full_log=full_log,
        errors_log=errors_log,
        audit_jsonl=audit_jsonl,
    )


def _human_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _purge_old_runs(component_dir: Path, retention_days: int) -> None:
    """Delete date subdirectories older than retention_days.

    We use mtime of the directory itself rather than parsing the YYYYMMDD
    name -- handles future renames / partial fills gracefully.
    """
    if not component_dir.exists() or not component_dir.is_dir():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for child in component_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                shutil.rmtree(child)
            except OSError:
                # Best-effort. Log via root logger; don't crash the caller.
                logging.getLogger(__name__).warning(
                    "failed to purge old log dir %s", child
                )


# ---------------------------------------------------------------------------
# Audit trail writer for the GoDaddyClient.audit_hook
# ---------------------------------------------------------------------------


def make_audit_jsonl_writer(audit_path: Path, max_body_chars: int = 4000):
    """Build an async callable that writes one JSONL row per API call to
    `audit_path`. Plug directly into GoDaddyClient(audit_hook=...).

    Each row captures (UTC): timestamp, method, url, status_code, request_body,
    response_body, response_size_bytes. Bodies are truncated to max_body_chars
    so a giant XML/JSON payload doesn't bloat the log.

    Safe to call when audit_path's parent directory might not exist yet --
    we create it lazily.
    """
    audit_path = Path(audit_path)

    async def _hook(method, url, status_code, request_body, response_body):
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "url": url,
                "status_code": status_code,
                "request_body": _truncate(request_body, max_body_chars),
                "response_body": _truncate(response_body, max_body_chars),
                "response_size_bytes": len(response_body) if response_body else 0,
            }
            # Append a single line. Open/close per write keeps things simple
            # at the cost of a little syscall overhead -- we're not in a
            # hot path here.
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            # Never crash the API call because logging failed.
            logging.getLogger(__name__).warning(
                "audit hook write failed: %s", e
            )

    return _hook


def _truncate(s, n: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else (s[:n] + f"...<truncated {len(s) - n} bytes>")
