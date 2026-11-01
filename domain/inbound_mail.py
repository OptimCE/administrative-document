"""A forwarded email, as the reception address receives it (phase 4).

Pure: bytes in, facts out, nothing stored. ``worker/inbound.py`` decides what
to do with them.

- **The community** comes from the address the mail reached: one mailbox
  serves every community, each with its own random code in the address
  (``demarches+<code>@…`` by plus-addressing, or ``<code>@inbox.…`` with a
  catch-all; ``INBOUND_ADDRESS_TEMPLATE``). Forwarded, copied or redirected,
  the code survives in one of the recipient headers: To, Cc, or the ones a
  provider adds when it delivers (Delivered-To, X-Original-To…).
- **The procedure** comes from the reference OptimCE writes in the subject of
  everything it sends, ``[OPT-<id>]``: a reply keeps it in its subject, a
  forward in its quoted body. The caller checks the procedure is the
  community's own (``dossier.id`` is global).
- **The pieces** are the attachments, a forwarded message (``message/rfc822``)
  included, as its own ``.eml``.
"""

from __future__ import annotations

import datetime
import email
import email.policy
import email.utils
import logging
import re
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

#: The reference OptimCE puts in the subject of what it sends.
REFERENCE = re.compile(r"\[OPT-(\d{1,10})\]", re.IGNORECASE)
#: Where a provider leaves the address a mail was delivered to.
RECIPIENT_HEADERS = (
    "To",
    "Cc",
    "Delivered-To",
    "X-Original-To",
    "X-Forwarded-To",
    "Envelope-To",
    "Resent-To",
)
_BODY_MAX_CHARS = 50_000
_NAME_MAX_CHARS = 200


class InboundMailError(ValueError):
    """Bytes that are not a mail this service can read."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class ParsedMail:
    message_id: str | None
    from_address: str | None
    subject: str
    sent_at: datetime.datetime | None
    recipients: tuple[str, ...]
    body_text: str
    attachments: tuple[Attachment, ...]


def address_pattern(template: str) -> re.Pattern[str]:
    """The addresses ``template`` (``demarches+{token}@optimce.be``) stands
    for, the code as its one group. Case-insensitive, as addresses are."""
    if template.count("{token}") != 1:
        raise ValueError("INBOUND_ADDRESS_TEMPLATE must hold {token} once")
    before, after = template.split("{token}")
    return re.compile(rf"^{re.escape(before)}([a-z0-9]{{8,32}}){re.escape(after)}$", re.IGNORECASE)


def tokens(mail: ParsedMail, pattern: re.Pattern[str]) -> list[str]:
    """The codes of the reception addresses the mail reached, in header order."""
    found: list[str] = []
    for address in mail.recipients:
        match = pattern.match(address)
        if match and match.group(1).lower() not in found:
            found.append(match.group(1).lower())
    return found


def references(mail: ParsedMail) -> list[int]:
    """The ``[OPT-<id>]`` references, the subject's first, then the body's."""
    found: list[int] = []
    for text in (mail.subject, mail.body_text):
        for match in REFERENCE.finditer(text or ""):
            reference = int(match.group(1))
            if reference not in found:
                found.append(reference)
    return found


def parse(raw: bytes) -> ParsedMail:
    """Read a mail. Raises ``InboundMailError`` for bytes that are not one."""
    if not raw or not raw.strip():
        raise InboundMailError("empty message")
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as exc:  # the stdlib raises a zoo of types on garbage
        raise InboundMailError(f"unreadable message: {exc}") from exc
    if not isinstance(message, EmailMessage) or not message.keys():
        raise InboundMailError("no headers")
    return ParsedMail(
        message_id=_header(message, "Message-ID"),
        from_address=_first_address(message, "From"),
        subject=_header(message, "Subject") or "",
        sent_at=_date(message),
        recipients=_recipients(message),
        body_text=_body(message),
        attachments=tuple(_attachments(message)),
    )


def _header(message: EmailMessage, name: str) -> str | None:
    try:
        value = message.get(name)
    except Exception:  # a malformed header must not lose the mail
        return None
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:998] or None


def _first_address(message: EmailMessage, name: str) -> str | None:
    value = _header(message, name)
    if not value:
        return None
    addresses = [a for _n, a in email.utils.getaddresses([value]) if a]
    return addresses[0][:320] if addresses else None


def _recipients(message: EmailMessage) -> tuple[str, ...]:
    values: list[str] = []
    for name in RECIPIENT_HEADERS:
        try:
            values.extend(str(v) for v in message.get_all(name) or ())
        except Exception:
            logger.debug("unreadable %s header skipped", name, exc_info=True)
    return tuple(a.strip().lower() for _n, a in email.utils.getaddresses(values) if a)


def _date(message: EmailMessage) -> datetime.datetime | None:
    value = _header(message, "Date")
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


class _Text(HTMLParser):
    """The text of an HTML body: what a mail client would show, roughly."""

    _BREAKS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _body(message: EmailMessage) -> str:
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        content = part.get_content() if part is not None else ""
    except Exception:
        return ""
    if not isinstance(content, str):
        return ""
    if part is not None and part.get_content_subtype() == "html":
        parser = _Text()
        parser.feed(content)
        content = "".join(parser.parts)
    lines = [line.rstrip() for line in content.replace("\r\n", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:_BODY_MAX_CHARS]


def _attachments(message: EmailMessage) -> list[Attachment]:
    found: list[Attachment] = []
    for index, part in enumerate(message.iter_attachments(), start=1):
        content_type = part.get_content_type()
        try:
            if content_type == "message/rfc822":
                inner = part.get_content()
                content = inner.as_bytes() if hasattr(inner, "as_bytes") else bytes(inner)
                default = f"message-{index}.eml"
            else:
                payload = part.get_payload(decode=True)
                content = payload if isinstance(payload, bytes) else b""
                default = f"attachment-{index}"
        except Exception:
            logger.debug("unreadable attachment %d skipped", index, exc_info=True)
            continue
        if not content:
            continue
        found.append(
            Attachment(
                filename=_safe_name(part.get_filename(), default),
                content_type=content_type,
                content=content,
            )
        )
    return found


def _safe_name(name: str | None, default: str) -> str:
    """A file name to store and offer back: no path, no control character."""
    if not name:
        return default
    name = unicodedata.normalize("NFC", name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip(" .")
    return name[:_NAME_MAX_CHARS] or default
