"""The floating HUD, as a standalone process.

Run as ``python -m ciel.ui.hud``; reads one state name per line on stdin and
renders a small always-on-top pill.

**Why a separate process.** AppKit owns the main thread — ``NSApp.run()`` never
returns, and Cocoa refuses to draw from anywhere else. Ciel's pipeline also
wants the main thread for its asyncio loop. Rather than bolt one onto the other
with a threaded run loop (fragile, and a well-known source of mysterious
crashes on macOS), the HUD gets its own process and a pipe. That also means a
HUD that dies takes nothing with it: Ciel keeps talking, just without a light.

The window is deliberately inert — it ignores mouse events, never takes focus,
and follows you across Spaces. It is a status light, not an app.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

import objc
from AppKit import (
    # Re-exported by Cocoa, so no pyobjc-framework-Quartz dependency is needed
    # just to breathe an opacity animation.
    CABasicAnimation,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
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

# Raised above normal floating windows so the pill survives on top of most
# things without being a screen-saver-level nuisance.
STATUS_WINDOW_LEVEL = 25


@dataclass(frozen=True)
class Look:
    label: str
    # RGB 0-1, plus whether the dot should pulse.
    color: tuple[float, float, float]
    pulse: bool = False


# The visual vocabulary. Colors are chosen to read at a glance in peripheral
# vision: cool and dim when nothing is happening, warm and bright when Ciel
# wants something from you or is doing work on your behalf.
LOOKS: dict[str, Look] = {
    "idle": Look("Ciel", (0.45, 0.47, 0.52)),
    "listening": Look("Listening", (0.30, 0.78, 0.45), pulse=True),
    "thinking": Look("Thinking", (0.95, 0.70, 0.25), pulse=True),
    # Speaking its reasoning aloud — between thinking's amber and speaking's
    # blue on purpose: it *is* speech, but not yet the answer.
    "reasoning": Look("Reasoning", (0.66, 0.45, 0.95), pulse=True),
    "speaking": Look("Speaking", (0.35, 0.62, 0.95), pulse=True),
    "error": Look("Error", (0.90, 0.35, 0.35)),
}

PILL_WIDTH = 132.0
PILL_HEIGHT = 34.0
DOT_SIZE = 9.0


class HudController(NSObject):
    """Owns the window. All mutation happens on the main thread."""

    def initWithConfig_(self, config: dict):  # noqa: N802 - ObjC naming
        self = objc.super(HudController, self).init()
        if self is None:
            return None

        self._opacity = float(config.get("opacity", 0.92))
        self._hide_when_idle = bool(config.get("hide_when_idle", False))
        scale = float(config.get("scale", 1.0))
        self._width = PILL_WIDTH * scale
        self._height = PILL_HEIGHT * scale

        self._build_window(config.get("position", "bottom-right"), int(config.get("margin", 24)))
        self.applyState_("idle")
        return self

    # ── construction ─────────────────────────────────────────────────────────

    def _build_window(self, position: str, margin: int) -> None:
        frame = NSMakeRect(0, 0, self._width, self._height)

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        self._window.setLevel_(STATUS_WINDOW_LEVEL)
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setHasShadow_(True)
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
        layer.setCornerRadius_(self._height / 2.0)
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.09, 0.11, 0.92).CGColor()
        )
        layer.setBorderWidth_(1.5)
        self._window.setContentView_(container)
        self._container = container

        inset = self._height / 2.0 - DOT_SIZE / 2.0
        dot = NSView.alloc().initWithFrame_(
            NSMakeRect(inset, self._height / 2.0 - DOT_SIZE / 2.0, DOT_SIZE, DOT_SIZE)
        )
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(DOT_SIZE / 2.0)
        container.addSubview_(dot)
        self._dot = dot

        text_x = inset + DOT_SIZE + 9.0
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(text_x, self._height / 2.0 - 9.0, self._width - text_x - 10.0, 18.0)
        )
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(12.0))
        label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.96, 1.0))
        container.addSubview_(label)
        self._label = label

        self._position_window(position, margin)
        self._window.orderFrontRegardless()

    def _position_window(self, position: str, margin: int) -> None:
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        # visibleFrame excludes the menu bar and Dock, so the pill never hides
        # underneath either of them.
        area = screen.visibleFrame()

        left = area.origin.x + margin
        right = area.origin.x + area.size.width - self._width - margin
        bottom = area.origin.y + margin
        top = area.origin.y + area.size.height - self._height - margin

        x, y = {
            "top-left": (left, top),
            "top-right": (right, top),
            "bottom-left": (left, bottom),
            "bottom-right": (right, bottom),
        }.get(position, (right, bottom))

        self._window.setFrameOrigin_((x, y))

    # ── state ────────────────────────────────────────────────────────────────

    def applyState_(self, state: str) -> None:  # noqa: N802 - ObjC naming
        look = LOOKS.get(state, LOOKS["idle"])

        red, green, blue = look.color
        self._dot.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 1.0).CGColor()
        )
        # Tint the pill's outline with the state colour too. A 9-point dot on a
        # near-black pill all but disappears against a dark terminal — which is
        # exactly where this thing sits — so the whole silhouette has to carry
        # the signal, not just the dot.
        self._container.layer().setBorderColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                red, green, blue, 0.30 if state == "idle" else 0.75
            ).CGColor()
        )
        self._label.setStringValue_(look.label)

        self._dot.layer().removeAnimationForKey_("pulse")
        if look.pulse:
            # A slow breath rather than a blink: enough motion to catch the eye
            # in peripheral vision without being the most urgent thing on screen.
            pulse = CABasicAnimation.animationWithKeyPath_("opacity")
            pulse.setFromValue_(1.0)
            pulse.setToValue_(0.35)
            pulse.setDuration_(0.75)
            pulse.setAutoreverses_(True)
            pulse.setRepeatCount_(1e9)
            self._dot.layer().addAnimation_forKey_(pulse, "pulse")

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
