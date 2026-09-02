"""The harness must not read the application's database.

An eval set carries its own text precisely so a run does not depend on live data: the same
set, at the same commit, under the same config has to score the same in six months. Reading
``canonical_record`` would put a moving input back underneath a frozen one, and nothing
would report it — the hash still matches, the row count still matches, the number changes.

Holding that takes two mechanisms, the same pair the Platform boundary uses.
``eval/ruff.toml`` bans the import statically under the existing lint step; it cannot see
``importlib.import_module("meridian.db.session")``, which is what this covers.

The probe runs in a subprocess because ``tests/conftest.py`` imports the application's
models at collection time. An in-process assertion would fail whatever the harness did, and
a test that always fails gets deleted rather than fixed.
"""

import subprocess
import sys
import textwrap

# Matching is exact on the top-level name: `meridian_config`, `meridian_contract` and
# `meridian_dbkit` are shared libraries the harness is meant to use, and a `startswith`
# on "meridian" would flag all three.
PROBE = textwrap.dedent("""
    import importlib
    import pkgutil
    import sys

    import eval

    for info in pkgutil.walk_packages(eval.__path__, "eval."):
        importlib.import_module(info.name)

    leaked = sorted(
        name for name in sys.modules
        if name == "meridian" or name.startswith("meridian.")
    )
    print(" ".join(leaked))
""")


def test_the_harness_imports_nothing_from_the_application() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, f"the probe failed to run:\n{result.stderr}"
    assert result.stdout.strip() == "", (
        f"harness modules imported from the application: {result.stdout.strip()}"
    )
