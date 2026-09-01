"""The evaluation harness (T8).

Separate from the test suite by design: ``tests/`` answers *does the code work* and outputs
pass/fail; this answers *is the model good enough* and outputs numbers that move. Folding
the second into the first turns a quality floor into an assertion nobody dares touch, and
leaves a ten-configuration bake-off nowhere to record itself.

Nothing here imports ``meridian``. An eval set carries its own text, so the harness never
reads the database — which is what keeps a run reproducible when the database changes
underneath it.
"""
