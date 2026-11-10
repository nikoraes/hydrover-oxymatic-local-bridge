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
        ("cell", "Zout laag", None, "problem", "binary_sensor", False),
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
        if sensor_key == "cell":
            # Without an explicit object_id HA derives the entity id from the
            # (localised) name and area, e.g.
            # binary_sensor.tuinhuis_oxymatic_electrolyse_cel.
            object_id = f"{DEVICE_ID}_cell"
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

    # Retire entities the bridge used to publish but cannot honour: the
    # controller does not accept remote setting changes.
    for stale in (
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

    # Electrode health. Only meaningful while the cell is actually producing:
    # the controller keeps the cell off (temperature protection, ~15 °C floor),
    # and an idle cell reads a float voltage around 10.6 V with ~0 A. That is
    # not a fault, so the rule only arms once current is flowing.
    try:
        volts = float(str(readings["oxy_voltage"]).replace(",", "."))
        amps = float(str(readings["oxy_current"]).replace(",", "."))
    except (TypeError, ValueError):
        volts = amps = 0.0
    cell_problem = "ON" if (amps >= 1.0 and volts >= 9.5) else "OFF"
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


def publish_settings_state(client, force=False):
    """Publish the mode and program reported by the controller itself."""
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
                publish_settings_state(mqtt_client, force=changed)

    def handle_get_last_edition(self, params):
        """Reply to the controller's edition poll.

        The controller applies nothing we return (see module docstring), so
        there is no edition to hand out: answer a plain OK.
        """
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def handle_get_device_data(self):
        """Compatibility endpoint for status queries."""
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
