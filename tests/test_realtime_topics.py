"""Every realtime topic this annexe emits is registered, and a render that ends
says so.

An unregistered topic is not an error anywhere: ``build_envelope`` returns
None and ``emit`` publishes nothing, and the hub would drop it anyway. The
espace administratif's hints went nowhere that way until L1.8 registered them;
this scan is what notices the next one.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.realtime.envelope import TOPICS, build_envelope
from tests.test_worker import _document_with_pending_render, _success
from worker import docgen_results

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKIP = {".venv", "venv", "tests", "__pycache__", ".mypy_cache", "node_modules"}


def _emitted_topics() -> dict[str, list[str]]:
    """Topic literal -> the files emitting it."""
    found: dict[str, list[str]] = {}
    for path in _ROOT.rglob("*.py"):
        if _SKIP & set(path.relative_to(_ROOT).parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "emit":
                continue
            for keyword in node.keywords:
                if keyword.arg == "topic" and isinstance(keyword.value, ast.Constant):
                    found.setdefault(str(keyword.value.value), []).append(
                        str(path.relative_to(_ROOT))
                    )
    return found


ADMIN_TOPICS = {
    "admin.change.detected",
    "admin.dossier.updated",
    "admin.document.ready",
    "admin.deadline.due",
}


def test_every_emitted_topic_is_registered():
    emitted = _emitted_topics()
    # The scan finds what it should, or a refactor blinded it.
    assert ADMIN_TOPICS | {"notification.created"} <= set(emitted)
    unknown = {topic: files for topic, files in emitted.items() if topic not in TOPICS}
    assert unknown == {}


@pytest.mark.parametrize("topic", sorted(ADMIN_TOPICS))
def test_each_admin_topic_builds_an_envelope(topic):
    envelope = build_envelope(
        topic=topic, resource=("dossier", 7), hint={"status": "ready"}, scope_community_id=3
    )
    assert envelope is not None
    assert envelope["topic"] == topic


class TestARenderThatEndsIsSignalled:
    @pytest.fixture
    def emitted(self, monkeypatch):
        calls: list[dict] = []

        async def emit(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(docgen_results, "emit", emit)
        return calls

    @pytest.mark.asyncio
    async def test_a_landed_render_says_ready(
        self, client, manager_headers, db_session, community, sharing_operation, emitted
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        outcome = await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        assert outcome == docgen_results.ATTACHED
        assert [(e["topic"], e["resource"], e["hint"]) for e in emitted] == [
            ("admin.document.ready", ("document", document_id), {"status": "ready"})
        ]
        assert emitted[0]["scope_community_id"] == community.id

    @pytest.mark.asyncio
    async def test_a_permanent_failure_says_failed(
        self, client, manager_headers, db_session, community, sharing_operation, emitted
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        await docgen_results.process_docgen_result(
            {
                "request_id": request_id,
                "status": "failed",
                "error": {"code": "VALIDATION_ERROR", "message": "x", "permanent": True},
                "metadata": {"tenant_id": str(community.id), "document_id": document_id},
            },
            local_session=db_session,
            crm_session=db_session,
        )

        assert [(e["topic"], e["hint"]) for e in emitted] == [
            ("admin.document.ready", {"status": "failed"})
        ]

    @pytest.mark.asyncio
    async def test_a_transient_failure_and_a_redelivery_say_nothing(
        self, client, manager_headers, db_session, community, sharing_operation, emitted
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        await docgen_results.process_docgen_result(
            {
                "request_id": request_id,
                "status": "failed",
                "error": {"code": "TIMEOUT", "message": "x", "permanent": False},
                "metadata": {"tenant_id": str(community.id), "document_id": document_id},
            },
            local_session=db_session,
            crm_session=db_session,
        )
        assert emitted == []

        body = _success(request_id, document_id=document_id, tenant=community.id)
        await docgen_results.process_docgen_result(
            body, local_session=db_session, crm_session=db_session
        )
        await docgen_results.process_docgen_result(
            body, local_session=db_session, crm_session=db_session
        )
        assert len(emitted) == 1  # the redelivered success attaches nothing
