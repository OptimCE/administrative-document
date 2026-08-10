class NotificationTypes:
    """Notification ``type`` keys this service publishes.

    ``<feature>.<event>`` strings, matching crm-backend's free-form taxonomy.
    The frontend localises the displayed text from the key
    (``NOTIFICATIONS.TYPES.<type>.title``); the backend stores only key + data.

    Every key added here also needs an entry in
    ``crm-frontend/src/app/features/notifications/services/notification-type.registry.ts``
    and a title in all four ``crm-frontend/src/assets/i18n/*.json`` files, or it
    renders to the user as a raw i18n key with no error anywhere.

    ``admin_deadline.due_soon`` is the one INFORMATIONAL type here, so it is the
    only one a manager may mute. It is also the only producer anywhere that
    passes an explicit ``dedupe_key``: its sweep re-selects the same rows every
    day, so the payload alone cannot distinguish two occurrences and the
    occurrence date has to be in the key.

    This is the ONLY per-service module in ``core/notifications``; the other five
    are byte-identical across producers so Phase 2's extraction is a move rather
    than a rewrite.
    """

    ADMIN_DEADLINE_DUE_SOON = "admin_deadline.due_soon"
    ADMIN_DEADLINE_MISSED = "admin_deadline.missed"
    ADMIN_DOSSIER_ACKNOWLEDGED = "admin_dossier.acknowledged"
