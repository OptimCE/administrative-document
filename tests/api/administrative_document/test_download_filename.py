"""Naming a download.

A file the operating system cannot open is indistinguishable from a broken
download, so the one property that matters is: whatever else happens, the name
ends in a usable extension. Pure function, so no database and no HTTP.
"""

import pytest

from api.administrative_document.routes import _download_filename
from shared.const import extension_for_content_type

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def name(original, content_type=XLSX, document_id=7, version_no=2):
    return _download_filename(
        original, content_type=content_type, document_id=document_id, version_no=version_no
    )


class TestExtensionMapping:
    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("application/pdf", ".pdf"),
            (XLSX, ".xlsx"),
            (DOCX, ".docx"),
            # Parameters must not defeat the lookup.
            ("text/html; charset=utf-8", ".html"),
            ("application/pdf ", ".pdf"),
        ],
    )
    def test_known_media_types_resolve(self, content_type, expected):
        assert extension_for_content_type(content_type) == expected

    @pytest.mark.parametrize("content_type", [None, "", "application/octet-stream"])
    def test_an_unknown_media_type_adds_nothing(self, content_type):
        assert extension_for_content_type(content_type) == ""


class TestDownloadFilename:
    def test_the_uploaded_name_is_kept(self):
        assert name("annexe 6 - version finale.xlsx") == "annexe 6 - version finale.xlsx"

    def test_a_generated_name_is_kept(self):
        assert name("annexe6-notification.xlsx") == "annexe6-notification.xlsx"

    def test_a_nameless_version_still_gets_an_extension(self):
        """Versions stored before the filename was recorded take this path."""
        assert name(None) == "document-7-v2.xlsx"

    def test_the_extension_is_never_doubled(self):
        assert name("convention.docx", content_type=DOCX) == "convention.docx"

    def test_a_mismatched_extension_is_corrected_not_replaced(self):
        """The media type is what the bytes actually are, so it wins — but the
        author's name is preserved rather than discarded."""
        assert name("rapport.pdf", content_type=XLSX) == "rapport.pdf.xlsx"

    def test_an_unknown_media_type_leaves_the_name_alone(self):
        assert name(None, content_type=None) == "document-7-v2"

    def test_a_header_breaking_name_is_neutralised(self):
        """Content-Disposition is a header: a quote or newline would split it."""
        assert '"' not in name('evil".pdf\r\nX-Injected: 1')
        assert "\r" not in name('evil".pdf\r\nX-Injected: 1')
        assert "\n" not in name('evil".pdf\r\nX-Injected: 1')

    def test_an_empty_name_falls_back_rather_than_producing_a_bare_extension(self):
        assert name("") == "document-7-v2.xlsx"
