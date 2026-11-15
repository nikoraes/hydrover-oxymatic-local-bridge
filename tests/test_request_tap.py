"""Request tap: every call the controller makes must be recorded verbatim,
including endpoints the bridge does not implement (a token refresh after a
power cycle, for instance).

Run:  env -u PYTHONPATH python3 tests/test_request_tap.py
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# paho is not needed for these tests but bridge.py imports it at module level
for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
setattr(sys.modules["paho.mqtt.client"], "Client", object)

import bridge

ok = True


def check(what, cond, detail=""):
    global ok
    if cond:
        print(f"  PASS  {what}")
    else:
        ok = False
        print(f"  FAIL  {what}  {detail}")


class BadHeaders:
    def items(self):
        raise RuntimeError("boom")


def main():
    print("request tap")

    rec = bridge.tap_entry(
        "GET",
        "/rest/RefreshToken?token=abc&guid=def",
        "HTTP/1.0",
        {"Host": "oxymaticapp.hydrover.eu"},
        b"",
        "192.168.1.50",
        "TLSv1.2",
        "AES256",
    )
    check("method kept", rec["method"] == "GET")
    check("unknown path kept verbatim", rec["path"] == "/rest/RefreshToken?token=abc&guid=def",
          rec["path"])
    check("headers kept", rec["headers"] == {"Host": "oxymaticapp.hydrover.eu"}, rec["headers"])
    check("client kept", rec["client"] == "192.168.1.50")
    check("tls kept", rec["tls"] == "TLSv1.2" and rec["cipher"] == "AES256")
    check("empty body", rec["body"] == "")

    post = bridge.tap_entry("POST", "/rest/Token", "HTTP/1.1", {}, b"grant=new", "10.0.0.1")
    check("post body kept", post["body"] == "grant=new", post["body"])

    broken = bridge.tap_entry("GET", "/x", "HTTP/1.0", BadHeaders(), b"", "h")
    check("unreadable headers do not raise", broken["headers"] == {}, broken["headers"])

    tmp = tempfile.mkdtemp()
    log = os.path.join(tmp, "requests.jsonl")
    bridge.write_tap(bridge.tap_entry("GET", "/a?b=1", "HTTP/1.0", {}, b"", "c"), log)
    bridge.write_tap(bridge.tap_entry("GET", "/a?b=2", "HTTP/1.0", {}, b"", "c"), log)
    with open(log, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    check("one line per request", len(lines) == 2, str(len(lines)))
    check("first line is the first request", json.loads(lines[0])["path"] == "/a?b=1")
    check("second line is the second request", json.loads(lines[1])["path"] == "/a?b=2")

    # 401 experiment switch: off unless explicitly configured, path-matched only
    check("force_401 off by default", bridge.force_401_endpoint("/rest/GetLastEdition", []) is False)
    check("force_401 matches the configured path",
          bridge.force_401_endpoint("/rest/GetLastEdition?token=x", ["/rest/GetLastEdition"]) is True)
    check("force_401 ignores other paths",
          bridge.force_401_endpoint("/rest/SetDeviceData?token=x", ["/rest/GetLastEdition"]) is False)

    # a request must never fail because the log could not be written
    bridge.write_tap(bridge.tap_entry("GET", "/a", "HTTP/1.0", {}, b"", "c"),
                     "/nonexistent-dir-xyz/requests.jsonl")
    check("unwritable log does not raise", True)

    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
