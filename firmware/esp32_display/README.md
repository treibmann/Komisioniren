# ESP32 Pick-by-Light Display (HUB75 P5 64x32)

Firmware für die Filial-Kisten: ein ESP32-DevKitC treibt ein P5-RGB-LED-Panel
(64x32, HUB75) über das Seengreat RGB Matrix Adapter Board (E). Das Display zeigt
die vom Server gefunkte Stückzahl; `"0"` schaltet es dunkel (Posten erledigt).

## MQTT-Vertrag (vom Server vorgegeben, siehe `server.py`)
- Topic:  `baeckerei/display/<filialname klein>`  (z.B. `baeckerei/display/penny`)
- Payload: Menge als Text (`"3"`), `"0"` = Display aus

## Arduino-IDE einrichten
1. Boardverwalter-URL: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
   → Boards "esp32 by Espressif" installieren. Board wählen: **ESP32 Dev Module**.
2. Bibliotheken (Bibliotheksverwalter):
   - **ESP32-HUB75-MatrixPanel-I2S-DMA** (mrcodetastic)
   - **PubSubClient** (Nick O'Leary)

## Pro Kiste anpassen (oben im .ino)
- `ssid` / `password` – Bäckerei-WLAN
- `mqtt_server` – **LAN-IP des PCs/Brokers** = `192.168.0.180` (NICHT die Docker-IP 172.x)
- `filiale_name` – exakt wie in der PDF, klein (inkl. Umlaute/Punkte/Leerzeichen)
- `#define ADAPTER_V2` – je nach Aufdruck der Adapter-Platine (V1.x oder V2.x)

## Broker (läuft schon per Docker auf dem PC)
- `docker-compose.yml`: Mosquitto gibt Port **1883** frei
- `mosquitto.conf`: `listener 1883` + `allow_anonymous true`
- **Windows-Firewall:** Port 1883 eingehend erlauben (als Admin, einmalig):
  ```powershell
  New-NetFirewallRule -DisplayName "MQTT 1883" -Direction Inbound -LocalPort 1883 -Protocol TCP -Action Allow
  ```
- PC und ESP32 müssen im **selben WLAN/Netz** sein.

## Bildfehler beheben
- Bild verschoben/zerrissen → `mxconfig.clkphase` umschalten (true/false)
- Panel bleibt dunkel/flackert → `mxconfig.driver = HUB75_I2S_CFG::FM6126A;` aktivieren
- Falsche Farben/Pixel → Adapter-Pinvariante (V1.x/V2.x) prüfen

## Test ohne Hardware-Werkzeug
Am PC einen Wert senden (mosquitto-clients oder ein MQTT-Tool):
```
mosquitto_pub -h 192.168.0.180 -t "baeckerei/display/penny" -m "5"
mosquitto_pub -h 192.168.0.180 -t "baeckerei/display/penny" -m "0"
```
