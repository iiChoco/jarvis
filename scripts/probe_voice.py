#!/usr/bin/env python
"""Dev probe for the voice I/O path, with no model in the loop.

    uv run scripts/probe_voice.py speak   # TTS + player only
    uv run scripts/probe_voice.py echo    # full loop: mic -> STT -> TTS
    uv run scripts/probe_voice.py barge   # verify playback stops on demand

``speak`` isolates output, ``echo`` proves capture and transcription feed it
correctly, and ``barge`` checks the interrupt path that the real pipeline
depends on. If the assembled assistant misbehaves later, these say which half
to blame.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from ciel.audio.input import MicStream
from ciel.audio.output import Player
from ciel.audio.vad import Endpointer
from ciel.config import SAMPLE_RATE, load_config
from ciel.stt.local_whisper import WhisperSTT
from ciel.tts.macos_say import SayTTS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def probe_speak() -> int:
    cfg = load_config()
    tts = SayTTS(cfg.tts)
    await tts.warm_up()

    async with Player(cfg.audio, tts.sample_rate) as player:
        for line in [
            "Ciel here. Voice output is working.",
            "This is a second sentence, spoken separately.",
        ]:
            print(f"  speaking: {line!r}")
            started = time.monotonic()
            ok = await player.play(tts.stream(line))
            print(f"    {'completed' if ok else 'interrupted'} in {time.monotonic() - started:.2f}s")

    await tts.close()
    print("\nPASS: if you heard two sentences, output works.")
    return 0


async def probe_barge() -> int:
    """Start a long sentence and stop it mid-flow."""
    cfg = load_config()
    tts = SayTTS(cfg.tts)
    await tts.warm_up()

    long_line = (
        "This is a deliberately long sentence that should be cut off partway "
        "through, well before it reaches the end, to prove that barge in works "
        "and that playback halts promptly rather than finishing the utterance."
    )

    async with Player(cfg.audio, tts.sample_rate) as player:
        print("  speaking a long sentence; interrupting after 1.5s...")
        started = time.monotonic()
        task = asyncio.create_task(player.play(tts.stream(long_line)))
        await asyncio.sleep(1.5)
        player.stop()
        completed = await task
        elapsed = time.monotonic() - started

    await tts.close()
    print(f"    stopped after {elapsed:.2f}s, completed={completed}")

    if completed:
        print("\nFAIL: playback ran to completion — stop() had no effect.")
        return 1
    if elapsed > 2.5:
        print(f"\nFAIL: took {elapsed:.2f}s to stop; expected well under 2s.")
        return 1
    print("\nPASS: playback interrupted promptly.")
    return 0


async def probe_echo() -> int:
    cfg = load_config()
    stt = WhisperSTT(cfg.stt)
    tts = SayTTS(cfg.tts)
    await asyncio.gather(stt.warm_up(), tts.warm_up())

    endpointer = Endpointer(cfg.audio)
    turns = 0

    async with Player(cfg.audio, tts.sample_rate) as player:
        await player.play(tts.stream("Echo mode. Say something and I will repeat it."))

        print("\nSpeak, then pause. Two turns, then it exits. Ctrl-C to quit early.\n")
        async with MicStream(cfg.audio) as mic:
            async for frame in mic.frames():
                utterance = endpointer.push(frame)
                if utterance is None:
                    continue

                heard = await stt.transcribe(utterance)
                if not heard:
                    print("  (nothing intelligible)")
                    continue

                print(f"  heard: {heard!r}")
                await player.play(tts.stream(f"You said: {heard}"))
                # Discard audio captured while the speakers were live,
                # otherwise Ciel transcribes its own voice as the next turn.
                mic.drain()
                endpointer.reset()

                turns += 1
                if turns >= 2:
                    break

    await asyncio.gather(stt.close(), tts.close())
    print("\nPASS: full capture -> transcribe -> speak loop works." if turns else
          "\nFAIL: no turns captured.")
    return 0 if turns else 1


_MODES = {"speak": probe_speak, "echo": probe_echo, "barge": probe_barge}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "speak"
    if mode not in _MODES:
        print(f"unknown mode {mode!r}; expected one of {', '.join(_MODES)}")
        raise SystemExit(2)
    try:
        raise SystemExit(asyncio.run(_MODES[mode]()))
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
