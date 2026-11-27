# SPDX-License-Identifier: MIT
"""Who is driving this Hermes run: which gateway platform, and which profile.

Two facts are read from Hermes itself, never guessed from a process name:

* **Gateway platform.** The messaging gateway binds the platform of the turn it is
  serving (``slack``, ``telegram``, ``discord``, ...) as the ContextVar-backed
  session variable ``HERMES_SESSION_PLATFORM`` (``gateway/session_context.py``,
  bound by ``GatewayRunner._set_session_env`` in ``gateway/run.py``). The CLI, TUI
  and desktop never bind it (they bind ``HERMES_SESSION_SOURCE``), so an empty value
  means "not a gateway run". Read through ``get_session_env`` so the ContextVar wins
  over ``os.environ``.
* **Profile.** ``HERMES_SESSION_PROFILE`` when the gateway routes the chat to a named
  profile; otherwise ``hermes_cli.profiles.get_active_profile_name()``, which derives
  the name from the (context-aware) Hermes home. A profile whose home is not under
  ``~/.hermes/profiles`` reports ``custom``: that is "unknown", so it is omitted.

Both imports are Hermes' own and are only available inside a Hermes process; outside
one (or if Hermes moves them) this returns nothing and the plugin emits as before.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

ATTR_GATEWAY_PLATFORM = "hermes.gateway.platform"
ATTR_BOT_PROFILE = "hermes.bot.profile"
ATTR_RUN_TRIGGER = "hermes.run.trigger"

# Surfaces that are not a human chat channel, so a run on them is not a bot conversation even when the gateway
# process serves it (a `hermes chat` turn, the desktop app, the API server, a webhook). Hermes' own definition,
# gateway.session_context.NON_MESSAGING_SESSION_SURFACES, is used when it can be imported; this copy (Hermes
# v0.23.x) is the fallback.
_NON_MESSAGING_FALLBACK = frozenset({
    "", "api_server", "cli", "codex", "desktop", "gateway", "kanban", "local",
    "msgraph_webhook", "tool", "tui", "webhook",
})


def _non_messaging_surfaces() -> frozenset[str]:
    try:
        from gateway.session_context import (  # type: ignore[import-not-found]
            NON_MESSAGING_SESSION_SURFACES,
        )

        return frozenset(NON_MESSAGING_SESSION_SURFACES)
    except Exception:
        return _NON_MESSAGING_FALLBACK
# get_active_profile_name() answers this for a home it cannot name.
_UNNAMED_PROFILE = "custom"


class RunIdentity(NamedTuple):
    """The resource-level identity of a run. ``None`` means "not known / not applicable"."""

    platform: str | None = None
    profile: str | None = None
    trigger: str | None = None

    def resource_attributes(self) -> dict[str, str]:
        attrs: dict[str, str] = {}
        if self.platform:
            attrs[ATTR_GATEWAY_PLATFORM] = self.platform
        if self.profile:
            attrs[ATTR_BOT_PROFILE] = self.profile
        if self.trigger:
            attrs[ATTR_RUN_TRIGGER] = self.trigger
        return attrs


def detect_run_identity() -> RunIdentity:
    """Read the identity of the run on the calling thread/context. Never raises."""
    platform: str | None = None
    trigger: str | None = None
    session_profile = ""
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]

        value = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
        platform = None if value in _non_messaging_surfaces() else value
        session_profile = str(get_session_env("HERMES_SESSION_PROFILE", "") or "").strip()
        if platform is None and str(get_session_env("HERMES_CRON_SESSION", "") or "").strip() == "1":
            # A scheduled run binds no session platform, but it delivers to one: a cron job that reports to a
            # chat channel is a bot run. Local or webhook delivery stays a non-bot run, as before.
            deliver = str(get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "") or "").strip().lower()
            if deliver and deliver not in _non_messaging_surfaces():
                platform, trigger = deliver, "cron"
    except Exception:
        logger.debug("gateway session context unavailable", exc_info=True)

    profile = session_profile or None
    if profile is None:
        try:
            from hermes_cli.profiles import (  # type: ignore[import-not-found]
                get_active_profile_name,
            )

            name = str(get_active_profile_name() or "").strip()
            profile = None if name in ("", _UNNAMED_PROFILE) else name
        except Exception:
            logger.debug("hermes profile unavailable", exc_info=True)
    return RunIdentity(platform, profile, trigger)
