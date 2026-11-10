# Hydrover Oxymatic Local MQTT Bridge

A local, cloud-free emulator and MQTT bridge for the **Hydrover Oxymatic** pool controller (`ESP8266`/`ESP32` Wi-Fi module).

This project completely eliminates the dependency on the unreliable Hydrover cloud service (`http://20.61.77.136/` / `oxymaticapp.hydrover.eu`) and its fragile web scraping integration. It provides **real-time 16-second local telemetry** directly over your LAN and accepts local control commands, with **100% backward-compatibility** with the MQTT schema of `nikoraes/hydrover-oxymatic-mqtt` (no changes needed in Home Assistant or Node-RED).

---

## Background & Reverse Engineering

Hydrover Oxymatic controllers communicate with the cloud via an internal ESP8266/ESP32 module communicating over UART with the main motherboard MCU.

### The Problem
Previously, integrations monitored the controller by scraping the user-facing web portal (`oxymaticapp.hydrover.eu/devices/DeviceStatus?id=...`). This frequently failed with:
> *"Connection error / Check device connection"*

Packet inspection revealed why: Hydrover's cloud backend periodically rejected the controller's hardware authentication token with `401 Unauthorized`. Whenever this happened, the controller halted its transmission loop and displayed a connection error.

### The Wire Protocol
When routed locally, packet analysis of the controller's outbound communications revealed:

1. **Transport:** HTTPS (TLSv1.2, `ECDHE-RSA-AES256-GCM-SHA384`) on **port 443**.
2. **Server Name (SNI):** `oxymaticapp.hydrover.eu`.
3. **Certificate Validation:** **Disabled in firmware.** The ESP accepts self-signed certificates without CA validation or certificate pinning (`WiFiClientSecure.setInsecure()`).
4. **Telemetry Uplink (every ~16 seconds):**
   ```http
   GET /rest/SetDeviceData?token=<DEVICE_TOKEN>&device=Oxymatic&from=readings&guid=<DEVICE_GUID>&message="PH":"7.2","RX":"300.437000","CL":"0.00","CU":"0.10","Volts":"10.63","Amps":"0.02","IsPump":"false","Temp":"10.8","PHAlert":"1","RXAlert":"3","CUAlert":"4","CLAlert":"4","PHDepoAlert":"false","RXDepoAlert":"false","PERDepoAlert":"false","IonPerMode":"ION","TempAlert":"3","IsOxyLed2":"false","IsOxyLed1":"false","IsIonLed2":"false","IsIonLed1":"false","IonState":"0","OxyState":"0" HTTP/1.0
   Connection: close
   ```
5. **Command Polling (every ~16 seconds):**
   ```http
   GET /rest/GetLastEdition?from=device&device=Oxymatic&guid=<DEVICE_GUID>&token=<DEVICE_TOKEN> HTTP/1.0
   Connection: close
   ```
6. **Telemetry Fields Decoded:**
   | Field | Meaning | MQTT Topic |
   |---|---|---|
   | `PH` | Water pH reading | `homeassistant/sensor/oxymatic/pH/state` |
   | `RX` | Redox / ORP (mV) | `homeassistant/sensor/oxymatic/redox/state` |
   | `Temp` | Water Temperature (°C) | `homeassistant/sensor/oxymatic/temperature/state` |
   | `Amps` | Hydrolysis (OXY) current (A) | `homeassistant/sensor/oxymatic/oxy_current/state` |
   | `Volts` | Hydrolysis (OXY) voltage (V) | `homeassistant/sensor/oxymatic/oxy_voltage/state` |
   | `IonAmps` | Ionization (ION) current (A) | `homeassistant/sensor/oxymatic/ion_current/state` |
   | `IonVolts` | Ionization (ION) voltage (V) | `homeassistant/sensor/oxymatic/ion_voltage/state` |
   | `CU` | Copper ionization level (mg/L) | `homeassistant/sensor/oxymatic/copper/state` |
   | `IsPump` | Controller filtration pump relay | `homeassistant/binary_sensor/oxymatic/pump/state` |
   | `OxyState` | Hydrolysis operating state | Published via mode |
   | `IonState` | Ionization operating state | Published via mode |

7. **Settings Uplink (every ~16 seconds, alternating with the readings):**
   ```http
   GET /rest/SetDeviceData?from=device&message="IsAuto":"true","IsMan":"false",...,"Program":"11",... HTTP/1.0
   ```
   The same `/rest/SetDeviceData` endpoint carries a **second message type**: the
   controller's complete settings dump. It is the only place where the controller states
   which mode and which **program** it is actually running, so it is the authoritative
   feedback channel. Selected fields:

   | Field | Meaning |
   |---|---|
   | `IsAuto` / `IsMan` / `IsOff` | Mode position (exactly one is `true`) |
   | `Program` | Active program, a **1-based index** (see table below) |
   | `PHOn` / `PH0` / `PH100` | pH dosing enabled, band 0 %…100 % (e.g. 7.2 → 8.0) |
   | `RXOn` / `RX0` / `RX100` | Redox dosing band |
   | `TempMin` / `TempMax` | Program temperature window |
   | `OxyCurr` / `IonCurr` / `OxyPolTime` / `IonPolTime` | Electrode current limits and polarity cycle |
   | `Model` / `Version` / `PhCal` / `RxCal` | Hardware model, firmware, calibration dates |
   | `RemoteControl` | Whether the controller accepts remote editions |

   > The uplink has no `from=` collision: `readings` and `device` are distinct
   > messages on the same path. Parsing only the readings and ignoring `device` is what
   > made the old bridge publish invented mode/program values.

8. **Editions (cloud → controller): not usable.** `/rest/GetLastEdition` looks like the
   cloud→controller command channel, but on firmware 1.0.4c the reply is ignored. Tested
   live, one candidate at a time, checking the next settings dump and watching for any
   follow-up request:

   | Reply body | Channel | Result |
   |---|---|---|
   | `"Program":"4"` (bare fragment) | GetLastEdition | ignored |
   | `{"Program":"4"}` | GetLastEdition | ignored |
   | `Program=4` | GetLastEdition | ignored |
   | `{"prog":"4"}` | GetLastEdition | ignored |
   | `1` (edition id) | GetLastEdition | ignored, no follow-up request |
   | `{"TempMax":"9.0"}` | GetLastEdition | ignored |
   | full settings dump, `Program` changed | GetLastEdition | ignored |
   | full settings dump, `Program` changed | GetLastEdition | ignored |
   | full settings dump, `IsAuto`/`IsMan` swapped | GetLastEdition | ignored |
   | `"TempMax":"9.0"` | SetDeviceData `from=readings` | ignored |
   | `"TempMax":"9.0"` / `{"TempMax":"9.0"}` | SetDeviceData `from=device` | ignored |

   The third-party cloud integration for the same hardware
   ([tst-038/ha-oxymatic-smart](https://github.com/tst-038/ha-oxymatic-smart)) exposes
   program and mode as **read-only sensors** and only switches the manual outputs, which
   matches: the cloud reads this controller, it does not program it.

   So the bridge is **read-only**: it reports the mode and program the controller is
   running, and does not offer controls it cannot honour.

9. **Program table (firmware codes).** From the Hydrover OXYMATIC smart manual §8.2,
   table for systems with copper (the `SMARTPlus80 PH CU` hardware):

   | Code | Program | Oxy runtime | Ion |
   |---|---|---|---|
   | 1 | AUTO TEMP | depends on water temperature | – |
   | 2 | AUTO ORP | depends on ORP | – |
   | 3 | USER | user configured | – |
   | 4 | WINTER | 4:50 h | 10 min |
   | 5 | SPRING/AUTUMN | 7:45 h | 10 min |
   | 6 | SUMMER 20 | 10 h | 10 min |
   | 7 | SUMMER 25 | 12 h | 15 min |
   | 8 | SUMMER 28 | 14 h | 20 min |
   | 9 | SUMMER 32 | 16 h | 30 min |
   | 10 | SUMMER >32 | on request | – |
   | 11 | PUBLIC POOL | 24 h | 20 min |
   | 12 | FLOW DETECTION | depends on flow | – |

   **AUTO TEMP only treats inside the `TempMin`…`TempMax` window** (3.0–11.0 °C on this
   installation), which is why a cold pool under AUTO TEMP stops dosing pH/ORP entirely.
   Set the program with the **PROG** button on the controller; it persists.

10. **Electrode health.** Per the same manual, the cell voltage must stay **below 9.5 V**
    (amps 6–12 A). At or above 9.5 V the water lacks salinity and production collapses to
    ~0 A. The bridge exposes `binary_sensor.oxymatic_cell` (`ON` = problem) from `Volts`.

---

## Network Architecture & Traffic Redirection

The bridge acts as a local emulator of `oxymaticapp.hydrover.eu`. Because the controller targets `20.61.77.136:443`, you must deflect traffic for that IP to the host running this bridge.

### Option A: Static IPv4 Route on Router (Recommended)
Add a static route in your home router (e.g. FRITZ!Box, UniFi Dream Machine, pfSense, OPNsense):
- **IPv4 Network:** `20.61.77.136`
- **Subnet Mask:** `255.255.255.255` (or `/32`)
- **Gateway:** `<IP-of-your-server/Kubernetes-node>` (e.g. `192.168.1.130`)

### Option B: Local DNS Rewrite (Pi-hole / AdGuard Home / CoreDNS)
If your DHCP server supplies a local DNS server, rewrite:
`oxymaticapp.hydrover.eu` $\rightarrow$ `<IP-of-your-server>`

---

## Home Assistant & Node-RED Compatibility

The bridge implements standard **Home Assistant MQTT Discovery**. On startup, it publishes discovery configurations with `retain=true` under `homeassistant/sensor/oxymatic/...`.

### Discovered Entities

| Entity ID | Component | Description |
|---|---|---|
| `sensor.oxymatic_temperature` | Sensor | Water Temperature in °C |
| `sensor.oxymatic_ph` | Sensor | Water pH (unitless measurement) |
| `sensor.oxymatic_redox` | Sensor | Redox / ORP in mV |
| `sensor.oxymatic_oxy_current` | Sensor | Oxidation Current in A |
| `sensor.oxymatic_oxy_voltage` | Sensor | Oxidation Voltage in V |
| `sensor.oxymatic_ion_current` | Sensor | Ionization Current in A |
| `sensor.oxymatic_ion_voltage` | Sensor | Ionization Voltage in V |
| `sensor.oxymatic_copper` | Sensor | Copper level |
| `sensor.oxymatic_program` | Sensor | Active program, from the controller's `Program` code (attributes: code, model, firmware, mode) |
| `sensor.oxymatic_mode` | Sensor | Operating position, from the controller's `IsAuto`/`IsMan`/`IsOff` flags |
| `binary_sensor.oxymatic_cell` | Binary Sensor | Cell voltage problem (`ON` when `Volts` ≥ 9.5 V: add salt) |
| `binary_sensor.oxymatic_pump` | Binary Sensor | Physical pump relay feedback (`ON`/`OFF`) |

### No write entities

There are deliberately **no `select`/`switch` entities** for mode or program. Both are set
on the controller (mode buttons, `PROG` button) and the bridge only reports them; see
"Editions (cloud → controller): not usable" above. Earlier versions published
`select.oxymatic_mode` and `select.oxymatic_programma` — they are removed on the next start
(retained discovery configs are cleared), because a selector that cannot change anything is
worse than no selector.

---

## Configuration

Set the following environment variables:

| Variable | Description | Default |
|---|---|---|
| `DEVICE_ID` | Base identifier for HA entity IDs | `oxymatic` |
| `DEVICE_NAME` | Friendly device name in HA | `Oxymatic` |
| `MQTT_HOST` | Hostname or IP of Mosquitto broker | `mosquitto.mosquitto.svc.cluster.local` |
| `MQTT_PORT` | Port of Mosquitto broker | `8883` |
| `MQTT_TLS` | Enable TLS for MQTT connection (`true`/`false`) | `true` |
| `MQTT_USERNAME` | MQTT broker username | `""` |
| `MQTT_PASSWORD` | MQTT broker password | `""` |
| `LISTEN_PORT` | Port the HTTPS server listens on internally | `8443` |
| `CERT_FILE` | Path to TLS certificate | `/certs/tls.crt` |
| `KEY_FILE` | Path to TLS private key | `/certs/tls.key` |

*(If no certificate is found at `CERT_FILE`, a self-signed certificate for `oxymaticapp.hydrover.eu` is generated automatically on startup).*

---

## Kubernetes Deployment (home-k8s)

Manifests following home-k8s standards are in `k8s/`:

1. **Create the Secret:**
   ```bash
   kubectl create secret generic oxymatic-secrets -n oxymatic \
     --from-literal=MQTT_USERNAME='<user>' \
     --from-literal=MQTT_PASSWORD='<pass>'
   ```

2. **Apply the Service & Deployment:**
   ```bash
   kubectl apply -f k8s/
   ```

The `Service` uses `spec.externalIPs: ["20.61.77.136"]`, which programs Kubernetes `kube-proxy` to capture all traffic hitting GMKtec destined for `20.61.77.136:443` and forward it straight to the bridge pod on port `8443`.

---

## Docker Standalone

```bash
docker run -d \
  --name oxymatic-local-bridge \
  --restart unless-stopped \
  -p 443:8443 \
  -e MQTT_HOST="192.168.1.130" \
  -e MQTT_PORT="8883" \
  -e MQTT_USERNAME="nikoraes" \
  -e MQTT_PASSWORD="secretpassword" \
  ghcr.io/nikoraes/hydrover-oxymatic-local-bridge:latest
```

---

## Verification

Check container logs to verify real-time telemetry ingestion:

```bash
kubectl logs -n oxymatic deploy/oxymatic-local-bridge -f
```

Sample output:
```text
2026-10-26 12:00:00 [INFO] oxymatic-bridge: Telemetry: pH=7.2, RX=302.4 mV, Temp=10.8 °C, CU=0.10, Volts=10.63, Amps=0.02, Pump=false
2026-10-26 12:00:16 [INFO] oxymatic-bridge: Telemetry: pH=7.2, RX=302.5 mV, Temp=10.8 °C, CU=0.10, Volts=10.63, Amps=0.02, Pump=false
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.
