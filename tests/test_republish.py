#!/usr/bin/env python3
"""Regression test: mode/program must be republished at least every REFRESH_SECONDS.

Run:  python3 tests/test_republish.py
(no pytest needed — the bridge is stdlib + paho, and this keeps it that way)
"""
import importlib.util
import pathlib
import sys
import types

# The bridge imports paho at module level but only uses it for the real client;
# stub it so the publish logic can be tested without the MQTT dependency.
if "paho.mqtt.client" not in sys.modules:
    for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
        sys.modules.setdefault(name, types.ModuleType(name))
    class _Client:  # minimal surface used at import time
        def __init__(self, *a, **kw):
            pass
        def connect(self, *a, **kw):
            pass
        def loop_start(self):
            pass
    sys.modules["paho.mqtt.client"].Client = _Client

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge", ROOT / "bridge.py")
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))

    def states(self):
        return [(t, p) for t, p, _ in self.sent if t.endswith("/mode/state") or t.endswith("/prog/state")]


def reset(refresh_seconds=300):
    bridge.last_published.clear()
    bridge.settings.clear()
    bridge.settings.update({
        "IsAuto": "true", "IsMan": "false", "IsOff": "false",
        "Program": "11", "Model": "SMARTPlus80 PH CU", "Version": "1.0.4c",
        "RemoteControl": "true",
    })
    bridge.REFRESH_SECONDS = refresh_seconds
    bridge._settings_last_force = 0.0
    return FakeClient()


def fake_clock(monkeypatch_to):
    bridge.time = type("T", (), {"monotonic": staticmethod(lambda: monkeypatch_to[0]), "time": __import__("time").time})


def main():
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print(("PASS " if cond else "FAIL ") + label + (" | " + extra if extra else ""))

    # 1. a forced publish writes mode + program (states and attributes)
    c = reset()
    bridge.publish_settings_state(c, force=True)
    check("forced publish writes both states", len(c.states()) == 2, str(c.states()))

    # 2. an immediately following non-forced publish is suppressed
    c2 = reset()
    bridge.publish_settings_state(c2, force=True)
    n = len(c2.sent)
    bridge.publish_settings_state(c2, force=False)
    check("unchanged values are deduplicated", len(c2.sent) == n)

    # 3. ... but only until REFRESH_SECONDS elapses
    c3 = reset(refresh_seconds=300)
    clock = [1000.0]
    real_time = bridge.time
    bridge.time = type("T", (), {"monotonic": staticmethod(lambda: clock[0]), "time": real_time.time})
    try:
        bridge.publish_settings_state(c3, force=True)      # t=1000, forced
        n = len(c3.sent)
        clock[0] = 1299.0
        bridge.publish_settings_state(c3, force=False)     # 299 s later -> silent
        check("no refresh before the interval", len(c3.sent) == n)
        clock[0] = 1300.0
        bridge.publish_settings_state(c3, force=False)     # 300 s -> refresh
        check("refresh after the interval", len(c3.sent) > n, f"{n} -> {len(c3.sent)}")
    finally:
        bridge.time = real_time

    # 4. a real change still publishes immediately, regardless of the interval
    c4 = reset(refresh_seconds=300)
    bridge.publish_settings_state(c4, force=True)
    n = len(c4.sent)
    bridge.settings["Program"] = "5"
    bridge.publish_settings_state(c4, force=False)
    check("a changed program publishes at once", len(c4.sent) > n)

    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
