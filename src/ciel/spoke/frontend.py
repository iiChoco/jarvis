"""The spoke's frontend: the audio state machine, with the hub for a brain.

This is the pipeline's frame loop with the thinking taken out. There
is one microphone and one reader of it — this loop — and the same
small state machine: before the wake word a frame is noise to be
scored, during an utterance it is speech to be buffered, while Ciel is
talking it is a possible interruption. What changes is what happens to
an utterance once it is whole: instead of a brain, it goes up the wire
as a ``say``, and what comes back down — ``turn.sentence`` by
``turn.sentence`` — is played here and receipted, so the hub's voice
sink is paced by these speakers exactly as the single process's was.

Everything that must be in the room stays here: the wake word, the
endpointer, the speaker gate (identity before transcription — a
stranger's sentence never leaves the machine), STT, TTS, the player
and its ~50 ms barge-in stop, the follow-up window, the Cauchy hold,
the mute sentinel, the HUD. Confirm-tier questions arrive as
``confirm.request`` and are spoken and listened for here; the yes or
no goes back as ``confirm.answer``. Timers and Vigil nudges arrive as
``deliver.speak`` and are rung and spoken at idle.

What is deliberately *not* here yet: the command grammar's fast path
and local timer ringing (the hub handles both, one loopback hop away;
they return in the resilience phase, for when the hub is a server
that might be unreachable), and the typed lane (the hub's terminal).

Degrades to nothing, loudly-then-quietly, like every lane: a hub that
is down is one spoken notice, then a chime per swallowed utterance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from pathlib import Path
from typing import Any

import numpy as np

from ciel.audio.input import MicStream, float_to_pcm
from ciel.audio.output import Player
from ciel.audio.speaker import build_speaker_gate
from ciel.audio.vad import Endpointer
from ciel.audio.wake import WakeDetector, build_wake_detector
from ciel.commands import match as match_command
from ciel.config import SAMPLE_RATE, Config
from ciel.pipeline import _SLEEP_GAP_S, build_tts, trails_off
from ciel.reload import SourceWatcher, default_roots
from ciel.schedule import State
from ciel.spoke.client import HubClient
from ciel.spoke.executor import Executor
from ciel.spoke.publisher import Heartbeat, PublishQueue
from ciel.stt import SpeechToText, build_stt
from ciel.tts.base import TextToSpeech
from ciel.ui.indicator import Indicator, build_indicator

log = logging.getLogger(__name__)

_HUB_BEGIN_TIMEOUT_S = 30.0
"""How long a sent utterance may wait for the hub's ``turn.begin``
before the spoke gives up on it — the hub is up (the say was acked)
but wedged, or serving something long. The user gets a chime and the
mic back; the say stays in the ledger and lands when the hub does."""


def ring_pcm(rate: int) -> bytes:
    """The timer ring: a rising two-note figure, played twice."""

    def note(freq: float, dur: float) -> np.ndarray:
        t = np.arange(int(rate * dur), dtype=np.float32) / rate
        fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.015)
        return 0.2 * np.sin(2 * np.pi * freq * t) * fade

    gap = np.zeros(int(rate * 0.08), dtype=np.float32)
    figure = np.concatenate([note(660.0, 0.13), note(880.0, 0.18)])
    return float_to_pcm(np.concatenate([figure, gap, figure]).astype(np.float32))


def chime_pcm(rate: int) -> bytes:
    """The soft tone marking the end of spoken reasoning — and, here,
    the "hub unreachable" shrug."""
    t = np.arange(int(rate * 0.12), dtype=np.float32) / rate
    fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.02)
    return float_to_pcm((0.12 * np.sin(2 * np.pi * 880.0 * t) * fade).astype(np.float32))


async def _one(data: bytes):
    yield data


class Spoke:
    """Runs the room until stopped."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._stt: SpeechToText = build_stt(config.stt)
        self._tts: TextToSpeech = build_tts(config)
        self._wake: WakeDetector = build_wake_detector(config.wake)
        self._endpointer = Endpointer(
            config.audio, noise_floor=lambda: self._noise_floor
        )
        self._confirm_endpointer = Endpointer(
            config.audio, noise_floor=lambda: self._noise_floor
        )
        """The broker's own endpointer, here: the answer window's
        listening, never the turn's."""
        self._speaker = build_speaker_gate(config.voice)
        self._indicator: Indicator = build_indicator(config.ui)
        self._link = HubClient(
            config.spoke,
            on_frame=self._on_frame,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )

        # The Mac's senses and watchers, publishing to the hub (see
        # spoke/publisher.py), and the hub's hands here (spoke/executor.py).
        # The same modules the single process runs; only their queue and
        # their caller changed.
        self._publish = PublishQueue(self._link.send, node=config.spoke.client_id)
        self._watchers: list = []
        self._work_watcher = None
        self._calendar = None
        self._locator = None
        self._presence = None
        if config.proactive.enabled:
            from ciel.proactive.presence import PresenceProbe
            from ciel.proactive.work import WorkWatcher

            self._presence = PresenceProbe(config.proactive)
            self._work_watcher = WorkWatcher(config.proactive.watches_file, self._publish)
            self._watchers.append(self._work_watcher)
            if config.proactive.calendar_enabled and config.proactive.calendar_source != "google":
                from ciel.proactive.calendar import CalendarWatcher

                self._calendar = CalendarWatcher(config.proactive, self._publish)
                self._watchers.append(self._calendar)
        if config.location.enabled:
            from ciel.location import Locator

            self._locator = Locator(config.location)
            if config.proactive.enabled:
                from ciel.proactive.location import LocationWatcher

                self._watchers.append(
                    LocationWatcher(config.location, self._locator, self._publish)
                )
        if config.sections.enabled and config.proactive.enabled:
            # Portable in principle, Mac-bound in practice: its cookie is
            # minted by a browser profile on this machine, so the watcher
            # runs here and publishes like the other Mac watchers.
            from ciel.proactive.sections import SectionsWatcher

            self._watchers.append(
                SectionsWatcher(config.sections, self._publish, mail=config.mail)
            )
        messages = None
        if config.messages.enabled:
            from ciel.messages import MessagesClient

            messages = MessagesClient(config.messages)
        self._executor = Executor(
            config, self._link.send,
            messages=messages, locator=self._locator,
            watcher=self._work_watcher, calendar=self._calendar,
        )
        self._heartbeat = Heartbeat(
            self._link.send, self._presence, self._work_watcher,
            node=config.spoke.client_id,
            interval_s=config.spoke.heartbeat_s,
            idle_threshold_s=config.proactive.presence_idle_s,
        )
        self._mute_sentinel = config.state_dir / "mute"
        self._muted = self._mute_sentinel.exists()
        self._next_mute_check = 0.0
        self._player: Player | None = None
        self._mic: MicStream | None = None

        self._state = State.WAITING
        self._interrupted = False
        self._barge_run = 0
        self._noise_floor: float | None = None
        self._followup_until: float | None = None
        self._spoke = False
        self._pending_text: str | None = None
        self._continue_listening = False

        self._prelude: asyncio.Task[None] | None = None
        """The utterance's identity check and transcription, in flight."""
        self._awaiting_hub = False
        """A say went up; no ``turn.begin`` yet."""
        self._hub_turn: str | None = None
        """The hub turn being played, by id, until its ``turn.end``."""
        self._turn_deadline = 0.0
        self._playing: asyncio.Task[None] | None = None
        self._prev_kind: str | None = None
        self._ack_task: asyncio.Task[None] | None = None
        self._ack_speaking = False
        self._confirm: tuple[str, float] | None = None
        """(confirm_id, deadline) while listening for a spoken answer."""
        self._delivering = False
        self._hub_lost_said = False
        self._reported: tuple[bool, bool] | None = None
        """The last ``voice.state`` sent, so transitions send one frame."""

        self._last_wall = time.time()
        self._watcher: SourceWatcher | None = (
            SourceWatcher(default_roots(config.state_dir))
            if config.dev.autoreload
            else None
        )
        self.reload_requested = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._startup()
        player = Player(
            self._config.audio, self._tts.sample_rate, muted=lambda: self._muted
        )
        self._player = player
        try:
            async with MicStream(self._config.audio) as mic, player:
                self._mic = mic
                greeting = random.choice(self._config.wake.greeting_phrases)
                await player.play(self._tts.stream(greeting))
                mic.drain()
                self._announce_ready()
                self._last_wall = time.time()

                async for frame in mic.frames():
                    now_wall = time.time()
                    if now_wall - self._last_wall > _SLEEP_GAP_S:
                        log.info("wall clock jumped %.0f s — woke from sleep", now_wall - self._last_wall)
                        mic.drain()
                    self._last_wall = now_wall

                    if now_wall >= self._next_mute_check:
                        self._next_mute_check = now_wall + 1.0
                        on_disk = self._mute_sentinel.exists()
                        if on_disk != self._muted:
                            self._set_muted(on_disk)

                    if self._confirm is not None:
                        # A question is out; the room's answer is what
                        # this frame is for.
                        self._feed_confirm(frame)
                        continue

                    self._report_voice_state()

                    if self._state is State.WAITING:
                        if self._watcher is not None and self._watcher.changed:
                            print(f"\nsource changed ({self._watcher.changed.name}) — reloading")
                            with contextlib.suppress(Exception):
                                await player.play(self._tts.stream("Reloading."))
                            self.reload_requested = True
                            break
                        if self._delivering:
                            continue  # the room is Ciel's for a moment
                        self._track_noise_floor(frame)
                        if not self._muted and self._wake.push(frame):
                            await self._acknowledge(player, mic)
                            self._enter_listening()

                    elif self._state is State.LISTENING:
                        if self._followup_expired():
                            pending, self._pending_text = self._pending_text, None
                            if pending:
                                # Trailed off and never came back: answer
                                # what was said rather than forget it.
                                self._state = State.BUSY
                                self._barge_run = 0
                                self._send_turn(pending)
                            else:
                                self._enter_waiting()
                            continue
                        utterance = self._endpointer.push(frame)
                        if utterance is not None:
                            self._state = State.BUSY
                            self._barge_run = 0
                            self._prelude = asyncio.create_task(
                                self._handle_utterance(utterance)
                            )

                    elif self._state is State.BUSY:
                        self._check_barge_in(frame, player)
                        if self._prelude is not None and self._prelude.done():
                            self._prelude.result()
                            self._prelude = None
                        if (
                            self._awaiting_hub
                            and time.monotonic() > self._turn_deadline
                        ):
                            log.warning("the hub never began the turn — giving up on it")
                            self._awaiting_hub = False
                            await self._shrug(player)
                        if (
                            self._prelude is None
                            and not self._awaiting_hub
                            and self._hub_turn is None
                            and (self._playing is None or self._playing.done())
                        ):
                            self._finish_turn(mic)
        finally:
            self._confirm = None
            for task in (self._prelude, self._playing, self._ack_task):
                if task is not None and not task.done():
                    task.cancel()
            self._player = None
            self._mic = None
            await self._shutdown()

    async def _startup(self) -> None:
        started = time.monotonic()
        startup = [
            self._warm_up_stt(),
            self._warm_up_tts(),
            self._wake.start(),
            self._indicator.start(),
            self._link.start(),
            self._heartbeat.start(),
        ]
        if self._speaker is not None:
            startup.append(self._speaker.warm_up())
        if self._watcher is not None:
            startup.append(self._watcher.start())
        # The Mac's watchers start here but never block here, as in the
        # single process: a permission dialog stalls a watcher, not the room.
        startup.extend(w.start() for w in self._watchers)
        await asyncio.gather(*startup)
        log.info("spoke ready in %.1fs", time.monotonic() - started)

    async def _warm_up_stt(self) -> None:
        from ciel.stt.local_whisper import WhisperSTT

        try:
            await self._stt.warm_up()
            return
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            if isinstance(self._stt, WhisperSTT):
                raise
            log.warning("%s failed to start (%s) — falling back to faster-whisper",
                        type(self._stt).__name__, exc)
        self._stt = WhisperSTT(self._config.stt)
        await self._stt.warm_up()

    async def _warm_up_tts(self) -> None:
        try:
            await self._tts.warm_up()
            return
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            log.warning("%s failed to start (%s) — falling back to `say`",
                        type(self._tts).__name__, exc)
        from ciel.pipeline import _apply_effect
        from ciel.tts.macos_say import SayTTS

        self._tts = _apply_effect(SayTTS(self._config.tts), self._config)
        await self._tts.warm_up()

    async def _shutdown(self) -> None:
        closers = [
            self._stt.close(),
            self._tts.close(),
            self._wake.close(),
            self._indicator.close(),
            self._heartbeat.close(),
            self._executor.close(),
            self._link.close(),
        ]
        if self._speaker is not None:
            closers.append(self._speaker.close())
        if self._watcher is not None:
            closers.append(self._watcher.close())
        closers.extend(w.close() for w in self._watchers)
        await asyncio.gather(*closers, return_exceptions=True)

    def _announce_ready(self) -> None:
        mode = self._config.wake.mode
        if mode == "wakeword":
            phrase = Path(self._config.wake.model).stem.replace("_", " ")
            print(f'\nReady. Say "{phrase}".')
        elif mode == "always":
            print("\nReady. Just talk.")
        print(f"hub: {self._config.spoke.hub}")
        if self._muted:
            print("  [muted — Ciel will stay silent and not listen for its name]")

    # ── the utterance's way up ───────────────────────────────────────────────

    async def _handle_utterance(self, utterance: np.ndarray) -> None:
        """Identity, transcription, the Cauchy hold — then the wire."""
        self._indicator.set_state("thinking")
        self._spoke = False
        try:
            if self._speaker is not None:
                recognized, similarity = await self._speaker.check(utterance)
                if not recognized:
                    reason = (
                        "too short to verify outside a conversation"
                        if math.isnan(similarity)
                        else f"unrecognized voice ({similarity:.2f})"
                    )
                    log.info("%s", reason)
                    print(f"\n  [{reason} — ignored]", flush=True)
                    if self._pending_text is None:
                        return
                    # A stranger spoke during a continuation window; the
                    # user's held thought is still owed an answer.
                    text, self._pending_text = self._pending_text, None
                    self._send_turn(text)
                    return

            heard = await self._stt.transcribe(utterance)
            pending = self._pending_text
            if not heard and not pending:
                log.debug("nothing intelligible in %.2fs of audio", len(utterance) / SAMPLE_RATE)
                return
            if heard and self._config.commands.enabled:
                cmd = match_command(heard)
                if cmd is not None and cmd.kind == "dismiss":
                    # Checked against what was *just said*, before the
                    # merge: "never mind" cancels the held thought. The
                    # hub records the line and stays quiet, as it does.
                    self._pending_text = None
                    self._continue_listening = False
                    print("  [dismissed]")
                    self._send_turn(heard)
                    return
            if heard:
                text = f"{pending} {heard}".strip() if pending else heard
                if self._trails_off(heard):
                    self._pending_text = text
                    self._continue_listening = True
                    print(f"\n  you (…): {text}", flush=True)
                    return
            else:
                text = pending
            self._pending_text = None
            assert text is not None
            self._send_turn(text)
        except Exception:  # noqa: BLE001 - a failed prelude must not kill the loop
            self._pending_text = None
            log.exception("utterance handling failed")
            self._indicator.set_state("error")

    def _trails_off(self, heard: str) -> bool:
        if self._config.audio.filler_extend_ms <= 0:
            return False
        return trails_off(heard, self._config.audio)

    def _send_turn(self, text: str) -> None:
        """One whole utterance to the hub. The console line here is the
        room's record; the hub keeps the transcript."""
        print(f"\n  you: {text}", flush=True)
        self._interrupted = False
        self._spoke = False
        self._prev_kind = None
        if not self._link.connected:
            # Held in the ledger and sent when the hub is back — but the
            # user is here now, and silence would read as deafness.
            self._link.say(text)
            asyncio.create_task(self._hub_lost_notice())
            return
        self._awaiting_hub = True
        self._turn_deadline = time.monotonic() + _HUB_BEGIN_TIMEOUT_S
        self._indicator.set_state("thinking")
        self._link.say(text)

    def _finish_turn(self, mic: MicStream) -> None:
        """The BUSY exit: drain, then the follow-up window or waiting."""
        if self._continue_listening:
            # Mid-thought: straight back to listening, no drain —
            # nothing was played, and the queue holds whatever was said
            # while transcription ran.
            self._continue_listening = False
            self._enter_continuation()
            return
        mic.drain()
        if self._spoke and self._config.audio.followup_ms > 0:
            self._enter_followup()
        else:
            self._enter_waiting()

    # ── the hub's frames ─────────────────────────────────────────────────────

    def _on_connect(self, hello: dict[str, Any]) -> None:
        if self._hub_lost_said:
            print("\n  [hub reachable again]", flush=True)
        self._hub_lost_said = False
        self._reported = None
        if isinstance(hello.get("muted"), bool) and hello["muted"] != self._muted:
            self._set_muted(hello["muted"], from_hub=True)
        # Whatever the last socket swallowed rides again, and the hub gets
        # a fresh reading of the room at once.
        resent = self._publish.resend()
        if resent:
            log.info("re-published %d event(s) after the reconnect", resent)
        self._heartbeat.beat(force=True)

    def _on_disconnect(self) -> None:
        if self._hub_turn is not None or self._awaiting_hub:
            # The turn died with the socket; the loop finishes it.
            log.warning("hub dropped mid-turn")
            self._hub_turn = None
            self._awaiting_hub = False
        self._confirm = None

    def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame["type"]
        if kind == "turn.begin":
            if frame.get("lane") != "voice":
                return
            self._awaiting_hub = False
            self._hub_turn = frame["turn_id"]
            self._turn_deadline = time.monotonic() + self._config.hub.speak_timeout_s
            self._prev_kind = None
            self._state = State.BUSY
            if self._config.brain.ack_phrases:
                self._ack_task = asyncio.create_task(self._ack_filler())
        elif kind == "turn.sentence":
            if frame["turn_id"] != self._hub_turn:
                log.debug("sentence for a turn that is not ours — ignored")
                return
            self._playing = asyncio.create_task(self._play_sentence(
                frame["turn_id"], int(frame.get("n") or 0), frame["kind"], frame["text"]
            ))
        elif kind == "turn.end":
            if frame["turn_id"] == self._hub_turn:
                self._hub_turn = None
                self._confirm = None
                asyncio.create_task(self._ack_done())
        elif kind == "confirm.request":
            asyncio.create_task(self._ask(
                frame["confirm_id"], frame["text"], bool(frame.get("listen"))
            ))
        elif kind == "confirm.cancel":
            if self._confirm is not None and self._confirm[0] == frame["confirm_id"]:
                self._confirm = None
        elif kind == "deliver.speak":
            asyncio.create_task(self._deliver(
                frame["event_id"], frame["text"], frame.get("meta") or {}
            ))
        elif kind == "state":
            state = frame["state"]
            if state == "idle" and self._state is State.LISTENING:
                return  # a follow-up window is open here; the hub can't see it
            self._indicator.set_state(state)
        elif kind == "muted":
            self._set_muted(bool(frame["muted"]), from_hub=True)
        elif kind in ("tool.request", "tool.cancel"):
            self._executor.handle(frame)
        elif kind == "event.ack":
            self._publish.acked(frame["publish_id"])
        # rows, agents, confirm banners, hellos: the Chart's business

    async def _play_sentence(self, turn_id: str, n: int, kind: str, text: str) -> None:
        """One sentence through the speakers, receipted."""
        assert self._player is not None
        completed = True
        try:
            await self._ack_done()
            if (
                self._prev_kind == "thinking"
                and kind == "reply"
                and self._config.brain.thinking_chime
            ):
                await self._chime()
            self._prev_kind = kind
            self._indicator.set_state("reasoning" if kind == "thinking" and
                                      self._config.brain.speak_thinking else "speaking")
            completed = await self._player.play(self._tts.stream(text))
            self._spoke = True
            if not completed:
                if self._player.device_lost:
                    print("  (audio output lost)")
                else:
                    self._interrupted = True
                    print("  (interrupted)")
        except Exception:  # noqa: BLE001 - a lost sentence is receipted as such
            log.debug("sentence playback failed", exc_info=True)
            completed = False
        finally:
            self._link.send({
                "type": "turn.played", "turn_id": turn_id, "n": n,
                "completed": completed,
            })
            self._indicator.set_state("thinking")

    async def _ask(self, confirm_id: str, text: str, listen: bool) -> None:
        """Speak a confirm line; open the answer window when asked to."""
        assert self._player is not None and self._mic is not None
        if self._playing is not None and not self._playing.done():
            with contextlib.suppress(Exception):
                await self._playing
        await self._ack_done()
        self._confirm = None
        try:
            await self._player.play(self._tts.stream(text))
        except Exception:  # noqa: BLE001 - a gate that cannot ask still listens
            log.debug("could not speak the confirmation prompt", exc_info=True)
        if not listen:
            return
        self._mic.drain()
        self._confirm_endpointer.reset()
        self._indicator.set_state("listening")
        self._confirm = (
            confirm_id,
            time.monotonic() + self._config.shell.confirm_timeout_ms / 1000,
        )

    def _feed_confirm(self, frame: bytes) -> None:
        assert self._confirm is not None
        confirm_id, deadline = self._confirm
        utterance = self._confirm_endpointer.push(frame)
        if utterance is not None:
            self._confirm = None
            asyncio.create_task(self._answer(confirm_id, utterance))
        elif time.monotonic() >= deadline and not self._confirm_endpointer.speaking:
            self._confirm = None
            self._indicator.set_state("thinking")
            self._link.send({"type": "confirm.answer", "confirm_id": confirm_id, "text": ""})

    async def _answer(self, confirm_id: str, utterance: np.ndarray) -> None:
        self._indicator.set_state("thinking")
        try:
            heard = (await self._stt.transcribe(utterance)).strip()
        except Exception:  # noqa: BLE001 - an untranscribable answer is no answer
            log.debug("could not transcribe the answer", exc_info=True)
            heard = ""
        self._link.send({"type": "confirm.answer", "confirm_id": confirm_id, "text": heard})

    async def _deliver(self, event_id: str, text: str, meta: dict[str, Any]) -> None:
        """An unprompted line at idle: ring, speak, receipt."""
        assert self._player is not None and self._mic is not None
        self._delivering = True
        ok = True
        try:
            self._indicator.set_state("speaking")
            if meta.get("ring"):
                with contextlib.suppress(Exception):
                    await self._player.play(_one(ring_pcm(self._tts.sample_rate)))
            print(f"\n  ciel ({meta.get('kind') or meta.get('source') or 'nudge'}): {text}", flush=True)
            completed = await self._player.play(self._tts.stream(text))
            # Barge-in mid-nudge still counts as delivered; a dead device
            # does not — the same reading the single process makes.
            ok = completed or not self._player.device_lost
        except Exception:  # noqa: BLE001
            log.exception("delivery failed")
            ok = False
        finally:
            self._delivering = False
            self._mic.drain()
            self._indicator.set_state(
                "listening" if self._state is State.LISTENING else "idle"
            )
            self._link.send({"type": "deliver.result", "event_id": event_id, "ok": ok})

    async def _hub_lost_notice(self) -> None:
        """Once in words, then a chime — the resilience contract."""
        if self._player is None:
            return
        if not self._hub_lost_said:
            self._hub_lost_said = True
            print("  [hub unreachable]", flush=True)
            with contextlib.suppress(Exception):
                await self._player.play(self._tts.stream("I can't reach the hub right now."))
        else:
            await self._chime()

    async def _shrug(self, player: Player) -> None:
        with contextlib.suppress(Exception):
            await player.play(_one(chime_pcm(self._tts.sample_rate)))

    async def _chime(self) -> None:
        if self._player is not None:
            await self._shrug(self._player)

    # ── the ack filler ───────────────────────────────────────────────────────

    async def _ack_filler(self) -> None:
        assert self._player is not None
        await asyncio.sleep(self._config.brain.ack_delay_s)
        self._ack_speaking = True
        await self._player.play(
            self._tts.stream(random.choice(self._config.brain.ack_phrases))
        )

    async def _ack_done(self) -> None:
        if self._ack_task is None:
            return
        if not self._ack_speaking:
            self._ack_task.cancel()
        task, self._ack_task = self._ack_task, None
        self._ack_speaking = False
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:  # noqa: BLE001 — a lost filler costs nothing
            log.debug("ack playback failed", exc_info=True)

    # ── mute, presence of speech, the room ───────────────────────────────────

    def _set_muted(self, muted: bool, *, from_hub: bool = False) -> None:
        if muted == self._muted:
            return
        self._muted = muted
        try:
            if muted:
                self._mute_sentinel.touch()
            else:
                self._mute_sentinel.unlink(missing_ok=True)
        except OSError:
            log.debug("could not persist mute state", exc_info=True)
        if muted and self._player is not None:
            self._player.stop()
        print(f"\n  [{'muted' if muted else 'unmuted'}]", flush=True)
        if not from_hub:
            self._link.send({"type": "mute", "muted": muted})

    def _report_voice_state(self) -> None:
        listening = self._state is State.LISTENING
        speaking = listening and self._endpointer.speaking
        current = (listening, speaking)
        if current != self._reported:
            if self._link.send({"type": "voice.state", "listening": listening,
                                "speaking": speaking}):
                self._reported = current

    @staticmethod
    def _rms(frame: bytes) -> float:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(samples * samples)))

    def _track_noise_floor(self, frame: bytes) -> None:
        level = self._rms(frame)
        if self._noise_floor is None:
            self._noise_floor = level
            return
        alpha = 0.02 if level > self._noise_floor else 0.15
        self._noise_floor = (1 - alpha) * self._noise_floor + alpha * level

    def _check_barge_in(self, frame: bytes, player: Player) -> None:
        if not self._config.audio.barge_in or not player.is_playing:
            self._barge_run = 0
            return
        floor = self._noise_floor if self._noise_floor is not None else 0.0
        threshold = max(
            floor * self._config.audio.barge_in_ratio,
            self._config.audio.barge_in_floor,
        )
        if self._rms(frame) < threshold:
            self._barge_run = 0
            return
        self._barge_run += 1
        if self._barge_run >= self._config.audio.barge_in_frames:
            log.debug("barge-in (floor %.3f, threshold %.3f)", floor, threshold)
            self._barge_run = 0
            player.stop()

    async def _acknowledge(self, player: Player, mic: MicStream) -> None:
        wake = self._config.wake
        if not wake.ack or wake.mode == "always" or not wake.ack_phrases:
            return
        try:
            await player.play(self._tts.stream(random.choice(wake.ack_phrases)))
        except Exception:  # noqa: BLE001 - a lost ack must not cost the turn
            log.debug("could not speak the wake acknowledgement", exc_info=True)
        mic.drain()

    # ── state transitions ────────────────────────────────────────────────────

    def _enter_listening(self) -> None:
        self._state = State.LISTENING
        self._endpointer.reset()
        timeout_ms = self._config.audio.wake_timeout_ms
        self._followup_until = (
            time.monotonic() + timeout_ms / 1000 if timeout_ms > 0 else None
        )
        self._indicator.set_state("listening")
        print("\n  [listening]", flush=True)

    def _enter_continuation(self) -> None:
        self._state = State.LISTENING
        self._endpointer.reset()
        self._followup_until = (
            time.monotonic() + self._config.audio.filler_extend_ms / 1000
        )
        self._indicator.set_state("listening")

    def _enter_followup(self) -> None:
        self._state = State.LISTENING
        self._endpointer.reset()
        self._followup_until = (
            time.monotonic() + self._config.audio.followup_ms / 1000
        )
        self._indicator.set_state("listening")
        print("\n  [listening]", flush=True)

    def _followup_expired(self) -> bool:
        if self._followup_until is None or self._endpointer.speaking:
            return False
        return time.monotonic() >= self._followup_until

    def _enter_waiting(self) -> None:
        self._state = State.WAITING
        self._endpointer.reset()
        self._wake.reset()
        self._followup_until = None
        self._pending_text = None
        self._indicator.set_state("idle")


__all__ = ["Spoke", "chime_pcm", "ring_pcm"]
