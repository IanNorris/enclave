#!/usr/bin/env python3
"""kagi — query the Kagi Search API for high-quality, rankable result URLs.

This is a thin convenience wrapper around the Kagi Search API. Its purpose is
*not* to answer questions, but to surface real primary-source URLs from Kagi's
index, which you then fetch and quote verbatim. Treat the URLs as the trustworthy
output; do not treat any summarised prose as a citation.

Usage:
    kagi <query...>
    kagi --limit 5 <query...>
    kagi --json <query...>          # raw JSON for scripting

Requires the KAGI_TOKEN environment variable (injected by the orchestrator when
a Kagi token is configured). The token is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
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

    url = f"{API_URL}?{urllib.parse.urlencode({'q': query})}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bot {token}"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        print(f"error: Kagi API returned HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"error: could not reach Kagi API: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON from Kagi API: {e}", file=sys.stderr)
        return 1

    # Surface API-level errors (Kagi returns them in an "error" list).
    errors = payload.get("error")
    if errors:
        msgs = "; ".join(str(e.get("msg", e)) for e in errors) if isinstance(errors, list) else str(errors)
        print(f"error: Kagi API error: {msgs}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    data = payload.get("data", []) or []
    # t==0 are search results; t==1 are "related searches" — keep only results.
    results = [d for d in data if d.get("t") == 0][: args.limit]
    related = next((d.get("list", []) for d in data if d.get("t") == 1), [])

    if not results:
        print("No results.", file=sys.stderr)
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        rurl = (r.get("url") or "").strip()
        snippet = " ".join((r.get("snippet") or "").split())
        published = r.get("published")
        print(f"{i}. {title}")
        print(f"   {rurl}")
        if published:
            print(f"   published: {published}")
        if snippet:
            print(f"   {snippet}")
        print()

    if related:
        print("Related searches: " + ", ".join(related[:8]), file=sys.stderr)

    # Remaining API balance helps the user keep an eye on spend.
    balance = payload.get("meta", {}).get("api_balance")
    if balance is not None:
        print(f"[kagi] api_balance: {balance}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
