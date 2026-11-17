"""
review_draft.py - Human review of LLM-drafted tests (see tools/draft_test.py).

    python -m tools.review_draft --list
    python -m tools.review_draft --accept REQ-016 [--edited] [--reviewer NAME]
    python -m tools.review_draft --reject REQ-016 [--reason "..."]
    python -m tools.review_draft --stats

--accept  moves tests/drafts/test_<req>_draft.py to tests/test_<req>.py, records the
          review in its header, sets review_status "accepted" in reports/drafter_log.jsonl
          ("edited" with --edited, or automatically when the file differs from what the
          drafter wrote), and switches the requirement from open to active.
--reject  deletes the draft and records "rejected".
Every command prints the acceptance rate: (accepted + edited) / reviewed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from tools.draft_test import DRAFT_BANNER, DRAFTS, LOG, ROOT, SPEC, TESTS, draft_path, req_slug

REVIEWED = ("accepted", "edited", "rejected")


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #


def read_log(log: Path = LOG) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_log(entries: list[dict], log: Path = LOG) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def pending_entry(entries: list[dict], req_id: str) -> dict | None:
    for e in reversed(entries):
        if e.get("req") == req_id and e.get("review_status") == "pending":
            return e
    return None


def acceptance_rate(entries: list[dict]) -> tuple[float | None, dict[str, int]]:
    counts = {s: sum(e.get("review_status") == s for e in entries)
              for s in ("pending", *REVIEWED, "gate_failed")}
    reviewed = sum(counts[s] for s in REVIEWED)
    rate = (counts["accepted"] + counts["edited"]) / reviewed if reviewed else None
    return rate, counts


def rate_line(entries: list[dict]) -> str:
    rate, c = acceptance_rate(entries)
    pct = "n/a (nothing reviewed yet)" if rate is None else f"{rate:.0%}"
    return (f"Acceptance rate: {pct}  -  accepted {c['accepted']}, edited {c['edited']}, "
            f"rejected {c['rejected']}, pending {c['pending']}, gate failures {c['gate_failed']}")


# --------------------------------------------------------------------------- #
# Spec status
# --------------------------------------------------------------------------- #


def activate_requirement(req_id: str, spec_path: Path = SPEC) -> bool:
    """Switches `status: open` to `status: active` in the requirement's block, keeping
    the rest of the file (comments, layout) untouched. Returns True if it changed."""
    lines = spec_path.read_text(encoding="utf-8").splitlines(keepends=True)
    inside = False
    for i, line in enumerate(lines):
        m = re.match(r"\s*-\s+id:\s*(\S+)", line)
        if m:
            inside = m.group(1) == req_id
            continue
        if inside and re.match(r"\s+status:\s*open\s*$", line):
            lines[i] = line.replace("open", "active", 1)
            spec_path.write_text("".join(lines), encoding="utf-8")
            return True
    return False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _git_user() -> str:
    try:
        return subprocess.run(["git", "config", "user.name"], capture_output=True, text=True,
                              cwd=ROOT, check=True).stdout.strip() or "unknown"
    except Exception:   # noqa: BLE001 - git missing or not configured
        return "unknown"


def accepted_path(req_id: str, tests_dir: Path = TESTS) -> Path:
    return tests_dir / f"test_{req_slug(req_id)}.py"


def accept(req_id: str, *, edited: bool = False, reviewer: str | None = None,
           drafts_dir: Path = DRAFTS, tests_dir: Path = TESTS, log: Path = LOG,
           spec_path: Path = SPEC, out=print) -> Path:
    src = draft_path(req_id, drafts_dir)
    dst = accepted_path(req_id, tests_dir)
    if not src.exists():
        raise SystemExit(f"No draft for {req_id} at {src}")
    if dst.exists():
        raise SystemExit(f"{dst} already exists - merge the draft by hand")

    entries = read_log(log)
    entry = pending_entry(entries, req_id)
    text = src.read_text(encoding="utf-8")
    changed = bool(entry and entry.get("draft_sha256")
                   and hashlib.sha256(text.encode("utf-8")).hexdigest() != entry["draft_sha256"])
    if changed and not edited:
        out(f"{req_id}: the draft was modified after drafting - recording it as 'edited'")
    status = "edited" if (edited or changed) else "accepted"
    reviewer = reviewer or _git_user()
    stamp = _now()

    note = f"# Reviewed: {status} by {reviewer} on {stamp}"
    text = (text.replace(f"# {DRAFT_BANNER}", note, 1) if f"# {DRAFT_BANNER}" in text
            else f"{note}\n{text}")
    dst.write_text(text, encoding="utf-8")
    src.unlink()

    rel = dst.relative_to(ROOT).as_posix() if dst.is_relative_to(ROOT) else str(dst)
    if entry is None:
        out(f"{req_id}: no pending log entry found - adding one")
        entry = {"req": req_id, "model": None, "gates_passed": None, "timestamp": stamp}
        entries.append(entry)
    entry.update(review_status=status, reviewer=reviewer, reviewed_utc=stamp, file=rel)
    write_log(entries, log)

    if activate_requirement(req_id, spec_path):
        out(f"{req_id}: status open -> active in {spec_path.name}")
    out(f"{req_id}: {status}, moved to {rel}")
    out("    next: python -m pytest && python -m tools.trace_report")
    out(rate_line(entries))
    return dst


def reject(req_id: str, *, reason: str = "", reviewer: str | None = None,
           drafts_dir: Path = DRAFTS, log: Path = LOG, out=print) -> None:
    src = draft_path(req_id, drafts_dir)
    if not src.exists():
        raise SystemExit(f"No draft for {req_id} at {src}")
    src.unlink()
    entries = read_log(log)
    entry = pending_entry(entries, req_id)
    stamp = _now()
    if entry is None:
        entry = {"req": req_id, "model": None, "gates_passed": None, "timestamp": stamp, "file": None}
        entries.append(entry)
    entry.update(review_status="rejected", reviewer=reviewer or _git_user(), reviewed_utc=stamp,
                 reason=reason)
    write_log(entries, log)
    out(f"{req_id}: rejected, draft deleted")
    out(rate_line(entries))


def list_pending(drafts_dir: Path = DRAFTS, log: Path = LOG, out=print) -> None:
    drafts = sorted(drafts_dir.glob("test_*_draft.py")) if drafts_dir.exists() else []
    if not drafts:
        out("No drafts waiting for review.")
    for p in drafts:
        head = p.read_text(encoding="utf-8").splitlines()[:9]
        meta = {m.group(1): m.group(2) for line in head
                if (m := re.match(r"#\s*([a-z_0-9 ]+):\s*(.*)", line))}
        out(f"{p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else p}: "
            f"{meta.get('requirement', '?')}, model {meta.get('model', '?')}, "
            f"pytest {meta.get('pytest result', '?')}")
    out(rate_line(read_log(log)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--accept", metavar="REQ", help="accept the draft for REQ into tests/")
    act.add_argument("--reject", metavar="REQ", help="delete the draft for REQ")
    act.add_argument("--list", action="store_true", help="list drafts waiting for review")
    act.add_argument("--stats", action="store_true", help="print the acceptance rate")
    ap.add_argument("--edited", action="store_true", help="the reviewer changed the draft")
    ap.add_argument("--reviewer", help="reviewer name (default: git user.name)")
    ap.add_argument("--reason", default="", help="why the draft was rejected")
    args = ap.parse_args(argv)

    if args.accept:
        accept(args.accept, edited=args.edited, reviewer=args.reviewer)
    elif args.reject:
        reject(args.reject, reason=args.reason, reviewer=args.reviewer)
    elif args.list:
        list_pending()
    else:
        print(rate_line(read_log()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
