"""Resolving selector chains against a live page.

This is the only place that turns a :class:`~app.automation.selectors.Candidate`
into a Playwright locator. It walks a chain in order, reports which candidate
matched, and records when a fallback was needed -- which is how a change in
Amazon's markup becomes a visible signal in the logs instead of a silent
wrong answer.

Two details that have historically caused wrong readings:

* **Text is read with ``text_content()``, not ``inner_text()``.** Amazon's
  most reliable price node is ``.a-offscreen``, a screen-reader span pushed
  out of view. ``inner_text()`` reflects rendering and can return an empty
  string for it; ``text_content()`` returns the DOM text either way.

* **Presence checks never wait.** ``count()`` returns immediately, so probing
  ten candidates costs nothing. Waiting is opt-in through
  :meth:`PageReader.wait_for_any`, which is used only where the page really
  is expected to change.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.automation.selectors import Candidate, SelectorChain

logger = logging.getLogger("app.automation.page")

#: Collapses the runs of whitespace Amazon's markup is full of.
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Resolution:
    """Which candidate in a chain matched."""

    chain_name: str
    candidate: Candidate
    index: int

    @property
    def used_fallback(self) -> bool:
        return self.index > 0

    @property
    def loose(self) -> bool:
        return self.candidate.loose

    def describe(self) -> str:
        return f"{self.chain_name}[{self.index}]={self.candidate.describe()}"


@dataclass(frozen=True)
class Reading:
    """A value read from the page, with how it was found."""

    value: str | None
    resolution: Resolution | None = None

    @property
    def found(self) -> bool:
        return self.value is not None

    @property
    def loose(self) -> bool:
        return self.resolution is not None and self.resolution.loose


def clean(text: str | None) -> str | None:
    """Collapse whitespace and return ``None`` for an empty result."""
    if text is None:
        return None
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return collapsed or None


class PageReader:
    """Reads values from a page using selector chains."""

    def __init__(self, page: Any) -> None:
        self._page = page
        #: Chains that needed a fallback, for the diagnostics report.
        self.fallbacks: list[str] = []

    @property
    def page(self) -> Any:
        return self._page

    # ---- locating --------------------------------------------------------

    def _build(self, candidate: Candidate, scope: Any | None = None) -> Any | None:
        target = scope if scope is not None else self._page
        try:
            if candidate.css:
                return target.locator(candidate.css)
            if candidate.role and candidate.name:
                return target.get_by_role(
                    candidate.role,
                    name=re.compile(re.escape(candidate.name), re.IGNORECASE),
                )
            if candidate.text:
                return target.get_by_text(
                    re.compile(re.escape(candidate.text), re.IGNORECASE)
                )
        except Exception:  # noqa: BLE001 - a malformed selector must not crash a read
            logger.debug(
                "Could not build a locator", extra={"candidate": candidate.describe()}
            )
        return None

    def find(
        self,
        chain: SelectorChain,
        *,
        scope: Any | None = None,
        visible_only: bool = False,
    ) -> tuple[Any | None, Resolution | None]:
        """First matching candidate's locator, without waiting."""
        for index, candidate in enumerate(chain):
            locator = self._build(candidate, scope)
            if locator is None:
                continue
            try:
                if locator.count() == 0:
                    continue
                first = locator.first
                if visible_only and not first.is_visible():
                    continue
            except Exception:  # noqa: BLE001 - detached nodes and races are normal
                continue
            resolution = Resolution(chain.name, candidate, index)
            if resolution.used_fallback:
                self._note_fallback(resolution)
            return first, resolution
        return None, None

    def exists(
        self, chain: SelectorChain, *, scope: Any | None = None, visible_only: bool = False
    ) -> bool:
        locator, _ = self.find(chain, scope=scope, visible_only=visible_only)
        return locator is not None

    def count(self, chain: SelectorChain, *, scope: Any | None = None) -> int:
        """How many elements the first matching candidate finds."""
        for candidate in chain:
            locator = self._build(candidate, scope)
            if locator is None:
                continue
            try:
                total = locator.count()
            except Exception:  # noqa: BLE001
                continue
            if total:
                return total
        return 0

    def wait_for_any(
        self, chain: SelectorChain, *, timeout_ms: int, poll_ms: int = 250
    ) -> tuple[Any | None, Resolution | None]:
        """Wait until any candidate in ``chain`` attaches.

        Polls rather than delegating to a single locator's ``wait_for``,
        because the whole point of a chain is that it is not known in advance
        which candidate the served page will use.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            locator, resolution = self.find(chain)
            if locator is not None:
                return locator, resolution
            if time.monotonic() >= deadline:
                return None, None
            self._sleep(poll_ms / 1000)

    def _sleep(self, seconds: float) -> None:
        # Playwright's own wait keeps the driver connection serviced, which a
        # bare time.sleep would not.
        try:
            self._page.wait_for_timeout(seconds * 1000)
        except Exception:  # noqa: BLE001
            time.sleep(seconds)

    # ---- reading ---------------------------------------------------------

    def text(
        self,
        chain: SelectorChain,
        *,
        scope: Any | None = None,
        visible_only: bool = False,
        visible_text: bool = False,
    ) -> Reading:
        """Text content of the first match, whitespace-collapsed.

        ``visible_text`` reads what a person sees instead. The default is
        right for prices, which Amazon hides in an offscreen span, and wrong
        for anything Amazon builds from a template: the payment panel's
        ``textContent`` is 462 characters of inline JSON wrapped around the
        21 characters that say which card is being used.
        """
        locator, resolution = self.find(
            chain, scope=scope, visible_only=visible_only
        )
        if locator is None:
            return Reading(None, None)
        readers = ("inner_text", "text_content") if visible_text else ("text_content",)
        for reader_name in readers:
            try:
                raw = getattr(locator, reader_name)(timeout=2_000)
            except Exception:  # noqa: BLE001
                continue
            cleaned = clean(raw)
            if cleaned:
                return Reading(cleaned, resolution)
        return Reading(None, resolution)

    def attribute(
        self, chain: SelectorChain, name: str, *, scope: Any | None = None
    ) -> Reading:
        locator, resolution = self.find(chain, scope=scope)
        if locator is None:
            return Reading(None, None)
        try:
            raw = locator.get_attribute(name, timeout=2_000)
        except Exception:  # noqa: BLE001
            return Reading(None, resolution)
        return Reading(clean(raw), resolution)

    def input_value(self, chain: SelectorChain, *, scope: Any | None = None) -> Reading:
        """Value of a form control, falling back to its ``value`` attribute.

        Amazon renders some hidden fields in ways where ``input_value`` fails
        but the attribute is present, so both are tried.
        """
        locator, resolution = self.find(chain, scope=scope)
        if locator is None:
            return Reading(None, None)
        for reader in ("input_value", "attribute"):
            try:
                raw = (
                    locator.input_value(timeout=2_000)
                    if reader == "input_value"
                    else locator.get_attribute("value", timeout=2_000)
                )
            except Exception:  # noqa: BLE001
                continue
            cleaned = clean(raw)
            if cleaned:
                return Reading(cleaned, resolution)
        return Reading(None, resolution)

    def page_text(self, *, limit: int = 400_000) -> str:
        """The page's *visible* text, for wording-based classification.

        Returns an empty string when the text could not be read. It
        deliberately does not fall back to ``page.content()``: raw HTML
        contains hidden templates and markup the user never sees, and callers
        search this for meaningful phrases. Substituting the source made a
        slow cart page look like an empty one, because an off-screen
        "your cart is empty" template matched.
        """
        try:
            body = self._page.locator("body").first
            raw = body.inner_text(timeout=5_000)
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the page text")
            return ""
        return (raw or "")[:limit]

    def title(self) -> str:
        try:
            return self._page.title() or ""
        except Exception:  # noqa: BLE001
            return ""

    def url(self) -> str:
        try:
            return self._page.url or ""
        except Exception:  # noqa: BLE001
            return ""

    # ---- diagnostics -----------------------------------------------------

    def _note_fallback(self, resolution: Resolution) -> None:
        description = resolution.describe()
        if description in self.fallbacks:
            return
        self.fallbacks.append(description)
        logger.info(
            "Selector fallback used",
            extra={
                "chain": resolution.chain_name,
                "candidate_index": resolution.index,
                "candidate": resolution.candidate.describe(),
                "loose": resolution.loose,
            },
        )

    def reset_fallbacks(self) -> None:
        self.fallbacks.clear()
