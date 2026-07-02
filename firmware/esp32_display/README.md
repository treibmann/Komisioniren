# ESP32 Pick-by-Light Display (HUB75 P5 64x32)

Firmware für die Pick-by-Light-Displays: ein ESP32-DevKitC treibt ein P5-RGB-LED-Panel
(64x32, HUB75) über das Seengreat RGB Matrix Adapter Board (E).

**Positions-basiert:** Die Displays hängen an festen Plätzen. Die heutige
Tourenplanung ordnet den Plätzen Filialen zu (Platz 1 = 1. Filiale der Tour usw.).
Jeder ESP hat eine feste **Platznummer** und zeigt **Filialname + Menge**;
`"0"` schaltet das Display dunkel (Posten erledigt).

## MQTT-Vertrag (vom Server vorgegeben, siehe `server.py` → `mqtt_send`)
- Topic:  `baeckerei/display/<platz>`  (z.B. `baeckerei/display/1`)
- Payload: `"<Filialname>|<Menge>"` (z.B. `"Penny|5"`), `"0"` = Display aus
- Der Server rechnet die aktive Filiale in ihre Position der heutigen (vollen)
  Tour-Reihenfolge um. Fremdkunden/Verkaufsautos (nicht in der Tour) senden nicht.

## Arduino-IDE einrichten
1. Boardverwalter-URL: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
   → Boards "esp32 by Espressif" installieren. Board wählen: **ESP32 Dev Module**.
2. Bibliotheken (Bibliotheksverwalter):
   - **ESP32-HUB75-MatrixPanel-I2S-DMA** (mrcodetastic)
   - **PubSubClient** (Nick O'Leary)

## Pro Display anpassen (oben im .ino)
- `ssid` / `password` – Bäckerei-WLAN
- `mqtt_server` – **LAN-IP des PCs/Brokers** = `192.168.0.180` (NICHT die Docker-IP 172.x)
- `display_platz` – feste **Platznummer** dieses Displays (1, 2, 3, …). Bleibt fix;
  welche Filiale der Platz zeigt, entscheidet täglich die Tour (Server sendet den Namen mit).
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

## Test ohne Pack-Vorgang
Am PC einen Wert an einen Platz senden (mosquitto-clients oder ein MQTT-Tool):
```
mosquitto_pub -h 192.168.0.180 -t "baeckerei/display/1" -m "Penny|5"
mosquitto_pub -h 192.168.0.180 -t "baeckerei/display/1" -m "0"
```

## Platz ↔ Filiale
Die Zuordnung ergibt sich aus der **Tourenplanung** (Reihenfolge der Filialen für
den Wochentag). Platz *n* = *n*-te Filiale der heutigen Tour. Da der Server den
**Filialnamen mitsendet** und das Display ihn anzeigt, sieht der Packer immer,
welche Kiste gemeint ist – auch wenn sich die Zuordnung täglich ändert.
