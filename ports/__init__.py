"""Adapters to systems this service does not own.

Each port is a Protocol plus at least one concrete implementation, so the API
layer depends on the shape rather than the technology and tests can substitute a
fake without a broker or a second database.

- ``crm_core`` / ``crm_core_sqlalchemy`` — read-only access to the CRM core DB.
- ``events``                            — publishing domain events to NATS.

Phase 2 adds ``document_generation`` (render requests over NATS) here.
"""
