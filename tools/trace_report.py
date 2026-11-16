"""
trace_report.py - Requirements traceability matrix: requirement -> tests -> result -> evidence.

Reads   spec/requirements.yaml and reports/req_results.json (written by the test run)
Writes  reports/traceability.html and reports/traceability.md
Exit 1  if any active requirement is not verified, or a test links to an unknown requirement.

Usage:  python -m tools.trace_report
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "requirements.yaml"
REPORTS = ROOT / "reports"
RESULTS = REPORTS / "req_results.json"

STATUS_STYLE = {
    "VERIFIED": ("#e6f4ea", "#1e6b34"),
    "FAILED": ("#fce8e6", "#a50e0e"),
    "KNOWN GAP": ("#fef7e0", "#7a5900"),
    "NOT TESTED": ("#f1f3f4", "#3c4043"),
}
TEST_ICON = {"passed": "✔", "failed": "✘", "known_gap": "◐", "xpassed": "✘", "skipped": "–"}


def requirement_status(req: dict, tests: list[dict]) -> str:
    outcomes = {t["status"] for t in tests}
    if not tests or outcomes <= {"skipped"}:
        return "NOT TESTED"
    if outcomes & {"failed", "xpassed"}:
        return "FAILED"
    if "known_gap" in outcomes:
        return "KNOWN GAP"
    return "VERIFIED"


def build(spec_doc: dict, results_doc: dict) -> tuple[list[dict], list[str]]:
    reqs = spec_doc["requirements"]
    known_ids = {r["id"] for r in reqs}
    problems = []
    for t in results_doc["results"]:
        for rid in t["requirements"]:
            if rid not in known_ids:
                problems.append(f"{t['test']} links to unknown requirement {rid}")

    rows = []
    for r in reqs:
        tests = [t for t in results_doc["results"] if r["id"] in t["requirements"]]
        status = requirement_status(r, tests)
        rows.append({**r, "tests": tests, "verdict": status})
        if r.get("status", "active") == "active" and status != "VERIFIED":
            problems.append(f"{r['id']} ({r['title']}) is active but {status}")
        if r.get("status") == "open" and status == "VERIFIED":
            problems.append(f"{r['id']} now passes - change its status from open to active")
    return rows, problems


def write_markdown(rows, meta, problems, path: Path) -> None:
    lines = [f"# Traceability matrix - {meta['document']}", "",
             f"Firmware **{meta['firmware']}** · calibration **{meta['cal']}** · "
             f"commit `{meta['commit']}` · generated {meta['generated']}", "",
             "| Requirement | Title | Safety | Verdict | Tests |",
             "|---|---|---|---|---|"]
    for r in rows:
        tests = "<br>".join(f"{TEST_ICON.get(t['status'], '?')} `{t['test'].split('::', 1)[-1]}`"
                            for t in r["tests"]) or "—"
        lines.append(f"| {r['id']} | {r['title']} | {'yes' if r.get('safety') else 'no'} | "
                     f"**{r['verdict']}** | {tests} |")
    if problems:
        lines += ["", "## Gate findings", *[f"- {p}" for p in problems]]
    path.write_text("\n".join(lines) + "\n")


def write_html(rows, meta, problems, counts, path: Path) -> None:
    e = html.escape
    body = []
    for r in rows:
        bg, fg = STATUS_STYLE[r["verdict"]]
        test_html = []
        for t in r["tests"]:
            name = e(t["test"].split("::", 1)[-1])
            meas = "".join(f"<li>{e(m['label'])}: <b>{m['value']:g}</b> {e(m['unit'])}"
                           f"{' <span class=lim>(' + e(m['limit']) + ')</span>' if m['limit'] else ''}</li>"
                           for m in t["measured"])
            ev = " ".join(f"<a href='{e(p)}'>plot</a>" for p in t["evidence"])
            msg = f"<div class=msg>{e(t['message'])}</div>" if t["message"] else ""
            test_html.append(f"<div class=test><span class='ic {t['status']}'>"
                             f"{TEST_ICON.get(t['status'], '?')}</span> <code>{name}</code> {ev}"
                             f"{msg}<ul>{meas}</ul></div>")
        body.append(
            f"<tr><td><b>{e(r['id'])}</b><div class=cat>{e(r['category'])}"
            f"{' · safety' if r.get('safety') else ''}</div></td>"
            f"<td><b>{e(r['title'])}</b><div class=txt>{e(r['text'])}</div></td>"
            f"<td><span class=pill style='background:{bg};color:{fg}'>{r['verdict']}</span></td>"
            f"<td>{''.join(test_html) or '<span class=none>no linked test</span>'}</td></tr>")

    gate = ("<div class='gate ok'>Gate passed: every active requirement is verified.</div>"
            if not problems else
            "<div class='gate bad'><b>Gate failed</b><ul>"
            + "".join(f"<li>{e(p)}</li>" for p in problems) + "</ul></div>")
    cards = "".join(f"<div class=card><div class=n>{v}</div><div>{k}</div></div>"
                    for k, v in counts.items())

    path.write_text(f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Traceability matrix</title><style>
body{{font:14px/1.5 system-ui,sans-serif;margin:24px;color:#202124;background:#fff}}
h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#5f6368;margin-bottom:16px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}}
.card{{border:1px solid #dadce0;border-radius:10px;padding:10px 16px;min-width:110px}}
.card .n{{font-size:22px;font-weight:600}}
table{{border-collapse:collapse;width:100%}} td,th{{border-top:1px solid #e8eaed;padding:10px;vertical-align:top;text-align:left}}
th{{font-size:12px;color:#5f6368;text-transform:uppercase}}
.cat,.txt,.lim{{color:#5f6368;font-size:12px}} .txt{{max-width:420px}}
.pill{{padding:3px 10px;border-radius:12px;font-weight:600;font-size:12px;white-space:nowrap}}
.test{{margin-bottom:8px}} .test ul{{margin:2px 0 0 18px;padding:0;font-size:12px}}
.ic{{font-weight:700}} .passed{{color:#1e6b34}} .failed,.xpassed{{color:#a50e0e}} .known_gap{{color:#7a5900}}
.msg{{color:#a50e0e;font-size:12px}} .none{{color:#a50e0e}}
.gate{{padding:10px 14px;border-radius:8px;margin:12px 0}} .ok{{background:#e6f4ea}} .bad{{background:#fce8e6}}
code{{font-size:12px}}
</style></head><body>
<h1>Traceability matrix</h1>
<div class=meta>{e(meta['document'])} · firmware {e(meta['firmware'])} · calibration {e(meta['cal'])}
· DBC {e(meta['dbc'])} · commit {e(meta['commit'])} · {e(meta['generated'])}</div>
<div class=cards>{cards}</div>{gate}
<table><tr><th>Requirement</th><th>Description</th><th>Verdict</th><th>Tests, measurements and evidence</th></tr>
{''.join(body)}</table></body></html>""")


def main() -> int:
    if not RESULTS.exists():
        print(f"{RESULTS} not found - run the test suite first (python -m pytest)")
        return 1
    spec_doc = yaml.safe_load(SPEC.read_text())
    results_doc = json.loads(RESULTS.read_text())
    rows, problems = build(spec_doc, results_doc)

    cal = results_doc.get("calibration", {})
    meta = {
        "document": f"{spec_doc['id']} v{spec_doc['version']}",
        "firmware": results_doc.get("firmware_version", "?"),
        "cal": f"{cal.get('name', '?')} v{cal.get('version', '?')} ({cal.get('sha256', '')[:10]})",
        "dbc": results_doc.get("dbc_sha256", "")[:10],
        "commit": results_doc.get("git_commit", "unknown")[:10],
        "generated": results_doc.get("generated_utc", ""),
    }
    verdicts = [r["verdict"] for r in rows]
    counts = {"requirements": len(rows), **{v.lower(): verdicts.count(v) for v in STATUS_STYLE}}

    write_html(rows, meta, problems, counts, REPORTS / "traceability.html")
    write_markdown(rows, meta, problems, REPORTS / "traceability.md")

    for r in rows:
        print(f"{r['id']:8} {r['verdict']:11} {r['title']}")
    print("\n" + "  ".join(f"{k}: {v}" for k, v in counts.items()))
    for p in problems:
        print(f"GATE: {p}")
    print(f"\nWrote {REPORTS / 'traceability.html'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
