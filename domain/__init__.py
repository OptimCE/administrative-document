"""Pure domain logic — no database, no I/O beyond reading reference JSON.

Everything here is directly unit-testable and is the part of the service that
encodes the *rules* rather than the plumbing:

- ``statemachine`` — which status transitions are legal, and what each requires.
- ``deadlines``    — turning ``deadline_rule`` rows into concrete due dates.
- ``calendar``     — Belgian business-day and calendar-month arithmetic.
- ``regions``      — mapping a community's regulator code to a Region.
"""
