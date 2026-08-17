"""The Platform must not import the application (RFC A1).

Holding that boundary takes two mechanisms. ``platform/ruff.toml`` bans the import
statically and runs under the existing lint step; it cannot see
``importlib.import_module("meridian.db.types")``, which is what this covers.

The probe runs in a subprocess because ``tests/conftest.py`` imports the application's
models at collection time. An in-process assertion would fail whatever the Platform did, and
a test that always fails gets deleted rather than fixed.

Between them the two mechanisms leave one gap, measured rather than assumed:

===========================================  =========  ==============
leak                                         ruff       this test
===========================================  =========  ==============
``from meridian.x import y``, module level   caught     caught
``from meridian.x import y``, in a function  caught     missed
``importlib.import_module``, module level    missed     caught
``importlib.import_module``, in a function   missed     **missed**
===========================================  =========  ==============

Only modules are imported here, never called, so a dynamic import inside a function body
that no import executes is invisible to both. Writing one is not a slip; every way of
reaching the application's code by accident is covered.
"""

import subprocess
import sys
import textwrap

# Matching is exact on the top-level name: `meridian_config`, `meridian_contract` and
# `meridian_dbkit` are shared libraries the Platform is meant to use, and a `startswith`
# on "meridian" would flag all three.
PROBE = textwrap.dedent("""
    import importlib
    import pkgutil
    import sys

    import meridian_platform

    for info in pkgutil.walk_packages(meridian_platform.__path__, "meridian_platform."):
        importlib.import_module(info.name)

    leaked = sorted(
        name for name in sys.modules
        if name == "meridian" or name.startswith("meridian.")
    )
    print(" ".join(leaked))
""")


def test_the_platform_imports_nothing_from_the_application() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, f"the probe failed to run:\n{result.stderr}"
    assert result.stdout.strip() == "", (
        f"Platform modules imported from the application: {result.stdout.strip()}"
    )
