class NotificationTypes:
    """Notification ``type`` keys this service publishes.

    ``<feature>.<event>`` strings, matching crm-backend's free-form taxonomy.
    The frontend localises the displayed text from the key
    (``NOTIFICATIONS.TYPES.<type>.title``); the backend stores only key + data.

    Every key added here also needs an entry in
    ``crm-frontend/src/app/features/notifications/services/notification-type.registry.ts``
    and a title in all four ``crm-frontend/src/assets/i18n/*.json`` files, or it
    renders to the user as a raw i18n key with no error anywhere.

    The deadlines are told in-app, one notification per rung of the reminder
    ladder (J-14, J-7, J-2; ``admin_deadline.due_soon``) and when missed. Their
    email is ``admin_digest.summary``, off unless a community's admin turned it
    on (``digest_setting``: its rhythm and its recipients), built by the daily
    job (``worker/digest.py``): at most one per recipient per day, across
    communities, with an explicit ``dedupe_key`` holding the user and the day.
    A recipient stops it in their profile (``admin_digest`` email OFF).

    This is the ONLY per-service module in ``core/notifications``; the other five
    are byte-identical across producers so Phase 2's extraction is a move rather
    than a rewrite.
    """

    ADMIN_DEADLINE_DUE_SOON = "admin_deadline.due_soon"
    ADMIN_DEADLINE_MISSED = "admin_deadline.missed"
    ADMIN_DOSSIER_ACKNOWLEDGED = "admin_dossier.acknowledged"
    # Phase 4 (2026-11-01).
    ADMIN_DIGEST_SUMMARY = "admin_digest.summary"
    ADMIN_EFFECT_APPLIED = "admin_effect.applied"
    ADMIN_METER_ACTIVE = "admin_meter.active"
    ADMIN_CORRESPONDENCE_RECORDED = "admin_correspondence.recorded"
    ADMIN_CORRESPONDENCE_RECEIVED = "admin_correspondence.received"
