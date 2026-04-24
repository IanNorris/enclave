#!/usr/bin/env python3
"""Extend Ice() in the bundled Copilot CLI JS to accept Python-SDK permission kinds.

CLI 1.0.35 regressed by only handling interactive-CLI kinds (approve-once,
reject, user-not-available) in its `Ice()` permission-response switch. The
Python SDK emits the SDK result kinds (approved, denied-interactively-by-user,
denied-by-rules, denied-by-content-exclusion-policy,
denied-no-approval-rule-and-could-not-request-from-user) which now fall through
to the default arm and raise "unexpected user permission response" — breaking
every shell/write/mcp/memory tool call routed through a registered
on_permission_request handler.

We inject the missing cases right before the default arm. Idempotent: the
pattern won't match once patched.
"""
import re
import sys
from pathlib import Path

PATTERN = re.compile(
    r'(case"user-not-available":return\{kind:"denied-no-approval-rule-and-could-not-request-from-user"\};)'
    r'(default:[A-Za-z_$]+\(t,"unexpected user permission response"\))'
)
INJECT = (
    r'\1'
    r'case"approved":return{kind:"approved"};'
    r'case"denied-interactively-by-user":return{kind:"denied-interactively-by-user",feedback:t.feedback};'
    r'case"denied-by-rules":return{kind:"denied-by-rules",rules:t.rules};'
    r'case"denied-by-content-exclusion-policy":return{kind:"denied-by-content-exclusion-policy"};'
    r'case"denied-no-approval-rule-and-could-not-request-from-user":return{kind:"denied-no-approval-rule-and-could-not-request-from-user"};'
    r'\2'
)

ROOT = Path.home() / ".cache" / "copilot"
patched = already = 0
if ROOT.exists():
    for p in ROOT.rglob("*.js"):
        try:
            src = p.read_text()
        except Exception:
            continue
        if '"unexpected user permission response"' not in src:
            continue
        new = PATTERN.sub(INJECT, src)
        if new == src:
            already += 1
            continue
        try:
            p.write_text(new)
            patched += 1
        except OSError as exc:
            print(f"[copilot-patch] {p}: {exc}", file=sys.stderr)

print(f"[copilot-patch] patched={patched} already_patched_or_unmatched={already}",
      file=sys.stderr)
