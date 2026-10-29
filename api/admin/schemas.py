"""Request and response bodies of the ``/admin`` routes."""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BaselineItemOut(BaseModel):
    """One meter or key of the starting state, and what was or may be said of it."""

    key: str = Field(description="Stable id of the item, to send back with its choice")
    subject_type: Literal["meter", "key"]
    subject_ref: str = Field(description="The EAN, or the key's id")
    id_sharing_operation: int
    operation_name: str | None = None
    change: Literal["present", "added", "removed"]
    choices: list[str]
    default_choice: str
    label: str | None = Field(default=None, description="A key's name")
    id_member: int | None = None
    grd: str | None = None
    injection_status: int | None = None
    start_date: datetime.date | None = None
    # Once confirmed: what the admin chose.
    chosen: str | None = None
    effective_date: datetime.date | None = None
    sent_date: datetime.date | None = None


class BaselineOut(BaseModel):
    status: Literal["not_supported", "pending", "confirmed"]
    regulator: str | None = None
    kind: Literal["first", "reactivation"] | None = None
    taken_at: datetime.datetime | None = None
    confirmed_at: datetime.datetime | None = None
    confirmed_by: str | None = None
    id_dossier: int | None = Field(default=None, description="The baseline procedure")
    items: list[BaselineItemOut] = Field(default_factory=list)


class BaselineChoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    choice: Literal["active", "not_notified", "in_progress", "ended"]
    effective_date: datetime.date | None = Field(
        default=None, description="Active: since when, if known. Ended: since when."
    )
    sent_date: datetime.date | None = Field(
        default=None, description="In progress: the day it was sent (required)"
    )


class BaselineConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choices: list[BaselineChoiceIn] = Field(
        default_factory=list, description="An item left out takes its default choice"
    )
