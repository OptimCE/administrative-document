"""The mailbox the reception addresses deliver to (phase 4).

One mailbox (``demarches@…``) serves every community; the worker connects OUT
to it every few minutes, so the platform opens no inbound route (spec, the
reception address; IMAP chosen over Brevo's inbound webhook, 01/11/2026).

``fetch`` reads the unseen messages without marking them; ``done`` marks one
seen once the worker has stored it (or decided to drop it). A crash between the
two leaves the message unseen: the next poll reads it again, and the worker's
sha256 check makes that a no-op.

The IMAP adapter uses the standard library (``imaplib``), in a thread: no new
dependency. ``MemoryMailbox`` is the tests' (and the fake's) mailbox.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawMail:
    uid: str
    content: bytes


class InboundMailbox(Protocol):
    async def fetch(self, limit: int) -> list[RawMail]: ...

    async def done(self, uid: str) -> None: ...


@dataclass
class ImapMailbox:
    host: str
    port: int
    user: str
    password: str
    folder: str = "INBOX"
    ssl: bool = True
    timeout: float = 30.0

    def _connect(self) -> imaplib.IMAP4:
        conn: imaplib.IMAP4 = (
            imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
            if self.ssl
            else imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        )
        conn.login(self.user, self.password)
        status, _ = conn.select(self.folder)
        if status != "OK":
            conn.logout()
            raise ConnectionError(f"cannot select {self.folder}")
        return conn

    async def fetch(self, limit: int) -> list[RawMail]:
        return await asyncio.to_thread(self._fetch, limit)

    def _fetch(self, limit: int) -> list[RawMail]:
        conn = self._connect()
        try:
            status, data = conn.uid("search", None, "UNSEEN")  # type: ignore[arg-type]
            if status != "OK" or not data or not data[0]:
                return []
            mails: list[RawMail] = []
            for uid in data[0].split()[:limit]:
                status, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
                content = next(
                    (p[1] for p in parts or () if isinstance(p, tuple) and len(p) > 1),
                    None,
                )
                if status == "OK" and isinstance(content, bytes):
                    mails.append(RawMail(uid=uid.decode(), content=content))
            return mails
        finally:
            _logout(conn)

    async def done(self, uid: str) -> None:
        await asyncio.to_thread(self._done, uid)

    def _done(self, uid: str) -> None:
        conn = self._connect()
        try:
            conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            _logout(conn)


def _logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except Exception:  # the poll's result does not depend on a clean goodbye
        logger.debug("IMAP logout failed", exc_info=True)


@dataclass
class MemoryMailbox:
    """A mailbox in memory: ``deliver`` puts a message in, ``fetch`` reads the
    unseen ones, ``done`` marks one seen."""

    _mails: dict[str, bytes] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)

    def deliver(self, content: bytes) -> str:
        uid = str(len(self._mails) + 1)
        self._mails[uid] = content
        return uid

    async def fetch(self, limit: int) -> list[RawMail]:
        return [
            RawMail(uid=uid, content=content)
            for uid, content in self._mails.items()
            if uid not in self.seen
        ][:limit]

    async def done(self, uid: str) -> None:
        self.seen.add(uid)
