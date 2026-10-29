"""The procedures engine: generic, and blind to every regulator.

Everything a regulator owns (its procedures, steps, deadlines, pieces, holidays)
is data in its plugin under ``regulators/``; this package only reads that data
through ``engine.contracts``. It never imports ``regulators``: the plugins
import the engine, and the entry points (API, worker) hand the engine a
registry. ``tests/engine/test_boundary.py`` enforces both directions.
"""
