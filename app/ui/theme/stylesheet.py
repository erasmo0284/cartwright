"""The application stylesheet, built from a token set.

Why this file is so short for a themed application
--------------------------------------------------

Qt does not layer QSS on top of the native style the way CSS layers on top of
a browser default. Setting a stylesheet installs ``QStyleSheetStyle``, and for
any element that a rule actually addresses, that style draws the element
itself instead of delegating to ``windows11``. What is lost is exactly what
makes a control feel native: the hover and press *animations*, the Windows 11
control geometry and the system accent. Elements with no matching rule still
fall through to the base style, which is the loophole this stylesheet is
designed around.

So the template below styles only surfaces this application invents -- cards,
badges, the navigation list, the banner, the guard rows -- each addressed by
``objectName`` or a dynamic property, and it leaves ``QPushButton``,
``QComboBox``, ``QCheckBox``, ``QSpinBox`` and ``QLineEdit`` completely
untouched so they keep native rendering. The three named buttons and
``#UrlInput`` are the deliberate exceptions: a primary action and the paste
field are worth hand-drawing, the settings form is not.

Other Qt-specific constraints the template respects
---------------------------------------------------

* QSS has **no inheritance**. A colour set on ``#Card`` does not reach a
  ``QLabel`` inside it, so container rules are always paired with an explicit
  rule for the text they contain.
* Customising one sub-control of a complex widget obliges you to customise
  all of them (the classic ``QComboBox`` arrow and ``QScrollBar`` handle
  trap). That is why ``#ScrollAreaFlat`` only removes the frame and never
  touches ``QScrollBar``: the native scrollbars are better than any we would
  hand-draw here.
* There is no ``box-shadow`` and there are no transitions. Elevation is
  expressed with a 1px stroke plus a lighter surface, which is what Windows 11
  itself does.
* A widget's own ``setStyleSheet`` beats an inherited rule, so no widget in
  this application is allowed to call it -- everything comes from here.
* No ``font-size`` anywhere. Pixel font sizes do not scale with the display
  scale factor, so text would stay put while its container grew at 150%.
  Sizing is done in Python with point sizes; see :mod:`app.ui.theme.fonts`.

The template is a module-level string rather than a bundled ``.qss`` data
file: it has to ship inside a PyInstaller one-file build, and a string cannot
go missing from the bundle or be edited into an unparsable state by a user.
"""

from __future__ import annotations

from string import Template
from typing import Final, Mapping

_QSS_TEMPLATE: Final[str] = """
/* ----------------------------------------------------------------------
 * Window and page background
 * ------------------------------------------------------------------- */

QMainWindow, QDialog, #Page {
    background-color: ${bg};
}

/* ----------------------------------------------------------------------
 * Cards: the only container that draws a surface
 * ------------------------------------------------------------------- */

#Card {
    background-color: ${surface};
    border: ${strokeWidth} solid ${stroke};
    border-radius: ${radiusLg};
}

#Card[muted="true"] {
    background-color: ${surfaceAlt};
}

/* A header strip inside a card. Transparent so the card's own corner radius
 * is not double-drawn; separated by a hairline instead of a fill. */
#CardHeader {
    background-color: transparent;
    border: none;
    border-bottom: ${strokeWidth} solid ${stroke};
    padding-bottom: ${spaceSm};
}

#Separator {
    background-color: ${stroke};
    border: none;
    max-height: ${strokeWidth};
    min-height: ${strokeWidth};
}

#SeparatorVertical {
    background-color: ${stroke};
    border: none;
    max-width: ${strokeWidth};
    min-width: ${strokeWidth};
}

/* ----------------------------------------------------------------------
 * Text roles. Colour and weight only -- size comes from fonts.py.
 * QSS does not inherit, so every role is addressed directly.
 * ------------------------------------------------------------------- */

QLabel[role="title"] {
    color: ${text};
    font-weight: 600;
}

QLabel[role="subtitle"] {
    color: ${textMuted};
}

QLabel[role="caption"] {
    color: ${textMuted};
}

QLabel[role="metric"] {
    color: ${text};
    font-weight: 600;
}

QLabel[role="metricLabel"] {
    color: ${textMuted};
}

QLabel[role="sectionHeading"] {
    color: ${text};
    font-weight: 600;
}

QLabel[role="title"]:disabled,
QLabel[role="metric"]:disabled,
QLabel[role="sectionHeading"]:disabled,
QLabel[role="subtitle"]:disabled,
QLabel[role="caption"]:disabled,
QLabel[role="metricLabel"]:disabled {
    color: ${textDisabled};
}

/* An inline hint under an input. It carries a severity because the same
 * line is used for "nothing is bought by checking" and for "that link was
 * not an Amazon product", and those two must not look identical. */
#InlineHint {
    color: ${textMuted};
}

#InlineHint[severity="success"] { color: ${success}; }
#InlineHint[severity="warning"] { color: ${warning}; }
#InlineHint[severity="blocked"] { color: ${blocked}; }
#InlineHint[severity="error"]   { color: ${danger}; font-weight: 600; }
#InlineHint[severity="info"]    { color: ${info}; }
#InlineHint[severity="neutral"] { color: ${textMuted}; }
#InlineHint:disabled { color: ${textDisabled}; }

/* ----------------------------------------------------------------------
 * Status badges. The severity property carries the meaning; the colours
 * are the (foreground, background) pairs from tokens.Semantic.
 * ------------------------------------------------------------------- */

#Badge {
    border: none;
    border-radius: ${radiusSm};
    padding: ${spaceXs} ${spaceSm};
    font-weight: 600;
    color: ${neutral};
    background-color: ${neutralBg};
}

#Badge[severity="success"] {
    color: ${success};
    background-color: ${successBg};
}

#Badge[severity="warning"] {
    color: ${warning};
    background-color: ${warningBg};
}

#Badge[severity="blocked"] {
    color: ${blocked};
    background-color: ${blockedBg};
}

#Badge[severity="error"] {
    color: ${danger};
    background-color: ${dangerBg};
}

#Badge[severity="info"] {
    color: ${info};
    background-color: ${infoBg};
}

#Badge[severity="neutral"] {
    color: ${neutral};
    background-color: ${neutralBg};
}

/* An 8px dot for list rows, where a full badge would be too heavy.
 * radiusSm is exactly half of 8px, so the square renders as a circle. */
#StatusDot {
    min-width: 8px;
    max-width: 8px;
    min-height: 8px;
    max-height: 8px;
    border: none;
    border-radius: ${radiusSm};
    background-color: ${neutral};
}

#StatusDot[severity="success"] { background-color: ${success}; }
#StatusDot[severity="warning"] { background-color: ${warning}; }
#StatusDot[severity="blocked"] { background-color: ${blocked}; }
#StatusDot[severity="error"]   { background-color: ${dangerFill}; }
#StatusDot[severity="info"]    { background-color: ${info}; }
#StatusDot[severity="neutral"] { background-color: ${neutral}; }

/* ----------------------------------------------------------------------
 * Guard rows: one line per purchase rule that was checked.
 * The left edge carries the outcome, so the list is readable without
 * relying on colour alone (each row also shows a check/cross icon).
 * ------------------------------------------------------------------- */

#GuardRow {
    background-color: transparent;
    border: none;
    border-left: 3px solid ${stroke};
    border-radius: 0px;
    padding: ${spaceSm} ${spaceMd};
}

#GuardRow[status="pass"] {
    border-left: 3px solid ${success};
    background-color: ${successBg};
}

#GuardRow[status="fail"] {
    border-left: 3px solid ${blocked};
    background-color: ${blockedBg};
}

#GuardRow[status="skipped"] {
    border-left: 3px solid ${strokeStrong};
    background-color: ${surfaceAlt};
}

#GuardRow QLabel[role="title"] { color: ${text}; }
#GuardRow QLabel[role="caption"] { color: ${textMuted}; }

/* ----------------------------------------------------------------------
 * Left navigation
 * ------------------------------------------------------------------- */

#NavList {
    background-color: transparent;
    border: none;
    outline: none;
    padding: ${spaceSm};
}

#NavList::item {
    color: ${text};
    background-color: transparent;
    border: none;
    border-radius: ${radius};
    padding: ${spaceSm} ${spaceMd};
    margin-bottom: ${spaceXs};
}

#NavList::item:hover {
    background-color: ${surfaceHover};
}

#NavList::item:selected {
    color: ${text};
    background-color: ${surface};
    border: ${strokeWidth} solid ${stroke};
}

/* Keep the selection visible when the list loses focus: without this rule
 * Qt greys the selected row out and the current page looks unselected. */
#NavList::item:selected:!active {
    color: ${text};
    background-color: ${surface};
    border: ${strokeWidth} solid ${stroke};
}

#NavList::item:disabled {
    color: ${textDisabled};
}

/* ----------------------------------------------------------------------
 * The three hand-drawn buttons. Everything else stays native.
 * ------------------------------------------------------------------- */

#PrimaryButton {
    background-color: ${accent};
    color: ${accentText};
    border: ${strokeWidth} solid ${accent};
    border-radius: ${radiusSm};
    padding: ${spaceSm} ${spaceLg};
    font-weight: 600;
    min-height: 16px;
}

#PrimaryButton:hover {
    background-color: ${accentHover};
    border-color: ${accentHover};
}

#PrimaryButton:pressed {
    background-color: ${accentPressed};
    border-color: ${accentPressed};
}

#PrimaryButton:focus {
    border: 2px solid ${focusRing};
}

#PrimaryButton:disabled {
    background-color: ${surfaceAlt};
    border-color: ${stroke};
    color: ${textDisabled};
}

#DangerButton {
    background-color: ${dangerFill};
    color: ${dangerFillText};
    border: ${strokeWidth} solid ${dangerFill};
    border-radius: ${radiusSm};
    padding: ${spaceSm} ${spaceLg};
    font-weight: 600;
    min-height: 16px;
}

#DangerButton:hover {
    background-color: ${dangerFillHover};
    border-color: ${dangerFillHover};
}

#DangerButton:pressed {
    background-color: ${dangerFillPressed};
    border-color: ${dangerFillPressed};
}

#DangerButton:focus {
    border: 2px solid ${focusRing};
}

#DangerButton:disabled {
    background-color: ${surfaceAlt};
    border-color: ${stroke};
    color: ${textDisabled};
}

/* A text-weight button for tertiary actions ("Open on Amazon", "Clear").
 * Borderless until hovered, so a row of them does not compete with the
 * primary action. */
#SubtleButton {
    background-color: transparent;
    color: ${accent};
    border: ${strokeWidth} solid transparent;
    border-radius: ${radiusSm};
    padding: ${spaceXs} ${spaceSm};
}

#SubtleButton:hover {
    background-color: ${surfaceHover};
    border-color: ${stroke};
}

#SubtleButton:pressed {
    background-color: ${surfaceAlt};
    border-color: ${strokeStrong};
}

#SubtleButton:focus {
    border: 2px solid ${focusRing};
}

#SubtleButton:disabled {
    color: ${textDisabled};
    background-color: transparent;
    border-color: transparent;
}

/* ----------------------------------------------------------------------
 * Test-mode banner: shown whenever the app will stop short of ordering.
 * It must be impossible to mistake for decoration, and impossible to
 * mistake for an error.
 * ------------------------------------------------------------------- */

#TestModeBanner {
    background-color: ${warningBg};
    border: ${strokeWidth} solid ${warning};
    border-radius: ${radius};
    padding: ${spaceSm} ${spaceMd};
}

#TestModeBanner QLabel {
    color: ${warning};
    font-weight: 600;
    background-color: transparent;
}

/* ----------------------------------------------------------------------
 * Empty states: a dashed outline reads as "nothing here yet" rather than
 * as a broken card.
 * ------------------------------------------------------------------- */

#EmptyState {
    background-color: transparent;
    border: ${strokeWidth} dashed ${strokeStrong};
    border-radius: ${radiusLg};
    padding: ${spaceXl};
}

#EmptyState QLabel {
    color: ${textMuted};
    background-color: transparent;
}

#EmptyState QLabel[role="title"] {
    color: ${text};
    font-weight: 600;
}

/* ----------------------------------------------------------------------
 * The paste field. The one QLineEdit we draw ourselves, because pasting a
 * product link is the app's front door and deserves to look like it.
 * The focus state thickens only the bottom border, mirroring Windows 11's
 * own accent underline, so nothing reflows when focus arrives.
 * ------------------------------------------------------------------- */

#UrlInput {
    background-color: ${surface};
    color: ${text};
    border: ${strokeWidth} solid ${strokeStrong};
    border-bottom: 2px solid ${strokeStrong};
    border-radius: ${radiusSm};
    padding: ${spaceSm} ${spaceMd};
    selection-background-color: ${accent};
    selection-color: ${accentText};
}

#UrlInput:hover {
    background-color: ${surfaceHover};
}

#UrlInput:focus {
    background-color: ${surface};
    border-bottom: 2px solid ${accent};
}

#UrlInput[invalid="true"] {
    border-bottom: 2px solid ${dangerFill};
}

#UrlInput:disabled {
    background-color: ${surfaceAlt};
    color: ${textDisabled};
    border-color: ${stroke};
}

/* ----------------------------------------------------------------------
 * Flat scroll area. Frame and viewport background only -- QScrollBar and
 * its sub-controls are deliberately left native, because customising one
 * sub-control would force us to hand-draw the handle, groove, both
 * arrows and all their states.
 * ------------------------------------------------------------------- */

#ScrollAreaFlat {
    background-color: transparent;
    border: none;
}

#ScrollAreaFlat > QWidget > QWidget {
    background-color: transparent;
}

/* ----------------------------------------------------------------------
 * Tooltips. Styled because Qt's default tooltip ignores the colour scheme
 * and shows black-on-yellow in dark mode.
 * ------------------------------------------------------------------- */

QToolTip {
    background-color: ${surface};
    color: ${text};
    border: ${strokeWidth} solid ${strokeStrong};
    padding: ${spaceXs} ${spaceSm};
}
"""


def build_qss(tokens: Mapping[str, str]) -> str:
    """Render the stylesheet for ``tokens``.

    Uses :meth:`string.Template.substitute`, never ``safe_substitute``: a
    misspelled placeholder must raise :class:`KeyError` while the application
    is starting up (and in the test suite), instead of emitting a literal
    ``${accnt}`` into the stylesheet, where Qt would silently discard the
    whole declaration and the user would get an unstyled window with no
    explanation.
    """
    return Template(_QSS_TEMPLATE).substitute(tokens)


def template_source() -> str:
    """The raw template. Exposed so tests can extract its placeholders."""
    return _QSS_TEMPLATE
