#!/usr/bin/env python3
"""kagi — query the Kagi Search API for high-quality, rankable result URLs.

This is a thin convenience wrapper around the Kagi Search API (v1). Its purpose
is *not* to answer questions, but to surface real primary-source URLs from
Kagi's index, which you then fetch and quote verbatim. Treat the URLs as the
trustworthy output; do not treat any summarised prose as a citation.

Usage:
    kagi <query...>
    kagi --limit 5 <query...>
    kagi --json <query...>            # raw JSON for scripting

Requires the KAGI_TOKEN environment variable (injected by the orchestrator when
a Kagi token is configured). The token is never printed.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://kagi.com/api/v1/search"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kagi",
        description="Search the web via the Kagi Search API (returns ranked URLs).",
    )
    parser.add_argument("query", nargs="+", help="Search query")
    parser.add_argument("-n", "--limit", type=int, default=10,
                        help="Max results to show (default: 10)")
    parser.add_argument("--json", action="store_true",
                        help="Print the raw JSON response")
    args = parser.parse_args()

    token = os.environ.get("KAGI_TOKEN", "").strip()
    if not token:
        print("error: KAGI_TOKEN is not set — Kagi search is not configured for "
              "this session.", file=sys.stderr)
        return 2

    query = " ".join(args.query).strip()
    if not query:
        print("error: empty query", file=sys.stderr)
        return 2

    # The v1 Search API is POST + Bearer auth with a JSON body. Pass `limit`
    # so Kagi caps the result set server-side (valid range is 1-1024).
    server_limit = max(1, min(1024, args.limit))
    body = json.dumps(
        {"query": query, "workflow": "search", "limit": server_limit}
    ).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    def _extract_errors(obj: dict) -> str | None:
        # The Search API uses "error" (singular array) for 4xx bodies, while
        # some responses (e.g. ip_not_allowed) use "errors" (plural). Handle both.
        errs = obj.get("error") or obj.get("errors")
        if not errs:
            return None
        if isinstance(errs, list):
            return "; ".join(str(x.get("message") or x.get("code") or x) for x in errs)
        return str(errs)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
        msg = body_text[:500]
        # Kagi returns structured errors; surface the message if present.
        try:
            parsed = _extract_errors(json.loads(body_text))
            if parsed:
                msg = parsed
        except Exception:
            pass
        print(f"error: Kagi API returned HTTP {e.code}: {msg}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"error: could not reach Kagi API: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON from Kagi API: {e}", file=sys.stderr)
        return 1

    # Surface API-level errors returned alongside a 200 status.
    err_msg = _extract_errors(payload)
    if err_msg:
        print(f"error: Kagi API error: {err_msg}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    data = payload.get("data") or {}
    results = (data.get("search") or [])[: args.limit]
    related = [r.get("title") for r in (data.get("related_search") or []) if r.get("title")]

    if not results:
        print("No results.", file=sys.stderr)
    for i, r in enumerate(results, 1):
        title = html.unescape((r.get("title") or "").strip())
        rurl = (r.get("url") or "").strip()
        snippet = html.unescape(" ".join((r.get("snippet") or "").split()))
        published = r.get("time") or r.get("published")
        print(f"{i}. {title}")
        print(f"   {rurl}")
        if published:
            print(f"   published: {published}")
        if snippet:
            print(f"   {snippet}")
        print()

    if related:
        print("Related searches: " + ", ".join(html.unescape(s) for s in related[:8]), file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
