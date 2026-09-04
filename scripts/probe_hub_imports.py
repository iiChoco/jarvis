"""Probe the hub's imports — would it start on a machine with no Mac in it?

    uv run scripts/probe_hub_imports.py

The hub is meant for a Linux server: no sound device, no VAD library,
no Metal, no pyobjc, no EventKit. This runs a child interpreter with an
import hook that refuses every one of those modules by name — the
audio stack, the Mac frameworks, the speech models — and then imports
the hub's modules and constructs ``Pipeline(config, role="hub")`` with a
throwaway state directory. Construction is the whole audit: it builds
the tool registry, the brain (unconnected), the broker, the server, and
Vigil's queue and policy, which is everything the hub touches before
it opens a socket. A module-level import of anything Mac-shaped fails
here before it fails on the server.

The spoke is expected to fail the same test, and is checked to.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCKED = [
    "sounddevice", "webrtcvad", "_sounddevice", "mlx", "mlx_whisper",
    "faster_whisper", "openwakeword", "onnxruntime", "sherpa_onnx", "piper",
    "objc", "AppKit", "Foundation", "Quartz", "EventKit", "Contacts",
    "CoreLocation", "Cocoa",
]

CHILD = r'''
import importlib.abc, sys, tempfile
BLOCKED = set(%r)

class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{name} is not installed on this machine (probe)")
        return None

sys.modules.pop("numpy", None)
sys.meta_path.insert(0, Refuse())
sys.path.insert(0, %r)

what = sys.argv[1]
if what == "hub":
    import ciel.wire, ciel.schedule, ciel.turn, ciel.confirm
    import ciel.hub.server, ciel.hub.rpc
    import ciel.pipeline
    from dataclasses import replace
    from pathlib import Path
    from ciel.config import Config, ProactiveConfig, ShellConfig, FilesConfig
    from ciel.pipeline import Pipeline
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(
        Config(), state_dir=tmp,
        proactive=replace(ProactiveConfig(), enabled=True, state_file=tmp / "p.json",
                          watches_file=tmp / "w.json", brief_time="08:30"),
        shell=replace(ShellConfig(), enabled=True),
        files=replace(FilesConfig(), enabled=True, workspace=tmp / "ws"),
    )
    p = Pipeline(cfg, role="hub")
    assert p._stt is None and p._tts is None and p._wake is None
    assert p._remote is not None and p._presence is p._remote.presence
    names = [t.name for t in __import__("ciel.brain.tools", fromlist=["TOOLS"]).TOOLS]
    print("HUB OK", len(names), "tools registered in the catalog")
else:
    try:
        import ciel.spoke.frontend
    except ImportError as exc:
        print("SPOKE REFUSED", exc)
        raise SystemExit(0)
    print("SPOKE IMPORTED (unexpected)")
    raise SystemExit(1)
''' % (BLOCKED, str(ROOT / "src"))


def run(what: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, what], capture_output=True, text=True, timeout=120,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def main() -> None:
    print("the hub, with every Mac and audio module refused")
    code, out = run("hub")
    tail = out.strip().splitlines()[-12:]
    for line in tail:
        print("   ", line[:160])
    ok = code == 0 and "HUB OK" in out
    print(f"  {'ok  ' if ok else 'FAIL'} the hub imports and constructs with no Mac in it")
    if not ok:
        sys.exit(1)
    print("\nthe spoke, same test")
    code, out = run("spoke")
    ok = code == 0 and "SPOKE REFUSED" in out
    print("   ", out.strip().splitlines()[-1][:160] if out.strip() else "(no output)")
    print(f"  {'ok  ' if ok else 'FAIL'} the spoke needs the room's modules, as it should")
    if not ok:
        sys.exit(1)
    print("\nall 2 checks passed")


if __name__ == "__main__":
    main()
