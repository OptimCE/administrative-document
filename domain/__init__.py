"""Pure domain logic — no database, no I/O beyond reading reference JSON.

Everything here is directly unit-testable and is the part of the service that
encodes the *rules* rather than the plumbing:

- ``prefill``      — the CRM state as a mandated form's render payload.
- ``cwape_labels`` — CRM codes in the regulator's exact vocabulary.
- ``regions``      — mapping a community's regulator code to a Region.

A procedure's lifecycle, deadlines and calendar are the engine's (``engine/``).
"""
