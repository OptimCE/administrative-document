"""domain/inbound_mail.py: reading what the reception address receives."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from domain.inbound_mail import (
    InboundMailError,
    address_pattern,
    parse,
    references,
    tokens,
)

TEMPLATE = "demarches+{token}@optimce.be"


def forwarded(
    *,
    to: str = "demarches+k7x2abcd9z01@optimce.be",
    subject: str = "TR: RE: [OPT-42] Modification du partage — Op Centre",
    body: str = "---------- Message transféré ----------\nVotre dossier est complet.",
    attachments: int = 2,
    html: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Gestionnaire <gestion@cer-exemple.be>"
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = "Mon, 02 Nov 2026 09:15:00 +0100"
    message["Message-ID"] = "<fwd-1@cer-exemple.be>"
    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")
    if attachments >= 1:
        message.add_attachment(
            b"%PDF-1.4 avenant signe",
            maintype="application",
            subtype="pdf",
            filename="Avenant signé.pdf",
        )
    if attachments >= 2:
        original = EmailMessage()
        original["From"] = "ores@ores.be"
        original["Subject"] = "RE: [OPT-42] Modification du partage"
        original.set_content("Complétude confirmée.")
        message.add_attachment(original)
    return message.as_bytes()


class TestReading:
    def test_reads_headers_body_and_attachments(self):
        mail = parse(forwarded())

        assert mail.message_id == "<fwd-1@cer-exemple.be>"
        assert mail.from_address == "gestion@cer-exemple.be"
        assert mail.subject.startswith("TR: RE: [OPT-42]")
        assert mail.sent_at is not None and mail.sent_at.utcoffset() is not None
        assert "Votre dossier est complet." in mail.body_text
        names = [(a.filename, a.content_type) for a in mail.attachments]
        assert names == [
            ("Avenant signé.pdf", "application/pdf"),
            ("message-2.eml", "message/rfc822"),
        ]
        assert mail.attachments[0].content == b"%PDF-1.4 avenant signe"
        assert b"Compl" in mail.attachments[1].content

    def test_an_html_only_body_is_read_as_text(self):
        message = EmailMessage()
        message["To"] = "demarches+k7x2abcd9z01@optimce.be"
        message["Subject"] = "x"
        message.set_content(
            "<html><head><style>p{}</style></head><body><p>Bonjour</p><p>[OPT-7]</p></body></html>",
            subtype="html",
        )

        mail = parse(message.as_bytes())

        assert "Bonjour" in mail.body_text and "p{}" not in mail.body_text
        assert references(mail) == [7]

    @pytest.mark.parametrize("raw", [b"", b"   \n", b"\x00\x01garbage"])
    def test_what_is_no_mail_is_refused(self, raw):
        with pytest.raises(InboundMailError):
            parse(raw)

    def test_an_attachment_name_carries_no_path(self):
        message = EmailMessage()
        message["To"] = "x@y.be"
        message.set_content("x")
        message.add_attachment(
            b"data",
            maintype="application",
            subtype="octet-stream",
            filename="..\\..\\etc/passwd\x07",
        )

        (attachment,) = parse(message.as_bytes()).attachments

        assert attachment.filename == "passwd"


class TestTheCommunity:
    def test_finds_the_code_in_any_recipient_header(self):
        pattern = address_pattern(TEMPLATE)
        assert tokens(parse(forwarded()), pattern) == ["k7x2abcd9z01"]
        # Cc'd, or only in the header the provider adds on delivery.
        copied = forwarded(to="ores@ores.be").replace(
            b"To: ores@ores.be", b"To: ores@ores.be\nCc: DEMARCHES+K7X2ABCD9Z01@optimce.be"
        )
        assert tokens(parse(copied), pattern) == ["k7x2abcd9z01"]
        delivered = forwarded(to="me@cer.be").replace(
            b"To: me@cer.be", b"To: me@cer.be\nDelivered-To: demarches+k7x2abcd9z01@optimce.be"
        )
        assert tokens(parse(delivered), pattern) == ["k7x2abcd9z01"]

    def test_another_address_has_no_code(self):
        pattern = address_pattern(TEMPLATE)
        assert tokens(parse(forwarded(to="demarches@optimce.be")), pattern) == []
        assert tokens(parse(forwarded(to="demarches+k7x2abcd9z01@evil.be")), pattern) == []

    def test_a_catch_all_template_works_alike(self):
        pattern = address_pattern("{token}@inbox.optimce.be")
        assert tokens(parse(forwarded(to="k7x2abcd9z01@inbox.optimce.be")), pattern) == [
            "k7x2abcd9z01"
        ]

    def test_the_template_must_hold_the_code_once(self):
        with pytest.raises(ValueError, match="token"):
            address_pattern("demarches@optimce.be")


class TestTheProcedure:
    def test_the_subjects_reference_comes_before_the_bodys(self):
        mail = parse(forwarded(body="Voir aussi [OPT-9] et [opt-42]."))
        assert references(mail) == [42, 9]

    def test_no_reference(self):
        assert references(parse(forwarded(subject="Bonjour", body="Rien"))) == []
