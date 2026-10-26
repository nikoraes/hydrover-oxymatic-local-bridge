#!/usr/bin/env python3
"""
Hydrover Oxymatic Local Bridge
-----------------------------
A local drop-in emulator and MQTT bridge for Hydrover Oxymatic pool controllers.

Intercepts outbound HTTPS/TLS telemetry sent by the Oxymatic controller's ESP
Wi-Fi module (via DNS rewrite or static router deflection), parses real-time sensor
data, publishes it to Mosquitto MQTT using Home Assistant MQTT Discovery standards,
and delivers pending mode/setpoint commands back to the controller locally.

Eliminates all cloud dependencies (oxymaticapp.hydrover.eu / 20.61.77.136).
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

COMMAND_TOPIC_MODE = f"homeassistant/select/{DEVICE_ID}/mode/set"
COMMAND_TOPIC_PROG = f"homeassistant/select/{DEVICE_ID}/prog/set"

# Shared state between MQTT thread and HTTP handler
state_lock = threading.Lock()
current_state = {
    "temperature": "11.0",
    "pH": "7.20",
    "redox": "300.0",
    "oxy_current": "0.00",
    "oxy_voltage": "0.00",
    "ion_current": "0.00",
    "ion_voltage": "0.00",
    "mode": "auto",
    "prog": "AUTO TEMP",
    "is_pump": "false",
    "copper": "0.10",
}
pending_editions = {}
mqtt_client = None


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


def build_discovery_payload(sensor_key, name, unit=None, device_class=None,
                             component="sensor", options=None, numeric=None, command_topic=None):
    """Construct a Home Assistant MQTT Discovery configuration dict."""
    device_info = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "Hydrover",
        "model": "Oxymatic Controller (Local)",
    }
    
    state_topic = (
        f"homeassistant/{component}/{DEVICE_ID}/{sensor_key}/state"
    )
    
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
        
    # State class measurement for chartable numeric sensors
    if component == "sensor" and (numeric is True or unit is not None):
        payload["state_class"] = "measurement"
        
    if component == "select":
        payload["options"] = options or ["auto", "man", "off"]
        payload["command_topic"] = command_topic or f"homeassistant/select/{DEVICE_ID}/{sensor_key}/set"
        
    return payload


def publish_discovery_configs(client):
    """Publish Home Assistant MQTT Auto-Discovery definitions (matching legacy oxymatic-mqtt)."""
    configs = [
        ("temperature", "Temperature", "°C", "temperature", "sensor", None, True, None),
        ("pH", "pH", None, "ph", "sensor", None, True, None),
        ("redox", "Redox", "mV", None, "sensor", None, True, None),
        ("oxy_current", "OXY Current", "A", "current", "sensor", None, True, None),
        ("oxy_voltage", "OXY Voltage", "V", "voltage", "sensor", None, True, None),
        ("ion_current", "ION Current", "A", "current", "sensor", None, True, None),
        ("ion_voltage", "ION Voltage", "V", "voltage", "sensor", None, True, None),
        ("copper", "Copper (CU)", "mg/l", None, "sensor", None, True, None),
        ("mode", "Mode", None, None, "select", ["auto", "man", "off"], False, COMMAND_TOPIC_MODE),
        ("prog", "Program", None, None, "sensor", None, False, None),
        ("pump", "Pump Running", None, "running", "binary_sensor", None, False, None),
    ]

    for sensor_key, name, unit, dev_class, comp, opts, num, cmd_top in configs:
        topic = f"homeassistant/{comp}/{DEVICE_ID}/{sensor_key}/config"
        payload = build_discovery_payload(
            sensor_key=sensor_key,
            name=name,
            unit=unit,
            device_class=dev_class,
            component=comp,
            options=opts,
            numeric=num,
            command_topic=cmd_top
        )
        client.publish(topic, json.dumps(payload), retain=True)
        logger.debug("Published discovery config for %s", sensor_key)

    logger.info("Published all Home Assistant MQTT discovery configs")


def publish_sensor_state(client, sensor_key, value, component="sensor"):
    """Publish a single sensor state update with retain=True."""
    topic = f"homeassistant/{component}/{DEVICE_ID}/{sensor_key}/state"
    # Format comma as dot for numeric values
    val_str = str(value).replace(",", ".")
    client.publish(topic, val_str, retain=True)


def on_mqtt_connect(client, userdata, flags, rc):
    """Callback for MQTT broker connection."""
    if rc == 0:
        logger.info("Connected to Mosquitto MQTT broker at %s:%d", MQTT_HOST, MQTT_PORT)
        publish_discovery_configs(client)
        client.subscribe(COMMAND_TOPIC_MODE)
        client.subscribe(COMMAND_TOPIC_PROG)
        logger.info("Subscribed to command topics: %s, %s", COMMAND_TOPIC_MODE, COMMAND_TOPIC_PROG)
    else:
        logger.error("Failed to connect to MQTT broker, return code %d", rc)


def on_mqtt_message(client, userdata, msg):
    """Handle incoming control commands from Home Assistant or Node-RED."""
    topic = msg.topic
    payload = msg.payload.decode("utf-8").strip()
    logger.info("MQTT Command received on %s: %s", topic, payload)

    with state_lock:
        if topic == COMMAND_TOPIC_MODE:
            normalized_mode = payload.lower()
            if normalized_mode in ("auto", "automatic"):
                current_state["mode"] = "auto"
                edition_cmd = {"Mode": "Auto", "mode": "auto", "OxyMode": "auto"}
            elif normalized_mode in ("man", "manual"):
                current_state["mode"] = "man"
                edition_cmd = {"Mode": "Manual", "mode": "man", "OxyMode": "manual"}
            elif normalized_mode in ("off", "stop"):
                current_state["mode"] = "off"
                edition_cmd = {"Mode": "Off", "mode": "off", "OxyMode": "off"}
            else:
                logger.warning("Unknown mode command: %s", payload)
                return

            # Store pending edition for controller pickup
            pending_editions["mode"] = edition_cmd
            publish_sensor_state(client, "mode", current_state["mode"], component="select")
            logger.info("Queued mode edition for controller: %s", edition_cmd)

        elif topic == COMMAND_TOPIC_PROG:
            current_state["prog"] = payload
            pending_editions["prog"] = {"Program": payload, "prog": payload}
            publish_sensor_state(client, "prog", current_state["prog"], component="sensor")
            logger.info("Queued program edition for controller: %s", payload)


class OxymaticRequestHandler(BaseHTTPRequestHandler):
    """Handles HTTP/1.0 and HTTP/1.1 requests from the Oxymatic controller."""

    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        # Redirect standard HTTP logging to our logger
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

        if not raw_message:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        try:
            # Parse the inner message fragment: "PH":"7.2","RX":"300.4",...
            payload_json = "{" + raw_message.strip().strip("{}") + "}"
            telemetry = json.loads(payload_json)
        except Exception as e:
            logger.error("Failed to parse telemetry message payload: %r (%s)", raw_message, e)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        logger.info(
            "Telemetry: pH=%s, RX=%s mV, Temp=%s °C, CU=%s, Volts=%s, Amps=%s, Pump=%s",
            telemetry.get("PH"), telemetry.get("RX"), telemetry.get("Temp"),
            telemetry.get("CU"), telemetry.get("Volts"), telemetry.get("Amps"),
            telemetry.get("IsPump")
        )

        with state_lock:
            # Update internal cache
            if "PH" in telemetry:
                current_state["pH"] = telemetry["PH"]
            if "RX" in telemetry:
                # Format Redox to 1 decimal place
                try:
                    current_state["redox"] = f"{float(telemetry['RX']):.1f}"
                except ValueError:
                    current_state["redox"] = telemetry["RX"]
            if "Temp" in telemetry:
                current_state["temperature"] = telemetry["Temp"]
            if "Amps" in telemetry:
                current_state["oxy_current"] = telemetry["Amps"]
            if "Volts" in telemetry:
                current_state["oxy_voltage"] = telemetry["Volts"]
            if "CU" in telemetry:
                current_state["copper"] = telemetry["CU"]
            if "IsPump" in telemetry:
                current_state["is_pump"] = telemetry["IsPump"]

            # Publish to MQTT if connected
            if mqtt_client and mqtt_client.is_connected():
                publish_sensor_state(mqtt_client, "pH", current_state["pH"])
                publish_sensor_state(mqtt_client, "redox", current_state["redox"])
                publish_sensor_state(mqtt_client, "temperature", current_state["temperature"])
                publish_sensor_state(mqtt_client, "oxy_current", current_state["oxy_current"])
                publish_sensor_state(mqtt_client, "oxy_voltage", current_state["oxy_voltage"])
                publish_sensor_state(mqtt_client, "ion_current", current_state["ion_current"])
                publish_sensor_state(mqtt_client, "ion_voltage", current_state["ion_voltage"])
                publish_sensor_state(mqtt_client, "copper", current_state["copper"])
                
                # Binary pump sensor (ON / OFF)
                pump_state = "ON" if str(current_state["is_pump"]).lower() == "true" else "OFF"
                publish_sensor_state(mqtt_client, "pump", pump_state, component="binary_sensor")

                # Keep select/mode and prog up to date
                publish_sensor_state(mqtt_client, "mode", current_state["mode"], component="select")
                publish_sensor_state(mqtt_client, "prog", current_state["prog"], component="sensor")

        # Reply with HTTP 200 OK
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"OK")

    def handle_get_last_edition(self, params):
        """Replies to the controller's command poll with pending setpoints/modes."""
        response_data = b"OK"
        content_type = "text/plain"

        with state_lock:
            if pending_editions:
                # Merge all pending editions into one response payload
                edition_dict = {}
                for k, v in list(pending_editions.items()):
                    edition_dict.update(v)
                pending_editions.clear()

                logger.info("Delivering pending edition to controller: %s", edition_dict)
                response_data = json.dumps(edition_dict).encode("utf-8")
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
            data = json.dumps(current_state).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


def start_mqtt():
    """Initialize and run the MQTT client loop."""
    global mqtt_client
    mqtt_client = mqtt.Client(client_id=f"oxymatic_bridge_{os.getpid()}")

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
