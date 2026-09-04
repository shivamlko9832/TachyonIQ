"""
Shared pytest configuration.

`uada.config.Settings` requires `db_url` with no default, and the module
instantiates a singleton at import time. Any test that imports a module
which transitively imports `uada.config` (e.g. `uada.db.adapter`) would
otherwise fail at collection time with no real database configured. Set a
harmless placeholder before any such import; individual tests construct
their own `Settings`/adapters with whatever connection string they need.
"""

import os

os.environ.setdefault("UADA_DB_URL", "sqlite:///:memory:")
