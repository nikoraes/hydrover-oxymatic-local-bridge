#!/usr/bin/env python3
"""
Hydrover Oxymatic Local Bridge
-----------------------------
A local drop-in emulator and MQTT bridge for Hydrover Oxymatic pool controllers.

Intercepts outbound HTTPS/TLS telemetry sent by the Oxymatic controller's ESP
Wi-Fi module (via DNS rewrite or static router deflection), publishes it to
Mosquitto MQTT using Home Assistant MQTT Discovery standards, and publishes the
controller's own settings so Home Assistant always shows the truth.

Eliminates all cloud dependencies (oxymaticapp.hydrover.eu / 20.61.77.136).

Uplink protocol (observed from the ESP module, every ~16 s, alternating):

  GET /rest/SetDeviceData?from=readings&message="PH":"7.6","RX":"278.7",...
      live measurements

  GET /rest/SetDeviceData?from=device&message="IsAuto":"true",...,"Program":"11",...
      the controller's FULL settings dump — the authoritative state for
      mode, program, dosing bands, temperature window, model and firmware.

  GET /rest/GetLastEdition?...
      poll for a pending setting edition (cloud -> controller).

Read-only by design: remote setting changes do NOT work on firmware 1.0.4c.
There is no cloud->controller command channel we can drive:

  * the `GetLastEdition` reply is ignored whatever we return ("OK", a JSON
    object, the bare `"Key":"value"` fragment the controller itself emits, a
    numeric id, or the complete settings dump with one field changed);
  * the `SetDeviceData` reply is equally ignored, for both `from=readings`
    and `from=device`;
  * nothing changes in the controller's next settings dump, and it never
    issues a follow-up request (so the reply is not an id to fetch either);
  * the third-party cloud integration for the same hardware exposes program
    and mode as read-only sensors and only switches the manual outputs, which
    matches what we observe.

Program and mode are therefore published as sensors. Both are set on the
controller itself (PROG button; a seasonal, persistent setting), and this
bridge reports what the controller is actually running.

`Program` is a 1-based index into the controller's pre-set program table. The
numbering below was corrected against the device itself (display shows WINTER
while the uplink reports 5, and PUBLIC POOL while it reports 11); the manual's
printed table is offset by one from WINTER onwards:

  1 AUTO TEMP   2 AUTO ORP   3 USER   4 unknown   5 WINTER
  6 SPRING/AUTUMN  7 SUMMER 20  8 SUMMER 25  9 SUMMER 28
  10 SUMMER 32  11 PUBLIC POOL  12 FLOW DETECTION
"""

import os
import json
import socket
import ssl
import threading
import logging
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import paho.mqtt.client as mqtt

# Request tap: log EVERY request the controller makes, including endpoints this
# bridge does not implement (e.g. a token refresh after a power cycle).
REQUEST_LOG_PATH = os.environ.get("REQUEST_LOG", "/tmp/bridge-requests.jsonl")


def edition_payload():
    """Return the configured edition payload as bytes, or None when unset."""
    raw = EDITION_REPLY
    if not raw and EDITION_REPLY_FILE:
        try:
            with open(EDITION_REPLY_FILE, encoding="utf-8") as f:
                raw = f.read().strip()
        except Exception as e:
            logger.warning("cannot read EDITION_REPLY_FILE %s: %s", EDITION_REPLY_FILE, e)
            return None
    return raw.encode("utf-8") if raw else None


def _reply_blob(inline, path):
    raw = inline
    if not raw and path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read().strip()
        except Exception as e:
            logger.warning("cannot read reply file %s: %s", path, e)
            return None
    return raw.encode("utf-8") if raw else None


def devicedata_payload():
    """Return the configured GetDeviceData document as bytes, or None."""
    return _reply_blob(DEVICEDATA_REPLY, DEVICEDATA_REPLY_FILE)


def force_401_endpoint(path, forced=None):
    """True when this request should be answered with the cloud's 401."""
    forced = FORCE_401_PATHS if forced is None else forced
    if not forced:
        return False
    return urllib.parse.urlparse(str(path)).path in forced


def tap_entry(method, path, http_version, headers, body, client, tls_version=None, cipher=None):
    """Build one record describing a request as it arrived, unmodified."""
    hdrs = {}
    try:
        hdrs = {str(k): str(v) for k, v in headers.items()}
    except Exception:
        pass
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "client": client,
        "method": method,
        "path": path,
        "http": http_version,
        "tls": tls_version,
        "cipher": cipher,
        "headers": hdrs,
        "body": body.decode("latin-1") if isinstance(body, (bytes, bytearray)) else (body or ""),
    }


KNOWN_ENDPOINTS = ("/rest/SetDeviceData", "/rest/GetLastEdition",
                   "/rest/GetDeviceData", "/Devices/DeviceStatus")
TAP_FULL = os.environ.get("TAP_FULL", "").lower() in ("1", "true", "yes")

# Experiment switch: answer selected endpoints with the cloud's 401 so the
# controller's reaction (e.g. a token refresh) can be observed in the request log.
# Empty by default; never affects normal operation.
FORCE_401_PATHS = [p.strip() for p in os.environ.get("FORCE_401_PATHS", "").split(",") if p.strip()]
FORCE_401_BODY = b"401 ACCESS TOKEN IS NOT VALID"

# Serve a captured edition payload to the controller instead of the bare "OK".
# The controller never acted on invented replies, but it has never been handed the
# real shape either; EDITION_REPLY (inline JSON) or EDITION_REPLY_FILE (path) does that.
EDITION_REPLY = os.environ.get("EDITION_REPLY", "").strip()
EDITION_REPLY_FILE = os.environ.get("EDITION_REPLY_FILE", "").strip()

# Serve a captured GetDeviceData document instead of the bridge's own JSON, so a
# changed field can be handed to the controller exactly as the cloud would.
DEVICEDATA_REPLY = os.environ.get("DEVICEDATA_REPLY", "").strip()
DEVICEDATA_REPLY_FILE = os.environ.get("DEVICEDATA_REPLY_FILE", "").strip()


def write_tap(record, path=None, full=None):
    """Append the record as one JSON line; never raise into the request path.

    Unknown endpoints are dumped in full on stdout, because the container log is
    the only channel that survives: this catches things like a token refresh that
    the bridge never implemented.
    """
    target = path or REQUEST_LOG_PATH
    line = json.dumps(record, ensure_ascii=False)
    if full is None:
        endpoint = urllib.parse.urlparse(str(record.get("path", ""))).path
        full = TAP_FULL or endpoint not in KNOWN_ENDPOINTS
    if full:
        logger.info("TAP %s", line)
    else:
        logger.info("TAP %s %s %s (http=%s tls=%s)",
                    record.get("client"), record.get("method"),
                    record.get("path"), record.get("http"), record.get("tls"))
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning("could not write request log %s: %s", target, e)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("oxymatic-bridge")

# Configuration from Environment
DEVICE_ID = os.environ.get("DEVICE_ID", "oxymatic")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Oxymatic")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto.mosquitto.svc.cluster.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TLS = os.environ.get("MQTT_TLS", "true").lower() in ("true", "1", "yes")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8443"))
CERT_FILE = os.environ.get("CERT_FILE", "/certs/tls.crt")
KEY_FILE = os.environ.get("KEY_FILE", "/certs/tls.key")

# Pre-set program table of the controller firmware (1-based).
PROGRAMS = {
    1: "AUTO TEMP",
    2: "AUTO ORP",
    3: "USER",
    # 4 is not documented and has not been identified on the device yet: the
    # firmware list carries one entry more than the manual's printed table
    # before WINTER. Reported as "PROGRAMMA 4" until it is read off the display.
    5: "WINTER",
    6: "SPRING/AUTUMN",
    7: "SUMMER 20",
    8: "SUMMER 25",
    9: "SUMMER 28",
    10: "SUMMER 32",
    11: "PUBLIC POOL",
    12: "FLOW DETECTION",
}
# Codes known to exist on this firmware even when their name is unknown.
KNOWN_CODES = range(1, 13)

# Mode <-> position flags. Exactly one flag is true in a settings dump.
MODE_FLAGS = {
    "auto": "IsAuto",
    "man": "IsMan",
    "off": "IsOff",
}

# Shared state between MQTT thread and HTTP handler
state_lock = threading.Lock()
readings = {
    "temperature": "11.0",
    "pH": "7.20",
    "redox": "300.0",
    "oxy_current": "0.00",
    "oxy_voltage": "0.00",
    "ion_current": "0.00",
    "ion_voltage": "0.00",
    "is_pump": "false",
    "copper": "0.10",
}
# Authoritative settings dump from the controller (from=device).
settings = {}
mqtt_client = None
last_published = {}

# Refresh mode/program even when the value did not change, so Home Assistant's
# "last updated" stays honest (the controller is read every ~32 s).
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))
_settings_last_force = 0.0

# --------------------------------------------------------------------------
# Write path: a document shaped like the cloud's GetDeviceData can be handed to
# the controller, which then adopts the program/mode it contains. Home Assistant
# expresses intent over MQTT; the bridge turns that into a document.
# --------------------------------------------------------------------------
TEMPLATE_FILE = os.environ.get(
    "OX_TEMPLATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "oxymatic_template.json"))
PROGRAM_CODES = {name: code for code, name in PROGRAMS.items()}
MODE_FIELDS = {
    "auto": {"IsAuto": True, "IsMan": False, "IsOff": False},
    "man": {"IsAuto": False, "IsMan": True, "IsOff": False},
    "off": {"IsAuto": False, "IsMan": False, "IsOff": True},
}
# What HA has asked for; None = follow the controller.
desired_program = None
desired_mode = None
# The last document we served. A document is a complete snapshot, so the next
# partial write must be layered on this, not on a settings dump that may be
# hours old (the controller only dumps at boot).
asserted: dict = {"program": None, "mode": None}
# Non-zero while an edition is waiting to be picked up (epoch seconds).
pending_edition = 0.0

COMMAND_TOPICS = {
    "program": "oxymatic/set/program",
    "mode": "oxymatic/set/mode",
    "state": "oxymatic/set/state",
}

# The controller only dumps its settings at boot, so a pod restart loses the
# authoritative state and anything derived from it. Both are mirrored to retained
# topics: the broker hands them back on connect, so a fresh pod resumes from what
# was true instead of falling back to the template's mode flags.
STATE_TOPICS = {
    "document": "oxymatic/state/document",
    "settings": "oxymatic/state/settings",
}


def load_template(path=None):
    try:
        with open(path or TEMPLATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if "document" in data and "edition" in data else None
    except Exception as e:
        logger.warning("no usable device template at %s: %s", path or TEMPLATE_FILE, e)
        return None


TEMPLATE = load_template()


def build_document():
    """The GetDeviceData document to serve: template + live readings + intent."""
    if not TEMPLATE:
        return None
    doc = dict(TEMPLATE["document"])
    pairs = (("temperature", "Temp", float), ("pH", "PH", float), ("redox", "RX", float),
             ("copper", "CU", float), ("oxy_voltage", "Volts", float),
             ("oxy_current", "Amps", float))
    for src, dst, caster in pairs:
        raw = readings.get(src)
        if raw in (None, ""):
            continue
        try:
            doc[dst] = caster(raw)
        except (TypeError, ValueError):
            pass
    if readings.get("is_pump") is not None:
        doc["IsPump"] = str(readings["is_pump"]).lower() == "true"
    # Only what was asked for is overridden. Everything else keeps the value the
    # controller reported: a mode-only command must not carry the template's
    # Program along with it (that switched PUBLIC POOL on during the AUTO test).
    if desired_program is not None:
        program = desired_program
    elif asserted["program"] is not None:
        program = asserted["program"]
    else:
        program = current_program_code()
    if program is not None:
        doc["Program"] = program
    mode = desired_mode or asserted["mode"] or current_mode()
    if mode in MODE_FIELDS:
        doc.update(MODE_FIELDS[mode])
    now_ms = int(time.time() * 1000)
    doc["LastEdition"] = "/Date(%d)/" % now_ms
    doc["LastProgEdition"] = "/Date(%d)/" % now_ms
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


def build_edition_list():
    """The GetLastEdition payload advertising the pending edition."""
    if not TEMPLATE:
        return None
    items = [["DataEdition", str(int(pending_edition))]]
    items += [[k, v] for k, v in TEMPLATE["edition"]]
    return json.dumps(items, separators=(",", ":")).encode("utf-8")


def request_edition():
    global pending_edition
    pending_edition = time.time()
    logger.info("Edition queued (program=%s mode=%s)", desired_program, desired_mode)


def known_program():
    """The program the bridge can vouch for: intent, last serve, or the dump."""
    if desired_program is not None:
        return desired_program
    if asserted["program"] is not None:
        return asserted["program"]
    return current_program_code()


def known_mode():
    if desired_mode is not None:
        return desired_mode
    if asserted["mode"] is not None:
        return asserted["mode"]
    return current_mode()


def apply_state(value):
    """Set program and mode together: 'WINTER:off' or a JSON object.

    The document is authoritative for every field it carries, and a single-field
    command has to inherit the other from something. When nothing is known (fresh
    pod, controller has not dumped yet) that inheritance would fall back to the
    template's defaults, which is how the unit ended up on PUBLIC POOL twice.
    """
    text = str(value).strip()
    if text.startswith("{"):
        try:
            blob = json.loads(text)
            program, mode = blob.get("program"), blob.get("mode")
        except ValueError:
            logger.warning("ignoring unparseable state command %r", value)
            return False
    else:
        parts = [p.strip() for p in text.split(":")]
        if len(parts) != 2:
            logger.warning("state command wants 'PROGRAM:MODE', got %r", value)
            return False
        program, mode = parts
    global desired_program, desired_mode
    code = program_code(program)
    mode_text = str(mode).strip().lower()
    if code is None or mode_text not in MODE_FIELDS:
        logger.warning("state command rejected %r", value)
        return False
    desired_program = code
    desired_mode = mode_text
    request_edition()
    return True


def program_code(value):
    """Resolve a program name or numeric code to the controller's code."""
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    for name, code in PROGRAM_CODES.items():
        if name.lower() == text.lower():
            return code
    return None


def apply_program(value):
    """Set the desired program from a name or a numeric code."""
    global desired_program
    code = program_code(value)
    if code is None:
        logger.warning("ignoring unknown program %r", value)
        return False
    if known_mode() is None and desired_mode is None:
        logger.warning("refusing program %r: the controller's mode is unknown, so a "
                       "document would assert the template's IsAuto. Set both with "
                       "'%s' (PROGRAM:MODE) or power-cycle the controller.",
                       value, "oxymatic/set/state")
        return False
    desired_program = code
    request_edition()
    return True


def apply_mode(value):
    global desired_mode
    text = str(value).strip().lower()
    if text not in MODE_FIELDS:
        logger.warning("ignoring unknown mode %r (want auto/man/off)", value)
        return False
    if known_program() is None and desired_program is None:
        logger.warning("refusing mode %r: the controller's program is unknown, so a "
                       "document would assert the template's Program. Set both with "
                       "'%s' (PROGRAM:MODE) or power-cycle the controller.",
                       value, "oxymatic/set/state")
        return False
    desired_mode = text
    request_edition()
    return True


def generate_self_signed_cert_if_needed(cert_path, key_path):
    """Generate a valid self-signed certificate if none exists."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        logger.info("Using existing TLS certificates at %s", cert_path)
        return

    logger.info("Generating self-signed certificate for oxymaticapp.hydrover.eu...")
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    os.system(
        f'openssl req -x509 -newkey rsa:2048 -keyout "{key_path}" -out "{cert_path}" '
        f'-days 3650 -nodes -subj "/CN=oxymaticapp.hydrover.eu" '
        f'-addext "subjectAltName=DNS:oxymaticapp.hydrover.eu,IP:20.61.77.136"'
    )


# --------------------------------------------------------------------------
# Home Assistant discovery / publishing
# --------------------------------------------------------------------------

def build_discovery_payload(sensor_key, name, unit=None, device_class=None,
                            component="sensor", numeric=None,
                            attributes_topic=None, icon=None, object_id=None):
    """Construct a Home Assistant MQTT Discovery configuration dict."""
    device_info = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "Hydrover",
        "model": "Oxymatic Controller (Local)",
    }

    state_topic = f"homeassistant/{component}/{DEVICE_ID}/{sensor_key}/state"

    payload = {
        "name": name,
        "state_topic": state_topic,
        "unique_id": f"{DEVICE_ID}_{sensor_key}",
        "device": device_info,
    }

    if unit:
        payload["unit_of_measurement"] = unit
    if device_class:
        payload["device_class"] = device_class
    if icon:
        payload["icon"] = icon
    if attributes_topic:
        payload["json_attributes_topic"] = attributes_topic
    if object_id:
        payload["object_id"] = object_id

    # State class measurement for chartable numeric sensors
    if component == "sensor" and (numeric is True or unit is not None):
        payload["state_class"] = "measurement"

    return payload


def publish_discovery_configs(client):
    """Publish (or retire) Home Assistant MQTT Auto-Discovery definitions."""
    configs = [
        # sensor_key, name, unit, device_class, component, numeric
        ("temperature", "Temperature", "°C", "temperature", "sensor", True),
        ("pH", "pH", None, "ph", "sensor", True),
        ("redox", "Redox", "mV", None, "sensor", True),
        ("oxy_current", "OXY Current", "A", "current", "sensor", True),
        ("oxy_voltage", "OXY Voltage", "V", "voltage", "sensor", True),
        ("ion_current", "ION Current", "A", "current", "sensor", True),
        ("ion_voltage", "ION Voltage", "V", "voltage", "sensor", True),
        ("copper", "Copper (CU)", "mg/l", None, "sensor", True),
        ("prog", "Program", None, None, "sensor", False),
        ("mode", "Mode", None, None, "sensor", False),
        ("pump", "Pump Running", None, "running", "binary_sensor", False),
    ]

    for sensor_key, name, unit, dev_class, comp, num in configs:
        attributes_topic = None
        icon = None
        object_id = None
        if sensor_key == "prog":
            attributes_topic = f"homeassistant/sensor/{DEVICE_ID}/prog/attributes"
            icon = "mdi:calendar-clock"
        if sensor_key == "mode":
            icon = "mdi:auto-mode"
            attributes_topic = f"homeassistant/sensor/{DEVICE_ID}/mode/attributes"
        if sensor_key == "pH":
            attributes_topic = f"homeassistant/sensor/{DEVICE_ID}/pH/attributes"
        topic = f"homeassistant/{comp}/{DEVICE_ID}/{sensor_key}/config"
        payload = build_discovery_payload(
            sensor_key=sensor_key,
            name=name,
            unit=unit,
            device_class=dev_class,
            component=comp,
            numeric=num,
            attributes_topic=attributes_topic,
            icon=icon,
            object_id=object_id,
        )
        client.publish(topic, json.dumps(payload), retain=True)
        logger.debug("Published discovery config for %s", sensor_key)

    # Write entities: these are the settable pair. A command publishes on the
    # command topic; the bridge queues an edition and the controller adopts it.
    device_info = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "Hydrover",
        "model": "Oxymatic Controller (Local)",
    }
    selects = [
        ("programma", "Programma", COMMAND_TOPICS["program"],
         [name for _, name in sorted(PROGRAMS.items())], "mdi:calendar-clock"),
        ("mode_select", "Modus", COMMAND_TOPICS["mode"],
         ["auto", "man", "off"], "mdi:auto-mode"),
    ]
    for key, name, command_topic, options, icon in selects:
        payload = {
            "name": name,
            "unique_id": f"{DEVICE_ID}_{key}",
            "state_topic": f"homeassistant/select/{DEVICE_ID}/{key}/state",
            "command_topic": command_topic,
            "options": options,
            "icon": icon,
            "device": device_info,
        }
        client.publish(f"homeassistant/select/{DEVICE_ID}/{key}/config",
                       json.dumps(payload), retain=True)
        logger.debug("Published select discovery config for %s", key)

    # Retire entities the bridge used to publish but cannot honour: the
    # controller does not accept remote setting changes.
    for stale in (
        f"homeassistant/binary_sensor/{DEVICE_ID}/cell/config",
        f"homeassistant/binary_sensor/{DEVICE_ID}/cell/state",
        f"homeassistant/select/{DEVICE_ID}/mode/config",
        f"homeassistant/select/{DEVICE_ID}/prog_sel/config",
        f"homeassistant/select/{DEVICE_ID}/mode/state",
        f"homeassistant/select/{DEVICE_ID}/prog_sel/state",
        f"homeassistant/sensor/undefined/oxy_current/config",
        f"homeassistant/sensor/undefined/oxy_voltage/config",
    ):
        client.publish(stale, "", retain=True)
    # The old cloud-scraping bridge published login errors on this topic.
    client.publish(f"homeassistant/text/{DEVICE_ID}/alert/state", "", retain=True)

    logger.info("Published all Home Assistant MQTT discovery configs")


def publish_state(client, sensor_key, value, component="sensor", force=False,
                  attributes=None):
    """Publish a state update (retained), skipping duplicates unless forced."""
    if value is None:
        return
    topic = f"homeassistant/{component}/{DEVICE_ID}/{sensor_key}/state"
    val_str = str(value).replace(",", ".")
    cache_key = f"{component}/{sensor_key}"
    attrs_str = None if attributes is None else json.dumps(attributes, ensure_ascii=False, sort_keys=True)
    if not force and last_published.get(cache_key) == (val_str, attrs_str):
        return
    last_published[cache_key] = (val_str, attrs_str)
    client.publish(topic, val_str, retain=True)
    if attributes is not None:
        client.publish(
            f"homeassistant/{component}/{DEVICE_ID}/{sensor_key}/attributes",
            attrs_str,
            retain=True,
        )


def publish_readings(client):
    """Publish the live measurements."""
    publish_state(client, "pH", readings["pH"], attributes={
        "low_setpoint": settings.get("PH0"),
        "high_setpoint": settings.get("PH100"),
        "ph_pump_enabled": settings.get("PHOn"),
    })
    publish_state(client, "redox", readings["redox"])
    publish_state(client, "temperature", readings["temperature"])
    publish_state(client, "oxy_current", readings["oxy_current"])
    publish_state(client, "oxy_voltage", readings["oxy_voltage"])
    publish_state(client, "ion_current", readings["ion_current"])
    publish_state(client, "ion_voltage", readings["ion_voltage"])
    publish_state(client, "copper", readings["copper"])

    pump_state = "ON" if str(readings["is_pump"]).lower() == "true" else "OFF"
    publish_state(client, "pump", pump_state, component="binary_sensor")

    # No electrode-health entity on purpose. The manual's "voltage must stay
    # under 9.5 V, otherwise add sea salt" only applies to a classic salt/
    # electrolysis pool: this installation is a natural, planted, chlorine-free
    # pool where salt is not an option. The cell also sits idle below ~15 °C by
    # design (float voltage ~10.6 V at ~0 A), so a volts-based verdict says
    # nothing. Oxy voltage/current are published as raw sensors instead.


def current_mode():
    """Derive the mode from the controller's position flags."""
    for mode, flag in MODE_FLAGS.items():
        if str(settings.get(flag, "")).lower() == "true":
            return mode
    return None


def mode_from_flags(blob):
    """Which mode a document/settings blob is asserting, from its position flags."""
    if not isinstance(blob, dict):
        return None
    for mode, flag in MODE_FLAGS.items():
        if blob.get(flag) is True or str(blob.get(flag, "")).lower() == "true":
            return mode
    return None


def current_program_code():
    """The controller's own Program code, as reported in its settings dump."""
    raw = settings.get("Program")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def current_program():
    """Derive the program name from the controller's numeric Program code."""
    raw = settings.get("Program")
    if raw is None:
        return None
    try:
        code = int(str(raw).strip())
    except ValueError:
        return None
    if code in PROGRAMS:
        return PROGRAMS[code]
    # Known code, name not identified yet: show the code rather than nothing,
    # so the entity never keeps a stale program name.
    if code in KNOWN_CODES:
        return f"PROGRAMMA {code}"
    return None


def publish_write_state(client, force=False):
    """Publish the program/mode HA has asked for (the select entities' state)."""
    programme = asserted["program"]
    if programme is None:
        programme = desired_program if desired_program is not None else current_program_code()
    if programme is not None:
        publish_state(client, "programma", PROGRAMS.get(programme, f"PROGRAMMA {programme}"),
                      component="select", force=force,
                      attributes={"code": programme,
                                  "source": "asserted" if asserted["program"] is not None
                                            else "controller"})
    mode = desired_mode or asserted["mode"] or current_mode()
    if mode:
        publish_state(client, "mode_select", mode, component="select", force=force,
                      attributes={"source": "asserted" if asserted["mode"] else "controller"})


def on_mqtt_message(client, userdata, msg):
    """Turn an HA command into a queued edition for the controller."""
    topic = msg.topic
    try:
        value = msg.payload.decode("utf-8", "replace").strip()
    except Exception:
        return
    if topic == STATE_TOPICS["settings"]:
        if not settings:
            try:
                settings.update(json.loads(value))
                logger.info("Restored settings from retained mirror: mode=%s program=%s",
                            current_mode(), current_program())
            except ValueError:
                pass
        return
    if topic == STATE_TOPICS["document"]:
        if asserted["program"] is None and asserted["mode"] is None:
            try:
                blob = json.loads(value)
            except ValueError:
                return
            asserted["program"] = blob.get("Program")
            asserted["mode"] = mode_from_flags(blob)
            logger.info("Restored the last served document: program=%s mode=%s",
                        asserted["program"], asserted["mode"])
        return
    logger.info("MQTT command on %s: %r", topic, value)
    if topic == COMMAND_TOPICS["program"]:
        if apply_program(value):
            publish_write_state(client, force=True)
    elif topic == COMMAND_TOPICS["mode"]:
        if apply_mode(value):
            publish_write_state(client, force=True)
    elif topic == COMMAND_TOPICS["state"]:
        if apply_state(value):
            publish_write_state(client, force=True)


def publish_settings_state(client, force=False):
    """Publish the mode and program reported by the controller itself.

    The settings dump arrives every ~32 s but the values rarely change, and
    publishing only on change leaves Home Assistant showing a timestamp days
    old for something that is being read continuously — which reads as a dead
    entity. Refresh unconditionally at least every REFRESH_SECONDS so "last
    updated" means what people think it means.
    """
    global _settings_last_force
    now = time.monotonic()
    if not force and now - _settings_last_force >= REFRESH_SECONDS:
        force = True
    if force:
        _settings_last_force = now

    mode = current_mode()
    if mode:
        publish_state(client, "mode", mode, force=force, attributes={
            "source": "controller",
            "remote_control": settings.get("RemoteControl"),
        })

    program = current_program()
    if program:
        raw_code = str(settings.get("Program", "")).strip()
        publish_state(client, "prog", program, force=force, attributes={
            "code": int(raw_code) if raw_code.isdigit() else raw_code,
            "source": "controller",
            "model": settings.get("Model"),
            "firmware": settings.get("Version"),
            "mode": mode,
        })


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    """Callback for MQTT broker connection."""
    if rc == 0:
        logger.info("Connected to Mosquitto MQTT broker at %s:%d", MQTT_HOST, MQTT_PORT)
        publish_discovery_configs(client)
        publish_write_state(client, force=True)
        for topic in STATE_TOPICS.values():
            client.subscribe(topic)
        for topic in COMMAND_TOPICS.values():
            client.subscribe(topic)
            logger.info("Subscribed to command topic %s", topic)
        with state_lock:
            publish_settings_state(client, force=True)
            publish_readings(client)
    else:
        logger.error("Failed to connect to MQTT broker, return code %s", rc)


# --------------------------------------------------------------------------
# HTTP emulation of the Hydrover cloud API
# --------------------------------------------------------------------------

def parse_message(raw_message):
    """Parse the controller's fragment: "KEY":"value","KEY2":"value2"."""
    payload_json = "{" + raw_message.strip().strip("{}") + "}"
    data = json.loads(payload_json)
    if not isinstance(data, dict):
        raise ValueError("message is not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


class OxymaticRequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP/1.0 and HTTP/1.1 requests from the Oxymatic controller."""

    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def _tap(self, method):
        """Log the raw request before any handling decides what to do with it."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = b""
        if length > 0 and method != "GET":
            try:
                body = self.rfile.read(length)
            except Exception:
                body = b""
        tls_version = cipher = None
        try:
            tls_version = self.connection.version()
            c = self.connection.cipher()
            cipher = c[0] if c else None
        except Exception:
            pass
        write_tap(tap_entry(method, self.path, self.request_version, self.headers,
                            body, self.client_address[0], tls_version, cipher))

    def _reply_ok(self):
        payload = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self._tap("POST")
        self._reply_ok()

    def do_PUT(self):
        self._tap("PUT")
        self._reply_ok()

    def do_DELETE(self):
        self._tap("DELETE")
        self._reply_ok()

    def do_HEAD(self):
        self._tap("HEAD")
        self._reply_ok()

    def _forced_401(self):
        """Return the cloud's 401 for configured paths; True when it answered."""
        if not force_401_endpoint(self.path):
            return False
        endpoint = urllib.parse.urlparse(self.path).path
        logger.warning("FORCE_401 answering %s with 401 (experiment)", endpoint)
        self.send_response(401)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(FORCE_401_BODY)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(FORCE_401_BODY)
        return True

    def do_GET(self):
        self._tap("GET")
        if self._forced_401():
            return
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)

        endpoint = parsed_url.path

        if endpoint == "/rest/SetDeviceData":
            self.handle_set_device_data(query_params)
        elif endpoint == "/rest/GetLastEdition":
            self.handle_get_last_edition(query_params)
        elif endpoint in ("/rest/GetDeviceData", "/Devices/DeviceStatus"):
            self.handle_get_device_data()
        else:
            logger.info("Unknown GET endpoint requested: %s", endpoint)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"OK")

    def handle_set_device_data(self, params):
        """Processes incoming telemetry from the Oxymatic controller."""
        raw_message = params.get("message", [""])[0]
        source = params.get("from", [""])[0]

        if not raw_message:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        try:
            data = parse_message(raw_message)
        except Exception as e:
            logger.error("Failed to parse message (from=%s): %r (%s)", source, raw_message, e)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        if source == "readings":
            self._apply_readings(data, raw_message)
        elif source in ("device", "settings"):
            self._apply_settings(data)
        else:
            logger.info("Unknown message source %r with keys: %s", source, sorted(data))

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"OK")

    def _apply_readings(self, data, raw_message):
        if not any(k in data for k in ("PH", "RX", "Temp", "Volts", "Amps")):
            logger.info("Non-reading payload (from=readings) keys: %s", sorted(data))
            return

        logger.info(
            "Readings: pH=%s, RX=%s mV, Temp=%s °C, CU=%s, Volts=%s, Amps=%s, Pump=%s",
            data.get("PH"), data.get("RX"), data.get("Temp"),
            data.get("CU"), data.get("Volts"), data.get("Amps"), data.get("IsPump")
        )

        with state_lock:
            if "PH" in data:
                readings["pH"] = data["PH"]
            if "RX" in data:
                try:
                    readings["redox"] = f"{float(data['RX']):.1f}"
                except ValueError:
                    readings["redox"] = data["RX"]
            if "Temp" in data:
                readings["temperature"] = data["Temp"]
            if "Amps" in data:
                readings["oxy_current"] = data["Amps"]
            if "Volts" in data:
                readings["oxy_voltage"] = data["Volts"]
            if "IonAmps" in data:
                readings["ion_current"] = data["IonAmps"]
            if "IonVolts" in data:
                readings["ion_voltage"] = data["IonVolts"]
            if "CU" in data:
                readings["copper"] = data["CU"]
            if "IsPump" in data:
                readings["is_pump"] = data["IsPump"]

            if mqtt_client and mqtt_client.is_connected():
                publish_readings(mqtt_client)

    def _apply_settings(self, data):
        """Full settings dump: authoritative mode / program / setpoints."""
        with state_lock:
            changed = (settings.get("Program") != data.get("Program") or
                       settings.get("IsAuto") != data.get("IsAuto") or
                       settings.get("IsMan") != data.get("IsMan") or
                       settings.get("IsOff") != data.get("IsOff"))
            settings.clear()
            settings.update(data)

            mode = current_mode()
            program = current_program()
            if changed:
                logger.info(
                    "Settings changed: mode=%s, program=%s (code %s)",
                    mode, program, settings.get("Program"),
                )
            else:
                logger.debug(
                    "Settings: mode=%s, program=%s (code %s), model=%s fw=%s, "
                    "pH band %s-%s, temp %s-%s, pump-time %s",
                    mode, program, settings.get("Program"), settings.get("Model"),
                    settings.get("Version"), settings.get("PH0"), settings.get("PH100"),
                    settings.get("TempMin"), settings.get("TempMax"), settings.get("TimePump"),
                )

            if mqtt_client and mqtt_client.is_connected():
                mqtt_client.publish(STATE_TOPICS["settings"],
                                    json.dumps(dict(settings)), retain=True)
                publish_settings_state(mqtt_client, force=changed)

    def handle_get_last_edition(self, params):
        """Reply to the controller's edition poll.

        A queued write (HA asked for a program/mode) advertises a fresh
        DataEdition, which makes the controller fetch GetDeviceData and adopt it.
        Otherwise the EDITION_REPLY payload or a plain OK is served.
        """
        global pending_edition
        body = None
        content_type = "text/plain"
        if pending_edition and TEMPLATE:
            body = build_edition_list()
            content_type = "application/json"
            logger.info("Serving edition DataEdition=%d (program=%s mode=%s)",
                        int(pending_edition), desired_program, desired_mode)
        if body is None:
            body = edition_payload() or b"OK"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def handle_get_device_data(self):
        """Serve the document that carries the requested program/mode."""
        global pending_edition, desired_program, desired_mode
        if pending_edition and TEMPLATE:
            doc = build_document()
            if doc:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(doc)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(doc)
                # Remember the snapshot we handed over: the next partial write
                # layers onto it. The settings dump cannot serve this purpose —
                # it only arrives at controller boot, so it is usually stale.
                served = json.loads(doc)
                logger.info("Served document: program=%s mode=%s — edition handed over",
                            served.get("Program"), mode_from_flags(served))
                pending_edition = 0.0
                asserted["program"] = served.get("Program")
                asserted["mode"] = mode_from_flags(served) or asserted["mode"]
                if mqtt_client and mqtt_client.is_connected():
                    mqtt_client.publish(STATE_TOPICS["document"],
                                        json.dumps(served), retain=True)
                desired_program = None
                desired_mode = None
                if mqtt_client and mqtt_client.is_connected():
                    publish_write_state(mqtt_client, force=True)
                return
        payload = devicedata_payload()
        if payload is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            return
        with state_lock:
            data = json.dumps({
                "readings": readings,
                "settings": settings,
                "mode": current_mode(),
                "program": current_program(),
            }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


def start_mqtt():
    """Initialize and run the MQTT client loop."""
    global mqtt_client
    # Container PIDs are always 1, so include the hostname: without it a
    # rolling update makes the old and new pod fight over the same client id
    # and both reconnect in a loop.
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              client_id=f"oxymatic_bridge_{socket.gethostname()}")

    if MQTT_USERNAME and MQTT_PASSWORD:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    if MQTT_TLS:
        mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
        mqtt_client.tls_insecure_set(True)

    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message

    logger.info("Connecting to MQTT broker %s:%d...", MQTT_HOST, MQTT_PORT)
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        logger.error("Failed to initiate MQTT connection: %s", e)


def main():
    logger.info("Starting Hydrover Oxymatic Local Bridge on port %d...", LISTEN_PORT)
    generate_self_signed_cert_if_needed(CERT_FILE, KEY_FILE)

    # Start MQTT client
    start_mqtt()

    # Create HTTPS Server
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), OxymaticRequestHandler)

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)

    logger.info("TLS server listening on 0.0.0.0:%d (oxymaticapp.hydrover.eu emulator ready)", LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        server.server_close()
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    main()
