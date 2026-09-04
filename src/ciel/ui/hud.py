"""The floating HUD, as a standalone process.

Run as ``python -m ciel.ui.hud``; reads one state name per line on stdin and
renders a small always-on-top chip.

**Why a separate process.** AppKit owns the main thread — ``NSApp.run()`` never
returns, and Cocoa refuses to draw from anywhere else. Ciel's pipeline also
wants the main thread for its asyncio loop. Rather than bolt one onto the other
with a threaded run loop (fragile, and a well-known source of mysterious
crashes on macOS), the HUD gets its own process and a pipe. That also means a
HUD that dies takes nothing with it: Ciel keeps talking, just without a light.

The window is deliberately inert — it ignores mouse events, never takes focus,
and follows you across Spaces. It is a status light, not an app.

**The look** is the Chart's state chip (``remote/chart.html``, the
"Instrument" design), drawn in Core Animation instead of CSS: a cut-corner
polygon on a near-black ground tinted with the state colour, a one-point
border in that colour, a six-point dot with a soft glow that breathes, and a
semibold monospace label in wide-tracked capitals. The two must read as the
same instrument — one is the corner of your screen, the other a browser tab
— so the colours, cuts, and timings below are copied from the page's CSS
rather than approximated.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

import objc
from AppKit import (
    # Core Animation classes are re-exported by Cocoa, so no
    # pyobjc-framework-Quartz import is needed to draw a shape or breathe.
    CAAnimationGroup,
    CABasicAnimation,
    CALayer,
    CAMediaTimingFunction,
    CAShapeLayer,
    CATransaction,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSKernAttributeName,
    NSMakeRect,
    NSScreen,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSObject

# Raised above normal floating windows so the chip survives on top of most
# things without being a screen-saver-level nuisance.
STATUS_WINDOW_LEVEL = 25

RGB = tuple[float, float, float]


def _hex(code: str) -> RGB:
    code = code.lstrip("#")
    return tuple(int(code[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _blend(under: RGB, over: RGB, alpha: float) -> RGB:
    """``over`` at ``alpha`` composited onto an opaque ``under``."""
    return tuple(u * (1.0 - alpha) + o * alpha for u, o in zip(under, over))  # type: ignore[return-value]


def _cg(rgb: RGB, alpha: float = 1.0):
    r, g, b = rgb
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, alpha).CGColor()


@dataclass(frozen=True)
class Look:
    label: str
    # The state colour (``--st-*`` on the Chart): the dot, its glow, the
    # border, and the tint over the ground.
    color: RGB
    # The label's colour — the Chart hand-picks a pale version per state
    # rather than deriving one, so these are copied too.
    text: RGB
    border_alpha: float
    tint_alpha: float
    # Seconds for one full breath (CSS ``br`` keyframes); 0 means still.
    breath_s: float = 0.0


# The ground under every chip: the Chart's page background, near the top of
# its gradient where the header sits.
GROUND: RGB = _hex("0a1620")

# The visual vocabulary, verbatim from chart.html's ``#state`` rules. Cool and
# dim when nothing is happening; warm and bright when Ciel wants something
# from you or is doing work on your behalf.
LOOKS: dict[str, Look] = {
    "idle": Look("Ciel", _hex("8fa8b3"), _hex("b6c6cd"), border_alpha=0.42, tint_alpha=0.10),
    "listening": Look("Listening", _hex("7adea8"), _hex("a6ecc7"), 0.50, 0.10, breath_s=3.4),
    "thinking": Look("Thinking", _hex("e8c98a"), _hex("f0dbaf"), 0.55, 0.12, breath_s=3.4),
    # Speaking its reasoning aloud — between thinking's gold and speaking's
    # cyan on purpose: it *is* speech, but not yet the answer.
    "reasoning": Look("Reasoning", _hex("b89be8"), _hex("d2bef4"), 0.55, 0.12, breath_s=3.4),
    "speaking": Look("Speaking", _hex("a0f0ff"), _hex("c7f4fd"), 0.60, 0.12, breath_s=1.6),
    "error": Look("Error", _hex("e88a8a"), _hex("f3b7b7"), 0.60, 0.12),
}

# Geometry at scale 1, in points. The Chart's chip is ``padding: 6px 13px 6px
# 10px`` around a 10px mono label with a 6px dot and an 8px gap; the HUD sits
# further from the eye than a browser tab, so it is one step larger.
CHIP_HEIGHT = 30.0
CUT = 8.0  # the corner cut, top-right and bottom-left
PAD_LEFT = 11.0
PAD_RIGHT = 14.0
DOT_SIZE = 7.0
DOT_GAP = 9.0
FONT_SIZE = 11.0
TRACKING_EM = 0.2  # ``letter-spacing: .2em``
GLOW_RADIUS = 4.5  # ``box-shadow: 0 0 9px`` — half the CSS blur
BORDER = 1.0


class HudController(NSObject):
    """Owns the window. All mutation happens on the main thread."""

    def initWithConfig_(self, config: dict):  # noqa: N802 - ObjC naming
        self = objc.super(HudController, self).init()
        if self is None:
            return None

        self._opacity = float(config.get("opacity", 0.92))
        self._hide_when_idle = bool(config.get("hide_when_idle", False))
        self._scale = float(config.get("scale", 1.0))
        self._position = str(config.get("position", "bottom-right"))
        self._margin = int(config.get("margin", 24))
        self._height = CHIP_HEIGHT * self._scale
        self._width = 0.0

        self._build_window()
        self.applyState_("idle")
        return self

    # ── construction ─────────────────────────────────────────────────────────

    def _build_window(self) -> None:
        s = self._scale
        frame = NSMakeRect(0, 0, 100.0 * s, self._height)

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        self._window.setLevel_(STATUS_WINDOW_LEVEL)
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        # The instrument is flat: no drop shadow, the tinted border is the
        # edge — same as the chips on the page.
        self._window.setHasShadow_(False)
        # A status light must never eat a click meant for the window beneath it,
        # and must never steal focus from whatever you're actually doing.
        self._window.setIgnoresMouseEvents_(True)
        self._window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._window.setAlphaValue_(self._opacity)

        container = NSView.alloc().initWithFrame_(frame)
        container.setWantsLayer_(True)
        layer = container.layer()
        layer.setBackgroundColor_(_cg(GROUND, 0.96))
        self._window.setContentView_(container)
        self._container = container

        # The cut corners: a mask clips the fill to the polygon, and a second
        # shape layer strokes the same polygon for the border. The stroke is
        # centred on the edge, so half of it falls outside the mask — hence
        # twice the width, for exactly one border's worth showing.
        mask = CAShapeLayer.layer()
        layer.setMask_(mask)
        self._mask = mask

        border = CAShapeLayer.layer()
        border.setFillColor_(NSColor.clearColor().CGColor())
        border.setLineWidth_(2.0 * BORDER * s)
        layer.addSublayer_(border)
        self._border = border

        # The dot is a bare layer, not a view: a layer's anchor point is its
        # centre, so the breath's scale grows it in place instead of from a
        # corner.
        dot = CALayer.layer()
        dot_size = DOT_SIZE * s
        dot.setBounds_(NSMakeRect(0, 0, dot_size, dot_size))
        dot.setCornerRadius_(dot_size / 2.0)
        dot.setPosition_((PAD_LEFT * s + dot_size / 2.0, self._height / 2.0))
        dot.setShadowOffset_((0.0, 0.0))
        dot.setShadowRadius_(GLOW_RADIUS * s)
        dot.setShadowOpacity_(1.0)
        layer.addSublayer_(dot)
        self._dot = dot

        label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        container.addSubview_(label)
        self._label = label
        self._font = NSFont.monospacedSystemFontOfSize_weight_(FONT_SIZE * s, NSFontWeightSemibold)

        self._window.orderFrontRegardless()

    @objc.python_method
    def _polygon(self, width: float):
        """The chip outline: corners cut at the top-right and bottom-left, as
        the Chart's ``clip-path`` — in AppKit's y-up coordinates."""
        h = self._height
        cut = CUT * self._scale
        path = NSBezierPath.bezierPath()
        path.moveToPoint_((0.0, cut))
        path.lineToPoint_((0.0, h))
        path.lineToPoint_((width - cut, h))
        path.lineToPoint_((width, h - cut))
        path.lineToPoint_((width, 0.0))
        path.lineToPoint_((cut, 0.0))
        path.closePath()
        return path.CGPath()

    @objc.python_method
    def _layout(self, width: float) -> None:
        """Resize the chip to ``width`` and re-anchor it to its corner."""
        if abs(width - self._width) < 0.5:
            return
        self._width = width
        frame = NSMakeRect(0, 0, width, self._height)
        self._container.setFrame_(frame)
        outline = self._polygon(width)
        self._mask.setFrame_(frame)
        self._mask.setPath_(outline)
        self._border.setFrame_(frame)
        self._border.setPath_(outline)
        self._position_window()

    def _position_window(self) -> None:
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        # visibleFrame excludes the menu bar and Dock, so the chip never hides
        # underneath either of them.
        area = screen.visibleFrame()
        margin = self._margin

        left = area.origin.x + margin
        right = area.origin.x + area.size.width - self._width - margin
        bottom = area.origin.y + margin
        top = area.origin.y + area.size.height - self._height - margin

        x, y = {
            "top-left": (left, top),
            "top-right": (right, top),
            "bottom-left": (left, bottom),
            "bottom-right": (right, bottom),
        }.get(self._position, (right, bottom))

        self._window.setFrame_display_(NSMakeRect(x, y, self._width, self._height), True)

    # ── state ────────────────────────────────────────────────────────────────

    def applyState_(self, state: str) -> None:  # noqa: N802 - ObjC naming
        look = LOOKS.get(state, LOOKS["idle"])
        s = self._scale

        # The label first: its width decides the chip's.
        kern = FONT_SIZE * s * TRACKING_EM
        text = NSAttributedString.alloc().initWithString_attributes_(
            look.label.upper(),
            {
                NSFontAttributeName: self._font,
                NSKernAttributeName: kern,
                NSForegroundColorAttributeName: NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    *look.text, 1.0
                ),
            },
        )
        # Measure through the cell, not the string: the cell knows its own
        # horizontal inset and applies the kerning the string's ``size()``
        # under-reports, and a chip one glyph too narrow clips the label.
        self._label.setAttributedStringValue_(text)
        size = self._label.cell().cellSize()
        text_x = (PAD_LEFT + DOT_SIZE + DOT_GAP) * s
        # The kern trails the last glyph too; don't pad it twice.
        width = text_x + size.width - kern + PAD_RIGHT * s

        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        try:
            self._layout(width)
            self._label.setFrame_(
                NSMakeRect(text_x, (self._height - size.height) / 2.0, size.width + 2.0, size.height)
            )

            self._container.layer().setBackgroundColor_(
                _cg(_blend(GROUND, look.color, look.tint_alpha), 0.96)
            )
            self._border.setStrokeColor_(_cg(look.color, look.border_alpha))
            self._dot.setBackgroundColor_(_cg(look.color))
            # Idle has no glow on the page either: the dot is just a dot.
            self._dot.setShadowColor_(_cg(look.color, 0.0 if state == "idle" else 1.0))
        finally:
            CATransaction.commit()

        self._dot.removeAnimationForKey_("breath")
        if look.breath_s:
            # The Chart's ``br`` keyframes: dim and slightly small at the ends,
            # full and slightly large in the middle — a breath, not a blink.
            # Autoreverse over half the period gives the same cycle.
            fade = CABasicAnimation.animationWithKeyPath_("opacity")
            fade.setFromValue_(0.45)
            fade.setToValue_(1.0)
            grow = CABasicAnimation.animationWithKeyPath_("transform.scale")
            grow.setFromValue_(0.88)
            grow.setToValue_(1.08)
            breath = CAAnimationGroup.animation()
            breath.setAnimations_([fade, grow])
            breath.setDuration_(look.breath_s / 2.0)
            breath.setAutoreverses_(True)
            breath.setRepeatCount_(1e9)
            breath.setTimingFunction_(CAMediaTimingFunction.functionWithName_("easeInEaseOut"))
            self._dot.addAnimation_forKey_(breath, "breath")

        if self._hide_when_idle:
            self._window.setAlphaValue_(0.0 if state == "idle" else self._opacity)
        else:
            # Idle is dimmed rather than hidden — still visibly "Ciel is
            # running", which is most of the point of having an indicator.
            self._window.setAlphaValue_(
                self._opacity * (0.55 if state == "idle" else 1.0)
            )

    def handleLine_(self, line: str) -> None:  # noqa: N802 - ObjC naming
        if line == "quit":
            NSApplication.sharedApplication().terminate_(None)
            return
        self.applyState_(line)


def _pump_stdin(controller: HudController) -> None:
    """Read state names and hand them to the main thread.

    Cocoa is not thread-safe; every UI mutation has to be marshalled onto the
    main thread, which is what performSelectorOnMainThread does here.
    """
    for raw in sys.stdin:
        line = raw.strip().lower()
        if not line:
            continue
        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "handleLine:", line, False
        )
    # stdin closed: Ciel exited (or crashed). Either way, take the light down.
    controller.performSelectorOnMainThread_withObject_waitUntilDone_(
        "handleLine:", "quit", False
    )


def main() -> int:
    import json

    config: dict = {}
    if len(sys.argv) > 1:
        try:
            config = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            pass

    app = NSApplication.sharedApplication()
    # Accessory: no Dock icon, no app menu. It is a light, not a program.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    controller = HudController.alloc().initWithConfig_(config)

    reader = threading.Thread(target=_pump_stdin, args=(controller,), daemon=True)
    reader.start()

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
