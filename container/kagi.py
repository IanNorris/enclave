#!/usr/bin/env python3
"""kagi — Kagi Search + Extract helpers for agents.

Two capabilities are exposed, selected by the command name (or a subcommand):

    kagi_search <query...>          # ranked primary-source result URLs
    kagi_extract <url> [url...]     # fetch page content as clean markdown

Equivalent subcommand forms also work:

    kagi search <query...>
    kagi extract <url> [url...]

The point of `kagi_search` is *not* to answer questions, but to surface real
primary-source URLs from Kagi's index. The point of `kagi_extract` is to pull
the readable content of a page as markdown (far more reliable than scraping
HTML yourself). Treat URLs and extracted page text as the trustworthy output;
never treat a summarised answer as a citation.

`kagi_search --extract N` will additionally extract the content of the top N
results inline in a single call (Kagi replaces each result's snippet with the
page markdown). This costs extra (Extract API rate), so use it deliberately.

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

SEARCH_URL = "https://kagi.com/api/v1/search"
EXTRACT_URL = "https://kagi.com/api/v1/extract"


def _token() -> str | None:
    token = os.environ.get("KAGI_TOKEN", "").strip()
    if not token:
        print("error: KAGI_TOKEN is not set — Kagi is not configured for this "
              "session.", file=sys.stderr)
        return None
    return token


def _extract_errors(obj: dict) -> str | None:
    """Return a human-readable error string from a Kagi response, or None.

    The APIs use `error` (singular array) for 4xx bodies and `errors` (plural)
    in some responses (e.g. ip_not_allowed, /extract). Handle both.
    """
    errs = obj.get("error") or obj.get("errors")
    if not errs:
        return None
    if isinstance(errs, list):
        return "; ".join(str(x.get("message") or x.get("code") or x) for x in errs)
    return str(errs)


def _post(url: str, token: str, body: dict) -> tuple[dict | None, int]:
    """POST a JSON body to a Kagi v1 endpoint.

    Returns (payload, 0) on success, or (None, exit_code) on failure (after
    printing an error to stderr).
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")), 0
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        msg = text[:500]
        try:
            parsed = _extract_errors(json.loads(text))
            if parsed:
                msg = parsed
        except Exception:
            pass
        print(f"error: Kagi API returned HTTP {e.code}: {msg}", file=sys.stderr)
        return None, 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"error: could not reach Kagi API: {e}", file=sys.stderr)
        return None, 1
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON from Kagi API: {e}", file=sys.stderr)
        return None, 1


def cmd_search(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kagi_search",
        description="Search the web via the Kagi Search API (returns ranked URLs).",
    )
    parser.add_argument("query", nargs="+", help="Search query")
    parser.add_argument("-n", "--limit", type=int, default=10,
                        help="Max results to show (default: 10)")
    parser.add_argument("--extract", type=int, default=0, metavar="N",
                        help="Also extract page content (markdown) for the top N "
                             "results inline (1-10). Costs extra.")
    parser.add_argument("--json", action="store_true",
                        help="Print the raw JSON response")
    args = parser.parse_args(argv)

    token = _token()
    if not token:
        return 2

    query = " ".join(args.query).strip()
    if not query:
        print("error: empty query", file=sys.stderr)
        return 2

    body: dict = {
        "query": query,
        "workflow": "search",
        "limit": max(1, min(1024, args.limit)),
    }
    if args.extract and args.extract > 0:
        body["extract"] = {"count": max(1, min(10, args.extract))}

    payload, code = _post(SEARCH_URL, token, body)
    if payload is None:
        return code

    err = _extract_errors(payload)
    if err:
        print(f"error: Kagi API error: {err}", file=sys.stderr)
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
        print("Related searches: " + ", ".join(html.unescape(s) for s in related[:8]),
              file=sys.stderr)
    return 0


def cmd_extract(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kagi_extract",
        description="Extract page content as clean markdown via the Kagi Extract API.",
    )
    parser.add_argument("url", nargs="+", help="One or more HTTPS URLs (max 10)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="Seconds budget for the whole bulk fetch")
    parser.add_argument("--json", action="store_true",
                        help="Print the raw JSON response")
    args = parser.parse_args(argv)

    token = _token()
    if not token:
        return 2

    urls = [u.strip() for u in args.url if u.strip()]
    if not urls:
        print("error: no URLs given", file=sys.stderr)
        return 2
    if len(urls) > 10:
        print("error: at most 10 URLs per call", file=sys.stderr)
        return 2

    body: dict = {"pages": [{"url": u} for u in urls], "format": "json"}
    if args.timeout is not None:
        body["timeout"] = args.timeout

    payload, code = _post(EXTRACT_URL, token, body)
    if payload is None:
        return code

    # A top-level error envelope with no data means the whole request failed.
    top_err = _extract_errors(payload) if not payload.get("data") else None
    if top_err:
        print(f"error: Kagi API error: {top_err}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    pages = payload.get("data") or []
    any_ok = False
    for p in pages:
        purl = (p.get("url") or "").strip()
        markdown = p.get("markdown")
        perr = p.get("error")
        print(f"# {purl}\n")
        if perr:
            print(f"[extract failed: {perr}]\n")
        elif markdown:
            any_ok = True
            print(markdown.rstrip() + "\n")
        else:
            print("[no content extracted]\n")

    # Surface per-request errors (e.g. one invalid URL) on stderr.
    req_errs = payload.get("errors")
    if req_errs:
        print("Extract warnings: " + (_extract_errors(payload) or ""), file=sys.stderr)
    return 0 if any_ok or pages else 1


def main() -> int:
    prog = os.path.basename(sys.argv[0] or "kagi")
    argv = sys.argv[1:]

    # Dispatch by command name first (kagi_search / kagi_extract), then by an
    # explicit subcommand (kagi search / kagi extract). Bare `kagi` defaults to
    # search for backwards compatibility.
    if prog == "kagi_extract":
        return cmd_extract(argv)
    if prog == "kagi_search":
        return cmd_search(argv)

    if argv and argv[0] in ("search", "extract"):
        sub, rest = argv[0], argv[1:]
        return cmd_extract(rest) if sub == "extract" else cmd_search(rest)

    return cmd_search(argv)


if __name__ == "__main__":
    sys.exit(main())
