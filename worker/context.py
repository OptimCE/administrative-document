"""Tenant context for worker handlers.

The API sets ``current_internal_community_id`` from the gateway headers; a worker
has no request, so it sets the same ContextVar by hand around each message.
Without it ``with_community_scope`` filters on None and every owned-DB SELECT
returns nothing — a failure that looks like missing data rather than a missing
context, so it is worth doing explicitly and in one place.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from core.context_vars import current_internal_community_id


@contextlib.contextmanager
def with_tenant(id_community: int) -> Iterator[None]:
    token = current_internal_community_id.set(id_community)
    try:
        yield
    finally:
        current_internal_community_id.reset(token)
