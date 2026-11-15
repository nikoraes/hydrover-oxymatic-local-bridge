"""Write path: HA intent -> edition -> document the controller adopts.

Run:  env -u PYTHONPATH python3 tests/test_write_path.py
"""
import json
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
setattr(sys.modules["paho.mqtt.client"], "Client", object)

import bridge

ok = True


def check(what, cond, detail=None):
    global ok
    if cond:
        print(f"  PASS  {what}")
    else:
        ok = False
        print(f"  FAIL  {what}  {detail!r}")


def reset():
    bridge.desired_program = None
    bridge.desired_mode = None
    bridge.pending_edition = 0.0


def main():
    print("write path")

    check("template loaded", bridge.TEMPLATE is not None)

    reset()
    check("no edition pending initially", bridge.pending_edition == 0.0)

    # program by name and by code
    reset()
    check("program name accepted", bridge.apply_program("WINTER") is True)
    check("name maps to code 5", bridge.desired_program == 5, bridge.desired_program)
    check("edition queued", bridge.pending_edition > 0)

    reset()
    check("numeric program accepted", bridge.apply_program("11") is True)
    check("numeric kept as-is", bridge.desired_program == 11, bridge.desired_program)

    reset()
    check("unknown program rejected", bridge.apply_program("NOPE") is False)
    check("rejection queues nothing", bridge.pending_edition == 0.0)

    # mode
    for mode in ("auto", "man", "off"):
        reset()
        check(f"mode {mode} accepted", bridge.apply_mode(mode) is True)
        check(f"mode {mode} stored", bridge.desired_mode == mode)
    reset()
    check("unknown mode rejected", bridge.apply_mode("sleep") is False)

    # document
    reset()
    bridge.readings.update({"temperature": "5.8", "pH": "7.40", "redox": "297.6",
                            "copper": "0.10", "oxy_voltage": "10.63",
                            "oxy_current": "0.12", "is_pump": "false"})
    bridge.apply_program("WINTER")
    bridge.apply_mode("off")
    doc = json.loads(bridge.build_document() or b"{}")
    check("doc is the cloud shape", "Nirvana" not in doc and "IsNirvana" in doc)
    check("doc carries the program", doc["Program"] == 5, doc["Program"])
    check("doc carries mode off", doc["IsOff"] is True and doc["IsAuto"] is False
          and doc["IsMan"] is False, (doc["IsOff"], doc["IsAuto"], doc["IsMan"]))
    check("doc carries live readings", doc["Temp"] == 5.8 and doc["PH"] == 7.4,
          (doc["Temp"], doc["PH"]))
    check("doc replaces LastProgEdition", doc["LastProgEdition"].startswith("/Date(")
          and "978303600000" not in doc["LastProgEdition"], doc["LastProgEdition"])
    check("doc omits every field the template has",
          set(doc) >= {"Program", "LastEdition", "RemoteControl", "Model"})

    # a partial write must inherit the controller's other field
    reset()
    bridge.settings.update({"Program": "5", "IsAuto": "false", "IsMan": "false",
                            "IsOff": "true"})
    bridge.apply_mode("auto")
    doc = json.loads(bridge.build_document() or b"{}")
    check("mode-only keeps the controller's program", doc["Program"] == 5, doc["Program"])
    check("mode-only sets the requested mode",
          doc["IsAuto"] is True and doc["IsOff"] is False,
          (doc["IsAuto"], doc["IsOff"]))

    reset()
    bridge.settings.update({"Program": "5", "IsAuto": "false", "IsMan": "false",
                            "IsOff": "true"})
    bridge.apply_program("WINTER")
    doc = json.loads(bridge.build_document() or b"{}")
    check("program-only keeps the controller's mode",
          doc["IsOff"] is True and doc["IsAuto"] is False,
          (doc["IsAuto"], doc["IsOff"]))
    check("program-only carries the requested program", doc["Program"] == 5)

    # mode man
    reset()
    bridge.apply_mode("man")
    doc = json.loads(bridge.build_document() or b"{}")
    check("man sets IsMan only", doc["IsMan"] is True and doc["IsAuto"] is False
          and doc["IsOff"] is False, (doc["IsAuto"], doc["IsMan"], doc["IsOff"]))

    # edition list
    reset()
    bridge.apply_program("PUBLIC POOL")
    ed = json.loads(bridge.build_edition_list())
    check("edition starts with DataEdition",
          ed[0][0] == "DataEdition" and int(ed[0][1]) > 1_700_000_000, ed[0])
    check("edition keeps the per-setting stamps",
          ["11", "1668508791"] in ed and ["Aux1", "1673101024"] in ed)
    check("DataEdition matches the queue",
          int(ed[0][1]) == int(bridge.pending_edition), (ed[0], bridge.pending_edition))

    # command topics are what HA will publish to
    check("program command topic", bridge.COMMAND_TOPICS["program"] == "oxymatic/set/program")
    check("mode command topic", bridge.COMMAND_TOPICS["mode"] == "oxymatic/set/mode")
    check("program options cover the known table", len(bridge.PROGRAM_CODES) == 11,
          len(bridge.PROGRAM_CODES))

    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
