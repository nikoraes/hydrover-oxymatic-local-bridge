#!/usr/bin/env python3
"""
Hydrover Oxymatic Local Bridge
-----------------------------
A local drop-in emulator and MQTT bridge for Hydrover Oxymatic pool controllers.

Intercepts outbound HTTPS/TLS telemetry sent by the Oxymatic controller's ESP
Wi-Fi module (via DNS rewrite or static router deflection), publishes it to
Mosquitto MQTT using Home Assistant MQTT Discovery standards, and delivers
pending setting editions (mode / program / setpoints) back to the controller.

Eliminates all cloud dependencies (oxymaticapp.hydrover.eu / 20.61.77.136).

Uplink protocol (observed from the ESP module, every ~16 s, alternating):

  GET /rest/SetDeviceData?from=readings&message="PH":"7.6","RX":"278.7",...
      live measurements

  GET /rest/SetDeviceData?from=device&message="IsAuto":"true",...,"Program":"11",...
      the controller's FULL settings dump — this is the authoritative state for
      mode and program, and it is what makes closed-loop control verifiable.

  GET /rest/GetLastEdition?...
      poll for a pending setting edition; the reply body is applied by the MCU.

`Program` is a 1-based index into the controller's pre-set program table
(Hydrover Oxymatic Smart manual §8.2, table for systems with copper):

  1 AUTO TEMP   2 AUTO ORP    3 USER        4 WINTER
  5 SPRING/AUTUMN  6 SUMMER 20  7 SUMMER 25  8 SUMMER 28
  9 SUMMER 32  10 SUMMER >32  11 PUBLIC POOL  12 FLOW DETECTION

Mode is reported as one of the mutually-exclusive position flags
IsAuto / IsMan / IsOff.
"""

import os
import sys
import json
import time
import socket
import ssl
import threading
import logging
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import paho.mqtt.client as mqtt

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

# How long to wait for the controller to confirm an edition before retrying.
CONFIRM_TIMEOUT = int(os.environ.get("CONFIRM_TIMEOUT", "90"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))

COMMAND_TOPIC_MODE = f"homeassistant/select/{DEVICE_ID}/mode/set"
COMMAND_TOPIC_PROG = f"homeassistant/select/{DEVICE_ID}/prog/set"

# Pre-set program table of the controller firmware (1-based).
PROGRAMS = {
    1: "AUTO TEMP",
    2: "AUTO ORP",
    3: "USER",
    4: "WINTER",
    5: "SPRING/AUTUMN",
    6: "SUMMER 20",
    7: "SUMMER 25",
    8: "SUMMER 28",
    9: "SUMMER 32",
    10: "SUMMER >32",
    11: "PUBLIC POOL",
    12: "FLOW DETECTION",
}
PROGRAM_OPTIONS = [PROGRAMS[i] for i in sorted(PROGRAMS)]

# Mode <-> position flags. Exactly one flag is true in a settings dump.
MODE_FLAGS = {
    "auto": "IsAuto",
    "man": "IsMan",
    "off": "IsOff",
}
MODE_OPTIONS = ["auto", "man", "off"]


def _norm(text):
    """Normalise a program name for lookups: case, spaces, slashes, accents."""
    return (
        str(text)
        .strip()
        .upper()
        .replace("_", " ")
        .replace("/", " ")
        .replace("-", " ")
        .replace(">", "")
        .replace("  ", " ")
    )


PROGRAM_BY_NAME = {_norm(name): code for code, name in PROGRAMS.items()}
# A few tolerant aliases so a human/automation payload still resolves.
PROGRAM_BY_NAME.update({
    _norm("PUBLIC POOL"): 11,
    _norm("PISCINA PUBLICA"): 11,
    _norm("SUMMER>32"): 10,
    _norm("FLOW"): 12,
})


def resolve_program(value):
    """Map an inbound payload (name or numeric code) to a program code."""
    text = str(value).strip()
    if text.isdigit():
        code = int(text)
        return code if code in PROGRAMS else None
    return PROGRAM_BY_NAME.get(_norm(text))


def resolve_mode(value):
    text = str(value).strip().lower()
    if text in ("auto", "automatic"):
        return "auto"
    if text in ("man", "manual"):
        return "man"
    if text in ("off", "stop"):
        return "off"
    return None


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
# Values we asked for and the controller has not echoed back yet:
#   key -> {"value": "...", "requested": ts, "attempts": n}
pending_wanted = {}
# Edition payload waiting to be handed to the controller on its next poll.
pending_edition = {}
mqtt_client = None
last_published = {}


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
                            component="sensor", options=None, numeric=None,
                            command_topic=None, attributes_topic=None,
                            icon=None):
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

    # State class measurement for chartable numeric sensors
    if component == "sensor" and (numeric is True or unit is not None):
        payload["state_class"] = "measurement"

    if component == "select":
        payload["options"] = options or MODE_OPTIONS
        payload["command_topic"] = command_topic or f"homeassistant/select/{DEVICE_ID}/{sensor_key}/set"
        # HA updates the entity optimistically on command; the controller's own
        # settings dump overwrites it a few seconds later, so the entity always
        # converges on the truth.

    return payload


def publish_discovery_configs(client):
    """Publish Home Assistant MQTT Auto-Discovery definitions."""
    configs = [
        # sensor_key, name, unit, device_class, component, options, numeric, command_topic
        ("temperature", "Temperature", "°C", "temperature", "sensor", None, True, None),
        ("pH", "pH", None, "ph", "sensor", None, True, None),
        ("redox", "Redox", "mV", None, "sensor", None, True, None),
        ("oxy_current", "OXY Current", "A", "current", "sensor", None, True, None),
        ("oxy_voltage", "OXY Voltage", "V", "voltage", "sensor", None, True, None),
        ("ion_current", "ION Current", "A", "current", "sensor", None, True, None),
        ("ion_voltage", "ION Voltage", "V", "voltage", "sensor", None, True, None),
        ("copper", "Copper (CU)", "mg/l", None, "sensor", None, True, None),
        ("mode", "Mode", None, None, "select", MODE_OPTIONS, False, COMMAND_TOPIC_MODE),
        ("prog", "Program", None, None, "sensor", None, False, None),
        ("prog_sel", "Oxymatic programma", None, None, "select", PROGRAM_OPTIONS, False,
         COMMAND_TOPIC_PROG),
        ("cell", "Electrolyse cel", None, "problem", "binary_sensor", None, False, None),
        ("pump", "Pump Running", None, "running", "binary_sensor", None, False, None),
    ]

    for sensor_key, name, unit, dev_class, comp, opts, num, cmd_top in configs:
        attributes_topic = None
        icon = None
        if sensor_key == "prog":
            attributes_topic = f"homeassistant/sensor/{DEVICE_ID}/prog/attributes"
            icon = "mdi:calendar-clock"
        if sensor_key == "pH":
            attributes_topic = f"homeassistant/sensor/{DEVICE_ID}/pH/attributes"
        topic = f"homeassistant/{comp}/{DEVICE_ID}/{sensor_key}/config"
        payload = build_discovery_payload(
            sensor_key=sensor_key,
            name=name,
            unit=unit,
            device_class=dev_class,
            component=comp,
            options=opts,
            numeric=num,
            command_topic=cmd_top,
            attributes_topic=attributes_topic,
            icon=icon,
        )
        client.publish(topic, json.dumps(payload), retain=True)
        logger.debug("Published discovery config for %s", sensor_key)

    # Clear discovery payloads left behind by the retired cloud-scraping bridge.
    for stale in (
        f"homeassistant/sensor/undefined/oxy_current/config",
        f"homeassistant/sensor/undefined/oxy_voltage/config",
    ):
        client.publish(stale, "", retain=True)
    # The old bridge published login errors on this retained topic.
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
    cached = last_published.get(cache_key)
    if not force and cached == (val_str, attrs_str):
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

    # Electrode health, straight from the Hydrover manual: the cell voltage must
    # stay under 9.5 V; at or above it the water lacks salinity and the cell
    # cannot produce (amps collapse to ~0).
    try:
        volts = float(str(readings["oxy_voltage"]).replace(",", "."))
        cell_problem = "ON" if volts >= 9.5 else "OFF"
    except (TypeError, ValueError):
        cell_problem = "OFF"
    publish_state(client, "cell", cell_problem, component="binary_sensor")


def current_mode():
    """Derive the mode from the controller's position flags."""
    for mode, flag in MODE_FLAGS.items():
        if str(settings.get(flag, "")).lower() == "true":
            return mode
    return None


def current_program():
    """Derive the program name from the controller's numeric Program code."""
    raw = settings.get("Program")
    if raw is None:
        return None
    try:
        return PROGRAMS.get(int(str(raw).strip()))
    except ValueError:
        return None


def publish_settings_state(client, force=False):
    """Publish the mode and program reported by the controller itself."""
    mode = current_mode()
    if mode:
        publish_state(client, "mode", mode, component="select", force=force)

    program = current_program()
    if program:
        raw_code = settings.get("Program")
        publish_state(client, "prog", program, force=force, attributes={
            "code": int(str(raw_code).strip()) if str(raw_code).strip().isdigit() else raw_code,
            "source": "controller",
            "model": settings.get("Model"),
            "firmware": settings.get("Version"),
            "mode": mode,
        })
        # The select shares the state topic; republish so HA keeps both in sync.
        publish_state(client, "prog_sel", program, component="select", force=force)


def publish_alert(client, message):
    """Publish a diagnostic message (empty string clears it)."""
    client.publish(f"homeassistant/text/{DEVICE_ID}/alert/state", message, retain=True)


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    """Callback for MQTT broker connection."""
    if rc == 0:
        logger.info("Connected to Mosquitto MQTT broker at %s:%d", MQTT_HOST, MQTT_PORT)
        publish_discovery_configs(client)
        client.subscribe(COMMAND_TOPIC_MODE)
        client.subscribe(COMMAND_TOPIC_PROG)
        logger.info("Subscribed to command topics: %s, %s", COMMAND_TOPIC_MODE, COMMAND_TOPIC_PROG)
        with state_lock:
            publish_settings_state(client, force=True)
            publish_readings(client)
    else:
        logger.error("Failed to connect to MQTT broker, return code %s", rc)


def queue_edition(client, wanted, edition):
    """Queue setting values for the controller and track them until confirmed."""
    now = time.time()
    with state_lock:
        for key, value in wanted.items():
            previous = pending_wanted.get(key)
            pending_wanted[key] = {
                "value": str(value),
                "edition": dict(edition),
                "requested": now,
                "attempts": 1 if previous is None else previous.get("attempts", 1) + 1,
            }
        pending_edition.update(edition)
    logger.info("Queued edition for controller: %s (awaiting confirmation)", edition)


def on_mqtt_message(client, userdata, msg):
    """Handle incoming control commands from Home Assistant or Node-RED."""
    topic = msg.topic
    payload = msg.payload.decode("utf-8").strip()
    logger.info("MQTT Command received on %s: %s", topic, payload)

    if topic == COMMAND_TOPIC_MODE:
        mode = resolve_mode(payload)
        if mode is None:
            logger.warning("Unknown mode command: %s", payload)
            return
        flag = MODE_FLAGS[mode]
        edition = {name: ("true" if name == flag else "false") for name in MODE_FLAGS.values()}
        queue_edition(client, {flag: "true"}, edition)

    elif topic == COMMAND_TOPIC_PROG:
        code = resolve_program(payload)
        if code is None:
            logger.warning("Unknown program command: %s (valid: %s)", payload,
                           ", ".join(PROGRAM_OPTIONS))
            publish_alert(client, f"Onbekend programma: {payload}")
            return
        queue_edition(client, {"Program": str(code)}, {"Program": str(code)})


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

    def do_GET(self):
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
        guid = params.get("guid", [""])[0]
        token = params.get("token", [""])[0]
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
            settings.clear()
            settings.update(data)

            mode = current_mode()
            program = current_program()
            logger.info(
                "Settings: mode=%s, program=%s (code %s), model=%s fw=%s, pH band %s-%s, "
                "temp %s-%s, pump-time %s",
                mode, program, settings.get("Program"), settings.get("Model"),
                settings.get("Version"), settings.get("PH0"), settings.get("PH100"),
                settings.get("TempMin"), settings.get("TempMax"), settings.get("TimePump"),
            )

            # Did the controller confirm what we asked for?
            now = time.time()
            confirmed = []
            expired = []
            for key, want in list(pending_wanted.items()):
                actual = str(settings.get(key, "")).strip().lower()
                if actual == want["value"].lower():
                    confirmed.append(f"{key}={want['value']}")
                    del pending_wanted[key]
                elif now - want["requested"] > CONFIRM_TIMEOUT:
                    if want["attempts"] >= MAX_RETRIES:
                        expired.append(f"{key}={want['value']}")
                        del pending_wanted[key]
                    else:
                        logger.warning(
                            "Controller has not applied %s=%s after %.0fs — re-sending (attempt %d)",
                            key, want["value"], now - want["requested"], want["attempts"] + 1,
                        )
                        pending_wanted[key] = dict(want, requested=now,
                                                   attempts=want["attempts"] + 1)
                        pending_edition.update(want["edition"])

            if confirmed:
                logger.info("Controller confirmed: %s", ", ".join(confirmed))
                if not pending_wanted and mqtt_client:
                    publish_alert(mqtt_client, "")

            if expired:
                logger.error("Controller refused/ignored: %s", ", ".join(expired))
                if mqtt_client:
                    publish_alert(mqtt_client, "Kon niet instellen: " + ", ".join(expired))

            if mqtt_client and mqtt_client.is_connected():
                publish_settings_state(mqtt_client, force=bool(confirmed))

    def handle_get_last_edition(self, params):
        """Replies to the controller's command poll with pending settings editions."""
        response_data = b"OK"
        content_type = "text/plain"

        with state_lock:
            if pending_edition:
                edition = dict(pending_edition)
                pending_edition.clear()
                logger.info("Delivering pending edition to controller: %s", edition)
                response_data = json.dumps(edition).encode("utf-8")
                content_type = "application/json; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response_data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response_data)

    def handle_get_device_data(self):
        """Compatibility endpoint for status queries."""
        with state_lock:
            data = json.dumps({
                "readings": readings,
                "settings": settings,
                "mode": current_mode(),
                "program": current_program(),
                "pending": pending_wanted,
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
