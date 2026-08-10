"""Who may WRITE the reference registries (templates, deadline rules).

Both registries are two-tier: ``id_community IS NULL`` is a platform default that
every community reads, and a community customises one by POSTing its own override.
``Role.ADMIN`` is COMMUNITY-scoped — the creator of any community holds it — so the
PATCH routes must not be able to reach a platform default or another tenant's
override. Reads stay two-tier; only writes are narrowed.
"""

import pytest

pytestmark = pytest.mark.asyncio

WAL = 1
CREATION_NOTIFICATION = 1
BUSINESS_DAYS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def visible_templates(client, headers) -> list[dict]:
    response = await client.get("/templates", headers=headers)
    assert response.status_code == 200, response.text
    rows: list[dict] = response.json()["data"]
    return rows


async def visible_rules(client, headers) -> list[dict]:
    response = await client.get("/deadline-rules", headers=headers)
    assert response.status_code == 200, response.text
    rows: list[dict] = response.json()["data"]
    return rows


async def platform_default_template(client, headers) -> dict:
    """A seeded CWaPE template — the shared registry row, owned by no community."""
    rows = await visible_templates(client, headers)
    defaults = [row for row in rows if row["id_community"] is None]
    assert defaults, "seeds should ship platform-default templates"
    return defaults[0]


async def platform_default_rule(client, headers) -> dict:
    rows = await visible_rules(client, headers)
    defaults = [row for row in rows if row["id_community"] is None]
    assert defaults, "seeds should ship platform-default deadline rules"
    return defaults[0]


async def create_template_override(client, headers, *, doc_type: str) -> dict:
    """POST an override for this community. Creation already stamps id_community."""
    response = await client.post(
        "/templates",
        json={
            "region": WAL,
            "doc_type": doc_type,
            "version": 1,
            "valid_from": "2026-01-01",
            "file_ref": "bundles/own/v1",
            "label": "override",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    row: dict = response.json()["data"]
    assert row["id_community"] is not None
    return row


async def create_rule_override(client, headers, *, deadline_type: str) -> dict:
    response = await client.post(
        "/deadline-rules",
        json={
            "region": WAL,
            "dossier_type": CREATION_NOTIFICATION,
            "trigger_event": "dossier.submitted",
            "deadline_type": deadline_type,
            "offset_value": 10,
            "offset_unit": BUSINESS_DAYS,
            "description": "override",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    row: dict = response.json()["data"]
    assert row["id_community"] is not None
    return row


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TestTemplateRegistryWrites:
    async def test_admin_can_patch_its_own_override(self, client, admin_headers):
        """The control: the write path still works for the row the community owns."""
        own = await create_template_override(client, admin_headers, doc_type="own_form")

        response = await client.patch(
            f"/templates/{own['id']}", json={"label": "renamed"}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["label"] == "renamed"

    async def test_admin_cannot_patch_a_platform_default(self, client, admin_headers):
        """A community admin editing a seeded CWaPE form would change it for every tenant."""
        default = await platform_default_template(client, admin_headers)

        response = await client.patch(
            f"/templates/{default['id']}",
            json={"label": "hijacked", "file_ref": "bundles/attacker/v1"},
            headers=admin_headers,
        )
        assert response.status_code == 404, response.text

        after = await platform_default_template(client, admin_headers)
        assert after["label"] == default["label"]
        assert after["file_ref"] == default["file_ref"]

    async def test_admin_cannot_patch_another_communitys_override(
        self, client, admin_headers, other_admin_headers
    ):
        theirs = await create_template_override(client, other_admin_headers, doc_type="their_form")

        response = await client.patch(
            f"/templates/{theirs['id']}",
            json={"label": "hijacked", "file_ref": "bundles/attacker/v1"},
            headers=admin_headers,
        )
        assert response.status_code == 404, response.text

        # Re-read as the owner: the row is untouched, not merely hidden.
        rows = await visible_templates(client, other_admin_headers)
        after = next(row for row in rows if row["id"] == theirs["id"])
        assert after["label"] == "override"
        assert after["file_ref"] == "bundles/own/v1"

    async def test_another_communitys_override_is_not_even_visible(
        self, client, admin_headers, other_admin_headers
    ):
        theirs = await create_template_override(client, other_admin_headers, doc_type="their_form")
        rows = await visible_templates(client, admin_headers)
        assert theirs["id"] not in {row["id"] for row in rows}

    async def test_platform_defaults_stay_readable(self, client, admin_headers):
        """Narrowing the write path must not narrow the read path."""
        rows = await visible_templates(client, admin_headers)
        assert any(row["id_community"] is None for row in rows)


# ---------------------------------------------------------------------------
# Deadline rules
# ---------------------------------------------------------------------------


class TestDeadlineRuleRegistryWrites:
    async def test_admin_can_patch_its_own_override(self, client, admin_headers):
        own = await create_rule_override(client, admin_headers, deadline_type="own_check")

        response = await client.patch(
            f"/deadline-rules/{own['id']}", json={"offset_value": 30}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["offset_value"] == 30

    async def test_admin_cannot_patch_a_platform_default(self, client, admin_headers):
        """A regulatory clock is changed by shipping a seed, never over HTTP."""
        default = await platform_default_rule(client, admin_headers)

        response = await client.patch(
            f"/deadline-rules/{default['id']}",
            json={"offset_value": 9999},
            headers=admin_headers,
        )
        assert response.status_code == 404, response.text

        after = await platform_default_rule(client, admin_headers)
        assert after["offset_value"] == default["offset_value"]

    async def test_admin_cannot_patch_another_communitys_override(
        self, client, admin_headers, other_admin_headers
    ):
        theirs = await create_rule_override(
            client, other_admin_headers, deadline_type="their_check"
        )

        response = await client.patch(
            f"/deadline-rules/{theirs['id']}",
            json={"offset_value": 9999},
            headers=admin_headers,
        )
        assert response.status_code == 404, response.text

        rows = await visible_rules(client, other_admin_headers)
        after = next(row for row in rows if row["id"] == theirs["id"])
        assert after["offset_value"] == 10

    async def test_another_communitys_override_is_not_even_visible(
        self, client, admin_headers, other_admin_headers
    ):
        theirs = await create_rule_override(
            client, other_admin_headers, deadline_type="their_check"
        )
        rows = await visible_rules(client, admin_headers)
        assert theirs["id"] not in {row["id"] for row in rows}

    async def test_platform_defaults_stay_readable(self, client, admin_headers):
        rows = await visible_rules(client, admin_headers)
        assert any(row["id_community"] is None for row in rows)
