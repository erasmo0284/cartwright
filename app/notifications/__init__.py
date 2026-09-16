"""Delivery of Windows notifications, and the wording of each one.

:mod:`app.notifications.notifier` owns the single path to the user's screen:
it honours the notification switches in Settings, refuses to repeat itself,
and falls back to a system tray balloon when Windows will not show a real
Action Center toast. The ``build_*`` helpers in the same module hold the
wording, so the copy for a notification is reviewed in one place rather than
written inline wherever an event happens to be raised.
"""
