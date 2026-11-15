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

8. **Editions (cloud → controller): this is the write channel, and it works.** The first
   attempts failed because the payloads were invented. The cloud's real reply is an
   **edition list**, and only a *newer* `DataEdition` triggers anything. Serving the
   captured list with a bumped `DataEdition` made the controller issue, once:

   ```http
   GET /rest/GetDeviceData?from=device&device=Oxymatic&guid=<GUID>&token=<TOKEN>&parameter=all
   ```

   Answer **that** with the captured document (1 874 bytes of flat JSON, `Program`, the
   three mode booleans, `LastEdition`/`LastProgEdition` as .NET `/Date(<ms>)/`) and the
   controller applies it. Proven on the unit, one value at a time, against the display:

   | Served | Display shows |
   |---|---|
   | `Program: 7` | SUMMER 20 |
   | `Program: 5` | WINTER |
   | `Program: 11` | PUBLIC POOL |
   | `IsAuto: true` | AUTO (from OFF, nobody at the keypad) |
   | `IsOff: true` | OFF |

   Corrections to what earlier versions of this file claimed:

   - **`Program` is a direct code selector**, not an index to be inferred — the document's
     value is used as-is.
   - **`GetLastEdition` replies are not ignored.** A junk body is ignored (and leaves the
     unit on FLOW DETECTION, code 12); a well-formed edition list with a newer
     `DataEdition` is acted on within ~16 s.
   - **A document is authoritative for every field it contains**, so anything you do not
     mean to change must be filled from the unit's own state — see "Write path" below.
   - The bridge's own invented `{readings,settings,mode,program}` JSON ends the exchange
     after one call and is what made the channel look read-only.

9. **Program table (firmware codes).** From the Hydrover OXYMATIC smart manual §8.2,
   table for systems with copper (the `SMARTPlus80 PH CU` hardware):

   | Code | Program | Oxy runtime | Ion | Source |
   |---|---|---|---|---|
   | 1 | AUTO TEMP | depends on water temperature | – | manual order |
   | 2 | AUTO ORP | depends on ORP | – | manual order |
   | 3 | USER | user configured | – | manual order |
   | 4 | *unknown* | – | – | not identified on the device yet |
   | 5 | WINTER | 4:50 h | 10 min | **confirmed on the display** |
   | 6 | SPRING/AUTUMN | 7:45 h | 10 min | inferred — serving 6 left the unit on FLOW DETECTION |
   | 7 | SUMMER 20 | 10 h | 10 min | **confirmed on the display** |
   | 8 | SUMMER 25 | 12 h | 15 min | inferred |
   | 9 | SUMMER 28 | 14 h | 20 min | inferred |
   | 10 | SUMMER 32 | 16 h | 30 min | inferred |
   | 11 | PUBLIC POOL | 24 h | 20 min | **observed on the device** |
   | 12 | FLOW DETECTION | depends on flow | – | **observed on the device** |

   The manual's printed table (§8.2) is **offset by one from WINTER onwards**: the firmware
   carries an extra entry between USER and WINTER, so setting WINTER on the controller
   reports `Program=5`, not 4, while PUBLIC POOL still reports 11. The firmware also has no
   selectable "SUMMER >32" entry (the manual lists it as "a consultar"). Codes whose name is
   not identified are published as `PROGRAMMA <code>` so the entity never shows a stale name.

   **AUTO TEMP only treats inside the `TempMin`…`TempMax` window** (3.0–11.0 °C on this
   installation), which is why a cold pool under AUTO TEMP stops dosing pH/ORP entirely.

   The program can be set from the **PROG** button on the controller (it persists) or from
   Home Assistant through the write path below. While the unit is **off** the programs page
   reads FLOW DETECTION (12) whatever is stored — that is normal and is not a lost write.

10. **No electrode-health entity (deliberate).** The manual says the cell voltage must stay
    below 9.5 V while producing and that above it you should add sea salt. That advice only
    holds for a classic salt/electrolysis pool: this installation is a **natural, planted,
    chlorine-free pool**, where adding salt is not an option (it harms the plants). The cell
    is also kept idle below ~15 °C by design, so an unloaded cell floats at ~10.6 V with
    ~0 A and a voltage-based verdict means nothing. `sensor.oxymatic_oxy_voltage` and
    `sensor.oxymatic_oxy_current` stay available as raw readings; a "problem" binary sensor
    for them would be a knob that lies.

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
| `binary_sensor.oxymatic_pump` | Binary Sensor | Physical pump relay feedback (`ON`/`OFF`) |

### Write entities

Two `select` entities are published alongside the sensors. They are real controls: a command
queues an edition, the controller fetches the document and adopts it.

| Entity | Command topic | Options |
|---|---|---|
| `select.oxymatic_programma` | `oxymatic/set/program` | the 11 known program names |
| `select.oxymatic_mode` | `oxymatic/set/mode` | `auto`, `man`, `off` |

The entity id gets Home Assistant's device-name prefix, so in a dashboard with a device
named *Tuinhuis Oxymatic* these appear as `select.tuinhuis_oxymatic_programma` and
`select.tuinhuis_oxymatic_modus`. The `unique_id` follows `DEVICE_ID`, not the prefix.

Plus a combined topic for a cold start: `oxymatic/set/state` with `PROGRAM:MODE`
(e.g. `WINTER:off`) or `{"program": "WINTER", "mode": "off"}`.

---

## Write path

Commands arrive over MQTT — there is **no HTTP write endpoint**. A command queues an
edition; `GetLastEdition` then advertises a fresh `DataEdition`; the controller calls
`GetDeviceData?parameter=all`; the bridge answers with `oxymatic_template.json` rebuilt as
*template + live readings + intent*, with bumped `LastEdition`/`LastProgEdition`. The
pending edition is handed over **once** and cleared, or the controller loops on
`GetDeviceData`.

```bash
# from anywhere that can publish to the broker
mosquitto_pub -t oxymatic/set/mode -m auto
mosquitto_pub -t oxymatic/set/program -m WINTER
mosquitto_pub -t oxymatic/set/state -m WINTER:off
```

`oxymatic_template.json` is the captured cloud document (shape + defaults). It must ship in
the image — the Dockerfile copies it explicitly, and a missing template logs
`no usable device template` and silently drops every edition.

### Rules that keep it honest

A served document is authoritative for **every** field it carries, which makes inheritance
the whole problem:

- **Fill unrequested fields from the unit's own state, never from the template.** A
  mode-only command built from the template also asserted the template's `Program: 11` and
  moved a WINTER pool onto PUBLIC POOL (24 h filtration, and with the pump output energised
  that is nearly all day of pumping).
- **Base the next partial write on the last document served, not on the settings dump.** The
  dump arrives at boot only, so it can be hours stale, and after a restart the select would
  advertise a stale option that silently blocks the next write (HA's `select` no-ops when
  the requested option equals its current state).
- **Refuse a partial write whose companion field is unknown.** With no intent, no served
  document and no dump, inheritance has nothing true to fall back on. `set/program` and
  `set/mode` return without queueing and log why; `set/state` is the cold-start path.
- **Log the effective values, not the intent** — `Served document: program=5 mode=auto` is
  what the unit will apply. `program=None mode=auto` in the log would hide a leaked default.

### State survives restarts

The settings dump is emitted at boot, so a new pod stores nothing and every inheritance
would fall back to the template. Both are mirrored to **retained** topics and reseeded on
connect (retained messages replay on subscribe):

| Retained topic | Content |
|---|---|
| `oxymatic/state/settings` | last settings dump (JSON) |
| `oxymatic/state/document` | last document actually served (JSON) |

The log prints the restore (`Restored the last served document: program=… mode=…`) so it is
provable rather than assumed. The settings mirror stays empty until the controller next
boots — that is the only time it dumps, so one power cycle after the first deploy seeds it;
until then the document mirror covers program/mode (not the pH/temp setpoints).

### When a write "does nothing"

1. Is the command in the log? `MQTT command on oxymatic/set/mode: 'auto'`. If not, the
   command never reached the bridge — check the select's state, which may equal the
   requested option (HA publishes nothing then), or the topic.
2. Is it followed by `Edition queued` and then `Serving edition`? If `Serving edition` never
   appears, the controller is not polling or the edition is not newer.
3. Does `Served document` show the values you expect? Compare with the display: the
   controller is the only honest witness, because it never reports its active program
   except at boot.
4. `auto` + FLOW DETECTION after a program write is a failure; `off` + FLOW DETECTION is
   normal.

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
| `OX_TEMPLATE` | Path to the document template used by the write path | `oxymatic_template.json` next to `bridge.py` |
| `REFRESH_SECONDS` | How often the mode/program sensors are republished even when unchanged | `300` |

Experiment switches, for reverse-engineering without a rebuild — all off by default:

| Variable | Effect |
|---|---|
| `FORCE_401_PATHS` | Answer the listed paths with the cloud's `401 ACCESS TOKEN IS NOT VALID` (makes the controller re-request a token) |
| `EDITION_REPLY` / `EDITION_REPLY_FILE` | Serve a fixed `GetLastEdition` body instead of `OK` |
| `DEVICEDATA_REPLY` / `DEVICEDATA_REPLY_FILE` | Serve a fixed `GetDeviceData` document |
| `REQUEST_LOG` | Append every controller request as JSONL (also `TAP_FULL` for full bodies) |

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
12:00:00 [INFO] oxymatic-bridge: Readings: pH=7.2, RX=302.400000 mV, Temp=10.8 °C, CU=0.10, Volts=10.63, Amps=0.02, Pump=false
12:00:16 [INFO] oxymatic-bridge: Readings: pH=7.2, RX=302.500000 mV, Temp=10.8 °C, CU=0.10, Volts=10.63, Amps=0.02, Pump=false
12:00:32 [INFO] oxymatic-bridge: Subscribed to command topic oxymatic/set/program
12:00:32 [INFO] oxymatic-bridge: Restored the last served document: program=5 mode=off
13:21:55 [INFO] oxymatic-bridge: MQTT command on oxymatic/set/mode: 'auto'
13:21:55 [INFO] oxymatic-bridge: Edition queued (program=None mode=auto)
13:22:06 [INFO] oxymatic-bridge: Serving edition DataEdition=1794748915 (program=None mode=auto)
13:22:10 [INFO] oxymatic-bridge: Served document: program=5 mode=auto — edition handed over
```

The last four lines are one write: command → edition → the controller fetching it →
the document that was actually applied. `program=5` there is the **effective** value
(inherited from the last served document), not the request.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
