"""Test configuration.

`PERSISTENCE` must be set before `app.*` is imported anywhere: the module-level
`app.store.sessions` / `app.auth.tokens` singletons build their backend at
construction time, and pytest imports conftest before test modules.

Tests therefore run on the in-memory backend by default — no database file, no
state leaking between runs. The persistence layer itself is covered explicitly
by `tests/test_p2_persistence.py`, which constructs its own SQLite backend on a
tmp path.
"""

import os

os.environ.setdefault("PERSISTENCE", "memory")
