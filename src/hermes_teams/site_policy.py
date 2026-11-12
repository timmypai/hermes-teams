"""Administrator-owned deployment input. Empty means no boards opted in.

A future authorized rebuild may bind a canonical native DB path to a reviewed
policy (see examples/policy.json). Never populate from model/worker arguments,
environment variables, task text, or writable project configuration. All native
processes for that board must share this administrator-owned installed module.
This is an explicit integration point, not an OS security boundary.
"""
BINDINGS = {}
