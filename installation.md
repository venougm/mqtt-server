# Instalasi di Linux (Ubuntu 24.04 / Proxmox VM)

Tutorial ini untuk menjalankan **LoRa APRS Live Map** secara permanen di VM
Ubuntu 24.04 kamu (Mosquitto + aplikasi web dalam satu VM). Semua perintah di
bawah dijalankan **di dalam VM Ubuntu**, bukan di laptop Windows.

Catatan: aplikasi ini butuh VPS/VM yang menjalankan proses terus-menerus
(koneksi MQTT + WebSocket), jadi **tidak bisa** dipasang di shared hosting
biasa. VM Proxmox-mu sudah tepat untuk ini.

## 0. Pindahkan kode ke VM

Pilih salah satu cara untuk memindahkan folder project (`mqtt-server/`) dari
laptop ke VM:

**Opsi A — git (disarankan jika kamu punya repo):**
```bash
git clone <url-repo-kamu> /opt/aprs-web
```

**Opsi B — scp langsung dari laptop Windows** (jalankan ini di laptop, lewat
PowerShell/WSL, ganti `user@vm-ip`):
```powershell
scp -r "c:\Users\venou\OneDrive - UGM 365\2026\aws academy\code\mqtt-server" user@vm-ip:/tmp/mqtt-server
```
lalu di VM:
```bash
sudo mv /tmp/mqtt-server /opt/aprs-web
```

Sisa tutorial ini mengasumsikan kode ada di `/opt/aprs-web`.

## 1. Install Mosquitto (MQTT broker)

```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients
```

Pasang konfigurasi minimal yang aman (mewajibkan username/password, menolak
koneksi anonim):

```bash
sudo cp /opt/aprs-web/deploy/mosquitto/mosquitto.conf /etc/mosquitto/conf.d/aprs.conf
sudo mosquitto_passwd -c /etc/mosquitto/passwd <username_pilihanmu>
sudo systemctl restart mosquitto
sudo systemctl status mosquitto
```

Simpan baik-baik username/password ini — nanti dipakai lagi di langkah 3
(`.env`) dan di WebUI iGate kamu (field MQTT Username/Password).

Opsional tapi disarankan: cek Mosquitto sudah berjalan dan bisa menerima
pesan (dari dua terminal berbeda di VM):
```bash
mosquitto_sub -h localhost -u <username> -P <password> -t 'aprs-igate/#' -v
# di terminal lain:
mosquitto_pub -h localhost -u <username> -P <password> -t 'aprs-igate/TEST-1' -m 'TEST-1>APRS:!0000.00N/00000.00E>test'
```
Kalau baris `TEST-1>APRS:...` muncul di terminal `mosquitto_sub`, broker sudah
jalan dengan benar.

## 2. Install Python dan dependensi aplikasi

```bash
sudo apt install -y python3 python3-venv
cd /opt/aprs-web
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 3. Konfigurasi aplikasi

```bash
cd /opt/aprs-web
cp .env.example .env
cp config.yaml.example config.yaml
nano .env
```

Isi `.env` dengan nilai nyata:
```ini
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
MQTT_USERNAME=<username_pilihanmu>
MQTT_PASSWORD=<password_pilihanmu>
MQTT_TOPIC_PREFIX=aprs-igate
```

- `MQTT_BROKER_HOST=localhost` karena Mosquitto berjalan di VM yang sama
  dengan aplikasi. Kalau broker ada di host lain, isi IP-nya.
- `MQTT_TOPIC_PREFIX=aprs-igate` — ini default firmware CA2RXU/richonguzman
  dan sudah dikonfirmasi sebagai topic yang kamu pakai. Harus **sama persis**
  dengan field "Topic" di WebUI iGate kamu.
- `MQTT_BROKER_HOST` dan `MQTT_TOPIC_PREFIX` wajib diisi — aplikasi akan
  menolak start kalau salah satu kosong.

`config.yaml` berisi setting non-rahasia (lookback history, log level, port
HTTP) — defaultnya biasanya sudah cukup, edit kalau perlu:
```bash
nano config.yaml
```

## 4. Jalankan sebagai service (systemd)

Supaya aplikasi otomatis jalan terus dan restart sendiri kalau VM reboot atau
crash:

```bash
# buat user khusus non-root untuk menjalankan aplikasi
sudo useradd -r -s /usr/sbin/nologin aprsweb
sudo chown -R aprsweb:aprsweb /opt/aprs-web

# pasang unit systemd
sudo cp /opt/aprs-web/deploy/systemd/aprs-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aprs-web
```

Cek statusnya:
```bash
sudo systemctl status aprs-web
```
Harus menunjukkan `active (running)`. Lihat log realtime kalau perlu
troubleshooting:
```bash
sudo journalctl -u aprs-web -f
```

## 5. Buka websitenya

Dari browser di komputer manapun yang satu jaringan dengan VM:
```
http://<IP-VM-Ubuntu>:8000/
```

Peta akan tampil kosong sampai iGate kamu benar-benar mengirim data MQTT ke
broker ini (lanjut ke langkah 6).

## 6. Hubungkan iGate CA2RXU ke broker ini

Di WebUI konfigurasi iGate (menu **Services → MQTT**):
- **Enable**: ON
- **Server**: IP VM Ubuntu kamu (sama dengan yang kamu akses di langkah 5)
- **Port**: `1883`
- **Topic**: `aprs-igate`
- **Username/Password**: sama dengan yang dibuat di langkah 1

Setelah disimpan dan iGate reconnect, station seharusnya mulai muncul di
peta secara live.

## Perintah berguna lainnya

| Tujuan | Perintah |
|---|---|
| Restart aplikasi | `sudo systemctl restart aprs-web` |
| Stop aplikasi | `sudo systemctl stop aprs-web` |
| Lihat log aplikasi | `sudo journalctl -u aprs-web -f` |
| Restart Mosquitto | `sudo systemctl restart mosquitto` |
| Tambah user MQTT lain | `sudo mosquitto_passwd /etc/mosquitto/passwd <user_baru>` |
| Update kode (setelah git pull/scp baru) | `sudo systemctl stop aprs-web && venv/bin/pip install -r requirements.txt && sudo systemctl start aprs-web` |

## Yang belum tercakup di tutorial ini

- **TLS/HTTPS** — belum dikonfigurasi (butuh sertifikat milikmu sendiri).
  Lihat komentar di `deploy/mosquitto/mosquitto.conf` untuk contoh listener
  TLS di Mosquitto; untuk HTTPS di sisi web, biasanya ditambahkan reverse
  proxy (nginx/caddy) di depan uvicorn — belum dibuatkan di proyek ini.
- **Jembatan Direwolf (Raspberry Pi) → MQTT** — Direwolf tidak native MQTT,
  jadi stasiun yang masuk lewat Direwolf belum otomatis muncul di peta ini.
  Rencana/rekomendasi pendekatannya ada di `deploy/README.md` bagian
  "Direwolf -> MQTT bridge", tapi belum diimplementasikan.
