"""One live interview: the interviewer's turns, the candidate's answers, and
the room's judgement about when one ends and the other begins.

A session is a single asyncio task with an inbox. The socket handler
drops validated frames into the inbox; the task owns every decision —
which is what makes the cut-off rule expressible at all. The shape:

    begin ─► THINKING ─► SPEAKING ─► LISTENING ─► (answer complete) ─► THINKING …
                 ▲                        │
                 └── cut off ◄────────────┘   the candidate spoke while the
                                              interviewer was thinking or
                                              talking: stop, apologize, merge

*Speaking* here means the browser is still playing the turn's audio; the
browser says ``played`` when its queue drains, and only then does the
silence clock start — otherwise the interviewer's own sentence would be
counted as the candidate's pause. A candidate who starts talking over the
playback simply starts their answer; no apology is owed for that, the
question was asked. An apology is owed when the *room* misjudged: it took
a pause for an ending, sent the answer up, and the candidate went on.
Then the interviewer's reply is cancelled, "Sorry, go on" is said, and
the continuation is merged onto the answer so the model hears it whole.

The interviewer never touches the page directly. Two directives on lines
of their own, ``[[exhibit: e1]]`` and ``[[problem: p1]]``, are lifted out
of the speech stream before the sentence splitter sees them and become
frames; the text around them is spoken as usual.

A session survives its socket: a page reload reconnects with a hello and
gets the history back. It does not survive silence — with no frame for
``idle_close_s`` it ends itself and writes the debrief, so a closed laptop
cannot hold a model subprocess open all night.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

from ciel.brain.sentences import split_sentences
from ciel.config import InterviewConfig
from ciel.interview import prompt as prompts
from ciel.interview import wire
from ciel.interview.brain import Backend, BackendError
from ciel.interview.endpoint import Endpointer
from ciel.interview.store import SessionStore

log = logging.getLogger(__name__)

_FLUSH_CHARS = 180
_PLAYED_GRACE_S = 8.0
"""Added to the turn's audio length: how long the room waits for the
browser's ``played`` before it arms the silence clock anyway — a tab that
never reports must not freeze the interview at 'speaking'."""
_HISTORY = 600


class InterviewSession:
    def __init__(
        self,
        *,
        cfg: InterviewConfig,
        store: SessionStore,
        username: str,
        session_id: str,
        meta: dict[str, Any],
        brief: dict[str, Any],
        backend: Backend,
        debrief_backend: Callable[[], Backend],
        speaker: Any = None,
        on_finished: Callable[["InterviewSession"], Awaitable[None] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self.username = username
        self.session_id = session_id
        self._meta = meta
        self._brief = brief
        self._mode = str(meta.get("mode") or "company")
        self._backend = backend
        self._debrief_backend = debrief_backend
        self._speaker = speaker
        self._on_finished = on_finished
        self._clock = clock

        self.state = "idle"
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._ended = False
        self.finished = asyncio.Event()

        self._endpoint = Endpointer(
            silence_s=cfg.silence_ms / 1000.0,
            extend_s=cfg.extend_ms / 1000.0,
            max_answer_s=cfg.max_answer_s,
        )
        self._turn = 0
        self._say_n = 0
        self._turn_audio_s = 0.0
        """Seconds of speech sent this turn, for the played deadline."""
        self._history: deque[dict[str, Any]] = deque(maxlen=_HISTORY)
        self._started = clock()
        self._last_activity = clock()
        self._played_deadline: float | None = None
        self._answer_completed_at: float | None = None
        self._last_answer = ""
        """The candidate text the current turn answers — the merge base."""
        self._cutoff_pending = False
        self._current_is_greeting = False
        self._latest_code: tuple[str, str] | None = None
        self._code_sent: tuple[str, str] | None = None
        self._client_tts = "browser"

        self._exhibits = {
            str(e.get("id")): e for e in brief.get("exhibits") or [] if isinstance(e, dict)
        }
        tech = brief.get("technical") if isinstance(brief.get("technical"), dict) else {}
        self._problems = {
            str(p.get("id")): p for p in tech.get("problems") or [] if isinstance(p, dict)
        }

    # ── public surface (the socket handler's side) ───────────────────────────

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    @property
    def live(self) -> bool:
        return self._task is not None and not self._ended

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"interview:{self.session_id}")

    async def attach(self, ws: Any, caps: dict[str, Any]) -> None:
        """A browser arrived (or came back): make it the one we talk to and
        give it the picture so far."""
        self._ws = ws
        self._client_tts = str(caps.get("tts") or "browser")
        self._last_activity = self._clock()
        await self._send({
            "type": "hello",
            "v": wire.WIRE_VERSION,
            "state": self.state,
            "tts": self.tts_mode,
            "turn": self._turn,
            "elapsed_s": round(self.elapsed, 1),
            "history": list(self._history),
        })
        self.start()

    def detach(self, ws: Any) -> None:
        if ws is self._ws:
            self._ws = None

    def on_frame(self, frame: dict[str, Any]) -> None:
        self._last_activity = self._clock()
        self._inbox.put_nowait(frame)

    def on_audio(self, seq: int, chunk: bytes) -> None:
        self._last_activity = self._clock()
        try:
            self._store.append_recording(self.username, self.session_id, chunk)
        except OSError:
            log.warning("could not append recording chunk %d for %s", seq, self.session_id, exc_info=True)

    async def pause(self, reason: str) -> None:
        """Tell the page the room is stepping away (a reload), then end."""
        await self._send({"type": "state", "state": "paused"})
        await self._finish(reason)

    async def close(self) -> None:
        if not self._ended:
            await self._finish("closed")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    @property
    def tts_mode(self) -> str:
        if self._speaker is not None and getattr(self._speaker, "available", False) and self._client_tts != "none":
            return "piper"
        return "browser"

    # ── the task ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            length = int((self._meta.get("setup") or {}).get("length_min") or 30)
            await self._backend.start(prompts.interviewer_prompt(self._mode, self._brief, length))
            self._meta = self._store.update_meta(
                self.username, self.session_id, state="live",
                started=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            )
            await self._turn_cycle(prompts.BEGIN, greeting=True)
            while not self._ended:
                frame = await self._next_frame()
                if frame is None:
                    await self._on_tick()
                else:
                    await self._handle(frame)
        except asyncio.CancelledError:
            raise
        except BackendError as exc:
            log.warning("interview %s: %s", self.session_id, exc)
            await self._send({"type": "error", "code": "interviewer", "reason": str(exc)})
            await self._finish("error")
        except Exception:  # noqa: BLE001 - one interview's failure stays its own
            log.exception("interview %s crashed", self.session_id)
            await self._send({"type": "error", "code": "crash", "reason": "the room hit an error"})
            await self._finish("error")

    async def _next_frame(self) -> dict[str, Any] | None:
        """The next inbound frame, or None when a clock fired first."""
        now = self._clock()
        deadlines = [self._last_activity + self._cfg.idle_close_s]
        if self.state == "speaking" and self._played_deadline is not None:
            deadlines.append(self._played_deadline)
        if self.state == "listening":
            due = self._endpoint.deadline()
            if due is not None:
                deadlines.append(due)
        timeout = max(0.0, min(deadlines) - now)
        try:
            return await asyncio.wait_for(self._inbox.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def _on_tick(self) -> None:
        now = self._clock()
        if now >= self._last_activity + self._cfg.idle_close_s:
            log.info("interview %s idle for %.0fs — ending", self.session_id, self._cfg.idle_close_s)
            await self._record("event", "the room ended the interview after a long silence")
            await self._finish("idle")
            return
        if self.state == "speaking" and self._played_deadline is not None and now >= self._played_deadline:
            self._arm_listening()
            return
        if self.state == "listening":
            decision = self._endpoint.decide(now)
            if decision == "extend":
                await self._send({"type": "state", "state": "listening", "hold_ms": self._cfg.extend_ms})
            elif decision == "complete":
                await self._answer_complete()

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame["type"]
        now = self._clock()
        if kind == "ping":
            await self._send({"type": "pong"})
        elif kind in ("speech.partial", "speech.final", "barge"):
            if self.state == "speaking":
                # Talking over the playback: the answer has begun.
                self._arm_listening()
            if self.state != "listening":
                return
            if kind == "speech.partial":
                self._endpoint.feed_partial(frame["text"], now)
            elif kind == "speech.final":
                self._endpoint.feed_final(frame["text"], now)
        elif kind == "answer.done":
            if self.state == "speaking":
                self._arm_listening()
            if self.state == "listening" and self._endpoint.text:
                self._endpoint.mark_done()
                await self._answer_complete()
        elif kind == "typed":
            if self.state == "speaking":
                self._arm_listening()
            if self.state == "listening" and frame["text"].strip():
                self._endpoint.seed(frame["text"], now)
                self._endpoint.mark_done()
                await self._answer_complete()
        elif kind == "played":
            if self.state == "speaking" and frame["turn"] == self._turn:
                self._arm_listening()
        elif kind == "code.snapshot":
            self._latest_code = (frame["lang"], frame["code"])
            self._store.append_code(self.username, self.session_id, self.elapsed, frame["lang"], frame["code"], "snapshot")
        elif kind == "code.submit":
            self._latest_code = (frame["lang"], frame["code"])
            self._code_sent = self._latest_code
            self._store.append_code(self.username, self.session_id, self.elapsed, frame["lang"], frame["code"], "submit")
            await self._record("event", f"code submitted ({frame['lang']}, {len(frame['code'])} chars)")
            if self.state in ("listening", "speaking"):
                # Whatever they were saying rides along with the code.
                spoken = self._endpoint.text
                text = prompts.code_message(frame["lang"], frame["code"], submitted=True)
                if spoken:
                    await self._record("candidate", spoken)
                    text = f"{text}\n\nThe candidate also said: {spoken}"
                await self._turn_cycle(text)
        elif kind == "end":
            await self._record("event", "the candidate ended the interview")
            await self._turn_cycle(prompts.END, closing=True)
            await self._finish("ended")

    # ── answers and turns ────────────────────────────────────────────────────

    def _arm_listening(self) -> None:
        self.state = "listening"
        self._played_deadline = None
        self._endpoint.reset()
        self._queue_state("listening", hold_ms=self._cfg.silence_ms)

    def _queue_state(self, state: str, **extra: Any) -> None:
        # Fire-and-forget from synchronous spots; the send lock keeps order.
        asyncio.create_task(self._send({"type": "state", "state": state, **extra}))

    async def _answer_complete(self) -> None:
        answer = self._endpoint.text
        self._answer_completed_at = self._clock()
        if not answer:
            self._endpoint.reset()
            return
        await self._record("candidate", answer)
        text = answer
        if self._cutoff_pending:
            text = f"{prompts.CUTOFF_MARKER}\n\n{answer}"
            self._cutoff_pending = False
        if self._latest_code is not None and self._latest_code != self._code_sent and self._mode == "technical":
            lang, code = self._latest_code
            self._code_sent = self._latest_code
            text = f"{prompts.code_message(lang, code, submitted=False)}\n\nThe candidate says: {text}"
        self._last_answer = answer
        await self._turn_cycle(text)

    async def _turn_cycle(self, user_text: str, *, greeting: bool = False, closing: bool = False) -> None:
        """One interviewer turn, cancellable by the candidate's voice."""
        self._current_is_greeting = greeting
        self.state = "thinking"
        await self._send({"type": "state", "state": "thinking"})
        self._turn += 1
        self._say_n = 0
        self._turn_audio_s = 0.0
        turn = self._turn
        task = asyncio.create_task(self._speak_turn(turn, user_text))
        self._turn_task = task
        try:
            while not task.done():
                getter = asyncio.create_task(self._inbox.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    frame = getter.result()
                    if await self._during_turn(frame, task, greeting=greeting, closing=closing):
                        return
                else:
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                raise exc
        finally:
            self._turn_task = None
        if self._ended:
            return
        await self._send({"type": "turn.end", "turn": turn})
        if closing:
            return
        if self._say_n:
            # The page reports played when its audio (or its own voice)
            # finishes; the deadline is the speech length plus a grace, so a
            # tab that never reports costs seconds, not minutes.
            self.state = "speaking"
            self._played_deadline = self._clock() + self._turn_audio_s + _PLAYED_GRACE_S
            await self._send({"type": "state", "state": "speaking"})
        else:
            self._arm_listening()

    async def _during_turn(
        self, frame: dict[str, Any], task: asyncio.Task[None], *, greeting: bool, closing: bool
    ) -> bool:
        """Handle a frame that arrived while the interviewer was talking.
        Returns True when the turn was abandoned (the caller stops)."""
        kind = frame["type"]
        now = self._clock()
        if kind in ("speech.partial", "speech.final", "barge"):
            text = str(frame.get("text") or "").strip()
            if kind != "barge" and not text:
                return False
            # The candidate is talking while the interviewer thinks or
            # speaks: stop the interviewer.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self._backend.interrupt()
            if closing:
                return False
            grace = self._cfg.barge_grace_ms / 1000.0
            late_tail = (
                self._answer_completed_at is not None and now - self._answer_completed_at <= grace
            )
            if greeting:
                # They talked over the welcome: no fault, just listen.
                self.state = "listening"
                self._endpoint.seed(text, now)
                await self._send({"type": "state", "state": "listening", "hold_ms": self._cfg.silence_ms})
                return True
            merged = f"{self._last_answer} {text}".strip()
            self._cutoff_pending = True
            self.state = "listening"
            self._endpoint.seed(merged, now)
            if not late_tail:
                apology = "Sorry, go on."
                await self._send({"type": "apology", "text": apology})
                await self._record("event", "the interviewer cut in and apologized")
            await self._send({"type": "state", "state": "listening", "hold_ms": self._cfg.silence_ms})
            return True
        if kind == "end":
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self._backend.interrupt()
            await self._record("event", "the candidate ended the interview")
            await self._finish("ended")
            return True
        if kind == "code.snapshot":
            self._latest_code = (frame["lang"], frame["code"])
            self._store.append_code(self.username, self.session_id, self.elapsed, frame["lang"], frame["code"], "snapshot")
        elif kind == "code.submit":
            # Hold it: handled once the interviewer finishes this turn.
            self._inbox.put_nowait(frame)
            await asyncio.sleep(0.2)
        elif kind == "ping":
            await self._send({"type": "pong"})
        return False

    async def _speak_turn(self, turn: int, user_text: str) -> None:
        buffer = ""
        async for delta in self._backend.ask(user_text):
            buffer += delta
            buffer = await self._lift_directives(buffer)
            hold = ""
            idx = buffer.rfind("[[")
            if idx != -1 and "]]" not in buffer[idx:]:
                hold, buffer = buffer[idx:], buffer[:idx]
            sentences, buffer = split_sentences(buffer, _FLUSH_CHARS)
            buffer += hold
            for sentence in sentences:
                await self._say(turn, sentence)
        buffer = await self._lift_directives(buffer)
        tail = buffer.strip()
        if tail:
            await self._say(turn, tail)

    async def _lift_directives(self, buffer: str) -> str:
        """Emit every complete directive in the buffer and cut it out."""
        while True:
            match = prompts.DIRECTIVE.search(buffer)
            if match is None:
                return buffer
            kind, ident = match.group(1), match.group(2)
            buffer = buffer[:match.start()] + buffer[match.end():]
            if kind == "exhibit":
                exhibit = self._exhibits.get(ident)
                if exhibit is None:
                    log.info("interview %s: unknown exhibit %r", self.session_id, ident)
                    continue
                frame = {
                    "type": "exhibit", "id": ident, "title": str(exhibit.get("title", "")),
                    "kind": str(exhibit.get("kind", "table")),
                    "columns": list(exhibit.get("columns") or []),
                    "rows": [list(r) for r in exhibit.get("rows") or []],
                    "note": str(exhibit.get("note", "")),
                }
                await self._send(frame, remember=True)
                await self._record("event", f"exhibit shared: {frame['title']}")
            else:
                problem = self._problems.get(ident)
                if problem is None:
                    log.info("interview %s: unknown problem %r", self.session_id, ident)
                    continue
                frame = {
                    "type": "problem", "id": ident, "title": str(problem.get("title", "")),
                    "statement": str(problem.get("statement", "")),
                    "examples": list(problem.get("examples") or []),
                    "constraints": list(problem.get("constraints") or []),
                    "starter": dict(problem.get("starter") or {}),
                    "difficulty": str(problem.get("difficulty", "")),
                    "topic": str(problem.get("topic", "")),
                }
                await self._send(frame, remember=True)
                await self._record("event", f"problem posed: {frame['title']}")

    async def _say(self, turn: int, sentence: str) -> None:
        self._say_n += 1
        if self.state != "speaking":
            self.state = "speaking"
            await self._send({"type": "state", "state": "speaking"})
        frame: dict[str, Any] = {"type": "say", "n": self._say_n, "turn": turn, "text": sentence}
        wav = None
        if self.tts_mode == "piper":
            wav = await self._speaker.wav(sentence)
            if wav:
                frame["audio"] = base64.b64encode(wav).decode("ascii")
        # 16-bit mono at the voice's rate; the browser voice is guessed by length.
        rate = getattr(self._speaker, "sample_rate", 22050) or 22050
        self._turn_audio_s += (len(wav) - 44) / (2 * rate) if wav else 0.06 * len(sentence)
        await self._send(frame, remember=True, remember_without=("audio",))
        await self._record("interviewer", sentence)

    # ── record and send ──────────────────────────────────────────────────────

    async def _record(self, speaker: str, text: str) -> None:
        t = self.elapsed
        try:
            self._store.append_transcript(self.username, self.session_id, t, speaker, text)
        except OSError:
            log.warning("could not write transcript for %s", self.session_id, exc_info=True)
        await self._send({"type": "transcript", "t": round(t, 2), "speaker": speaker, "text": text}, remember=True)

    async def _send(
        self, frame: dict[str, Any], *, remember: bool = False, remember_without: tuple[str, ...] = ()
    ) -> None:
        try:
            data = wire.encode(frame, "h2c")
        except wire.WireError:
            log.exception("interview %s: refusing to send a malformed frame", self.session_id)
            return
        if remember:
            kept = {k: v for k, v in frame.items() if k not in remember_without}
            self._history.append(kept)
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            try:
                await ws.send_str(data)
            except Exception:  # noqa: BLE001 - a dead socket loses this frame; the history keeps it
                log.debug("interview %s: send failed", self.session_id, exc_info=True)
                if ws is self._ws:
                    self._ws = None

    # ── the end ──────────────────────────────────────────────────────────────

    async def _finish(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        self.state = "ended"
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn_task
        with contextlib.suppress(Exception):
            await self._backend.close()
        duration = round(self.elapsed)
        self._meta = self._store.update_meta(
            self.username, self.session_id, state="ended",
            ended=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            duration_s=duration, question_count=max(0, self._turn - 1),
            cost_usd=round(self._backend.cost_usd, 4), tts=self.tts_mode, end_reason=reason,
        )
        await self._send({"type": "state", "state": "ended"})
        log.info("interview %s ended (%s) after %ds, %d turns", self.session_id, reason, duration, self._turn)
        if reason != "closed":
            await self._write_debrief()
        self.finished.set()
        if self._on_finished is not None:
            result = self._on_finished(self)
            if asyncio.iscoroutine(result):
                await result

    async def _write_debrief(self) -> None:
        transcript = self._store.transcript(self.username, self.session_id)
        if not any(r.get("speaker") == "candidate" for r in transcript):
            log.info("interview %s: nothing from the candidate, no debrief", self.session_id)
            # Still tell the page the wait is over: the review explains.
            await self._send({"type": "debrief.ready", "session_id": self.session_id})
            return
        system, user, schema = prompts.debrief_request(
            self._mode, self._brief, transcript, self._store.code(self.username, self.session_id)
        )
        backend = self._debrief_backend()
        try:
            debrief = await backend.ask_json(system, user, schema, effort=self._cfg.debrief_effort)
        except BackendError as exc:
            log.warning("interview %s: debrief failed: %s", self.session_id, exc)
            await self._send({"type": "error", "code": "debrief", "reason": "the debrief could not be written"})
            return
        finally:
            with contextlib.suppress(Exception):
                await backend.close()
        self._store.write_debrief(self.username, self.session_id, debrief, prompts.render_debrief(debrief, self._mode))
        self._meta = self._store.update_meta(
            self.username, self.session_id, state="debriefed",
            cost_usd=round(self._meta.get("cost_usd", 0.0) + backend.cost_usd, 4),
        )
        await self._send({"type": "debrief.ready", "session_id": self.session_id})


__all__ = ["InterviewSession"]
