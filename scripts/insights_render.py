"""HTML renderer for Enclave Insights — self-contained, no external assets."""

from __future__ import annotations

import html
from collections import Counter
from typing import Any


def _esc(s: Any) -> str:
    return html.escape(str(s))


def _bar_rows(counter: Counter, top: int = 12, color: str = "#7c5cff") -> str:
    items = counter.most_common(top)
    if not items:
        return '<p class="muted">No data.</p>'
    mx = max(v for _, v in items) or 1
    rows = []
    for name, val in items:
        pct = round(val / mx * 100)
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{_esc(name)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{pct}%;'
            f'background:{color}"></span></span><span class="bar-val">{val}</span></div>'
        )
    return "\n".join(rows)


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def render_html(data: dict, narrative: dict | None = None) -> str:
    narrative = narrative or {}
    tc = data["tool_counts"]
    tf = data["tool_fail"]
    langs = data["lang_counts"]
    sessions = data["by_session"]
    scope = data["session_filter"] or "all sessions"

    total_tools = sum(tc.values())
    total_fail = sum(tf.values())
    fail_rate = round(total_fail / total_tools * 100, 1) if total_tools else 0
    total_commits = sum(s["commits"] for s in sessions.values())
    total_done = sum(s["done"] for s in sessions.values())
    total_bugs = sum(s["bugs"] for s in sessions.values())

    # ── At a glance ──
    glance = f"""
    <div class="stats">
      <div class="stat"><div class="n">{len(sessions)}</div><div class="l">Sessions</div></div>
      <div class="stat"><div class="n">{data['user_msgs']}</div><div class="l">Your messages</div></div>
      <div class="stat"><div class="n">{total_tools:,}</div><div class="l">Tool calls</div></div>
      <div class="stat"><div class="n">{total_commits}</div><div class="l">Commits</div></div>
      <div class="stat"><div class="n">{total_done}</div><div class="l">Tasks completed</div></div>
      <div class="stat"><div class="n">{fail_rate}%</div><div class="l">Tool failure rate</div></div>
      <div class="stat"><div class="n">{_fmt_secs(data['median_response_s'])}</div><div class="l">Median reply time</div></div>
      <div class="stat"><div class="n">{_fmt_dt(data['first_ts'])}<br>→ {_fmt_dt(data['last_ts'])}</div><div class="l">Span</div></div>
    </div>"""

    # ── Correction clusters → base-prompt candidates (the headline) ──
    clusters = data.get("correction_clusters", [])
    if clusters:
        crows = []
        for c in clusters:
            ex = "".join(f'<li>{_esc(e)}</li>' for e in c["examples"])
            crows.append(
                f'<div class="cluster"><div class="cluster-head">'
                f'<span class="cluster-topic">{_esc(c["topic"])}</span>'
                f'<span class="cluster-count">{c["count"]}× corrected</span></div>'
                f'<ul class="cluster-ex">{ex}</ul></div>'
            )
        corr_html = "\n".join(crows)
    else:
        corr_html = '<p class="muted">No recurring correction patterns detected.</p>'

    prompt_candidates = narrative.get("prompt_candidates") or _fallback_candidates(clusters)
    pc_html = "".join(
        f'<li><code>{_esc(p)}</code></li>' for p in prompt_candidates
    ) or '<li class="muted">None suggested.</li>'

    # ── Per-session table ──
    srows = []
    for sess, s in sorted(sessions.items(), key=lambda kv: -kv[1]["tool_total"]):
        srows.append(
            f"<tr><td>{_esc(sess)}</td><td>{s['user_msgs']}</td>"
            f"<td>{s['tool_total']}</td><td>{s['tool_fail']}</td>"
            f"<td>{s['commits']}</td><td>{s['done']}</td>"
            f"<td>{s['interesting']}</td><td>{s['corrections']}</td></tr>"
        )

    # ── Interesting moments (sampled) ──
    moments = data["interesting"][:18]
    mhtml = "".join(
        f'<div class="moment"><span class="moment-sess">{_esc(m["session"])}</span>'
        f'{_esc(m["text"][:240])}</div>' for m in moments
    ) or '<p class="muted">None found.</p>'

    # ── Narrative sections (optional) ──
    nar_html = ""
    if narrative.get("sections"):
        for title, body in narrative["sections"]:
            nar_html += f'<section class="card"><h2>{_esc(title)}</h2><div class="prose">{_md(body)}</div></section>'

    return _TEMPLATE.format(
        scope=_esc(scope),
        generated=_now(),
        glance=glance,
        narrative=nar_html,
        corrections=corr_html,
        prompt_candidates=pc_html,
        tools=_bar_rows(tc, 14),
        failures=_bar_rows(tf, 8, "#e05555"),
        langs=_bar_rows(langs, 8, "#4caf7a"),
        hours=_hour_chart(data["hour_hist"]),
        session_rows="\n".join(srows),
        moments=mhtml,
    )


def _fallback_candidates(clusters: list[dict]) -> list[str]:
    """Deterministic base-prompt suggestions from correction topics."""
    mapping = {
        "scope / over-reach": "Touch only the files/scope I name; don't refactor or change adjacent code unless asked.",
        "model selection": "Use the model I specified for delegated work; don't switch models without confirming.",
        "verification / testing": "Compile/run/verify changes before presenting them as done.",
        "destructive / safety": "List any kill/rm/destructive command and wait for confirmation before running it.",
        "commit / git workflow": "Follow the established git workflow (branch off trunk, small commits) without prompting.",
        "assumptions / verify-first": "Verify a field/API/version exists (headers or web) before asserting it does or doesn't.",
        "communication / verbosity": "Keep responses concise; lead with the result, details on request.",
        "restart / deploy hygiene": "After code changes that need a restart/rebuild, note it explicitly and apply it.",
    }
    return [mapping[c["topic"]] for c in clusters if c["topic"] in mapping]


def _hour_chart(hist: Counter) -> str:
    if not hist:
        return '<p class="muted">No data.</p>'
    mx = max(hist.values()) or 1
    cols = []
    for h in range(24):
        v = hist.get(h, 0)
        ht = round(v / mx * 100)
        cls = "hot" if 6 <= h < 18 else ""
        cols.append(
            f'<div class="hour-col"><div class="hour-bar {cls}" style="height:{ht}%" '
            f'title="{h:02d}:00 — {v} msgs"></div><div class="hour-lbl">{h if h%6==0 else ""}</div></div>'
        )
    return f'<div class="hours">{"".join(cols)}</div>'


def _fmt_secs(s: float) -> str:
    s = int(s)
    if s <= 0:
        return "—"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m {s%60}s"
    return f"{s//3600}h"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _md(text: str) -> str:
    """Minimal markdown: paragraphs, bold, inline code, bullet lists."""
    out, in_list = [], False
    for line in (text or "").split("\n"):
        ln = line.rstrip()
        s = _esc(ln.strip())
        s = __import__("re").sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = __import__("re").sub(r"`(.+?)`", r"<code>\1</code>", s)
        if ln.strip().startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{s[2:]}</li>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            if s:
                out.append(f"<p>{s}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enclave Insights — {scope}</title>
<style>
  :root {{ --bg:#0f0f14; --card:#1a1a22; --border:#2a2a36; --text:#e6e6ee;
           --muted:#8a8a9a; --accent:#7c5cff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,
          BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:980px; margin:0 auto; padding:2rem 1.2rem 4rem; }}
  h1 {{ font-size:1.8rem; margin:0 0 .2rem; }}
  .sub {{ color:var(--muted); margin:0 0 1.5rem; font-size:.9rem; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px;
           padding:1.2rem 1.4rem; margin:1rem 0; }}
  .card h2 {{ margin:0 0 .8rem; font-size:1.15rem; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:.8rem; }}
  .stat {{ background:var(--card); border:1px solid var(--border); border-radius:10px;
           padding:.8rem; text-align:center; }}
  .stat .n {{ font-size:1.4rem; font-weight:700; color:var(--accent); }}
  .stat .l {{ font-size:.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; margin-top:.2rem; }}
  .bar-row {{ display:flex; align-items:center; gap:.6rem; margin:.25rem 0; font-size:.85rem; }}
  .bar-label {{ width:130px; flex-shrink:0; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .bar-track {{ flex:1; height:14px; background:#23232e; border-radius:7px; overflow:hidden; }}
  .bar-fill {{ display:block; height:100%; border-radius:7px; }}
  .bar-val {{ width:48px; text-align:right; color:var(--muted); font-variant-numeric:tabular-nums; }}
  .cluster {{ border:1px solid var(--border); border-radius:8px; padding:.7rem .9rem; margin:.6rem 0; background:#16161d; }}
  .cluster-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:.4rem; }}
  .cluster-topic {{ font-weight:600; }}
  .cluster-count {{ font-size:.75rem; color:var(--accent); }}
  .cluster-ex {{ margin:.2rem 0 0; padding-left:1.1rem; color:var(--muted); font-size:.82rem; }}
  .cluster-ex li {{ margin:.15rem 0; }}
  .candidates {{ list-style:none; padding:0; margin:0; }}
  .candidates li {{ margin:.4rem 0; }}
  .candidates code {{ display:block; background:#16161d; border:1px solid var(--border);
                      border-left:3px solid var(--accent); border-radius:6px; padding:.5rem .7rem;
                      font-size:.82rem; white-space:normal; color:var(--text); }}
  table {{ width:100%; border-collapse:collapse; font-size:.82rem; }}
  th,td {{ text-align:left; padding:.4rem .5rem; border-bottom:1px solid var(--border); }}
  th {{ color:var(--muted); font-weight:600; }}
  td:first-child {{ font-family:ui-monospace,monospace; font-size:.78rem; }}
  .moment {{ border-left:2px solid var(--accent); padding:.35rem .7rem; margin:.4rem 0;
             background:#16161d; border-radius:0 6px 6px 0; font-size:.83rem; }}
  .moment-sess {{ display:inline-block; color:var(--accent); font-size:.72rem;
                  font-family:ui-monospace,monospace; margin-right:.5rem; }}
  .hours {{ display:flex; align-items:flex-end; gap:3px; height:90px; }}
  .hour-col {{ flex:1; display:flex; flex-direction:column; align-items:center; height:100%; }}
  .hour-bar {{ width:70%; background:#3a3a4a; border-radius:3px 3px 0 0; margin-top:auto; min-height:2px; }}
  .hour-bar.hot {{ background:var(--accent); }}
  .hour-lbl {{ font-size:.6rem; color:var(--muted); margin-top:2px; }}
  .prose p {{ margin:.5rem 0; }} .prose code {{ background:#16161d; padding:.05rem .3rem; border-radius:4px; }}
  .muted {{ color:var(--muted); }}
  .two {{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; }}
  @media(max-width:680px) {{ .two {{ grid-template-columns:1fr; }} .bar-label {{ width:90px; }} }}
</style></head><body><div class="wrap">
  <h1>✦ Enclave Insights</h1>
  <p class="sub">{scope} · generated {generated}</p>
  {glance}
  {narrative}
  <section class="card">
    <h2>🔁 Patterns you repeatedly correct</h2>
    <p class="muted" style="font-size:.85rem;margin-top:-.3rem">Recurring redirections, clustered by topic — candidates for the base prompt.</p>
    {corrections}
    <h2 style="margin-top:1.2rem">📌 Suggested base-prompt additions</h2>
    <ul class="candidates">{prompt_candidates}</ul>
  </section>
  <div class="two">
    <section class="card"><h2>🔧 Top tools</h2>{tools}</section>
    <section class="card"><h2>❌ Tool failures</h2>{failures}</section>
  </div>
  <div class="two">
    <section class="card"><h2>📝 Languages / file types</h2>{langs}</section>
    <section class="card"><h2>🕐 Activity by hour (UTC)</h2>{hours}</section>
  </div>
  <section class="card"><h2>✨ Highlight moments</h2>{moments}</section>
  <section class="card"><h2>📊 Per-session</h2>
    <table><thead><tr><th>Session</th><th>Msgs</th><th>Tools</th><th>Fails</th>
    <th>Commits</th><th>Done</th><th>Wins</th><th>Corrections</th></tr></thead>
    <tbody>{session_rows}</tbody></table>
  </section>
</div></body></html>"""
