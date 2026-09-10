"""Sci-saurus — control-plane core.

Implements the durable core of the v0.8 contract (docs/40-execution-contract.md):
transactional control store, immutable artifacts, event hash chain, message
outbox/leases, and task lifecycle. Per D19/D35 the control store is a native
SQLite database scoped to one research project; JSONL exports and Git
snapshots are derived views, never a second live authority.
"""

__version__ = "0.8.0"