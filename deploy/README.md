# Deploying to your Ubuntu 24.04 / Proxmox VM

None of the steps below are executed by this build -- this is documentation
for you to run manually on your own VM, which this build environment cannot
reach.

## 1. Install Mosquitto

```bash
sudo apt update
sudo apt install mosquitto mosquitto-clients
```

Apply the minimal secure config in `deploy/mosquitto/mosquitto.conf` (copy it
into `/etc/mosquitto/conf.d/aprs.conf`), then create the password file and
restart:

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd <username>
sudo systemctl restart mosquitto
```

## 2. Install Python and the app

```bash
sudo apt install python3 python3-venv
sudo mkdir -p /opt/aprs-web
sudo chown $USER:$USER /opt/aprs-web
# copy this project's files into /opt/aprs-web (e.g. via git clone or scp)
cd /opt/aprs-web
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 3. Configure

```bash
cp .env.example .env
cp config.yaml.example config.yaml
# edit .env: MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TOPIC_PREFIX
# edit config.yaml: HISTORY_LOOKBACK_HOURS, DB_PATH, LOG_LEVEL, HTTP_HOST, HTTP_PORT if needed
```

## 4. Install and start the systemd service

```bash
sudo cp deploy/systemd/aprs-web.service /etc/systemd/system/
# create a dedicated non-root user if "aprsweb" doesn't already exist:
sudo useradd -r -s /usr/sbin/nologin aprsweb
sudo chown -R aprsweb:aprsweb /opt/aprs-web
sudo systemctl daemon-reload
sudo systemctl enable --now aprs-web
sudo systemctl status aprs-web
```

The app will be reachable at `http://<vm-host>:8000/`.

## Hosting question: VPS/VM vs. static/serverless hosting

This app needs a host that keeps one persistent process running: a background
MQTT client holding a long-lived outbound connection to your broker, an ASGI
server (uvicorn) serving REST + static files, and a WebSocket server pushing
live updates to connected browsers. That fits a VPS or VM you control -- like
your own Ubuntu/Proxmox VM -- and does **not** fit typical static-site hosting
or serverless/FaaS platforms, which don't keep a process running continuously
or accept long-lived inbound WebSocket connections.

## Direwolf -> MQTT bridge (not implemented)

The Raspberry Pi running Direwolf has no native MQTT output. The recommended
default (documented, not implemented in this build) is a small standalone
script, `bridge/direwolf_bridge.py`, that connects to Direwolf's AGW TCP port
(default 8000) or consumes its APRS-IS igate output, decodes the AX.25/TNC2
frame, and republishes it verbatim as a TNC2 string on the same
`<prefix>/<CALLSIGN>` MQTT topic the LoRa iGate firmware uses -- so the core
app treats Direwolf-sourced and LoRa-iGate-sourced packets identically. This
is a separate, later task pending your confirmation.
