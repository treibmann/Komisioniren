/*  Pick-by-Light Display  –  ESP32-DevKitC + Seengreat RGB Matrix Adapter (E)
 *  Panel: P5 RGB HUB75, 64x32, 1/16 Scan
 *  Zeigt die vom Server (baeckerei/display/<filiale>) gefunkte Stueckzahl gross an;
 *  "0" = Display dunkel (Posten erledigt).
 *
 *  Benoetigte Bibliotheken (Arduino IDE -> Bibliotheksverwalter):
 *    - "ESP32-HUB75-MatrixPanel-I2S-DMA"  (von mrcodetastic)
 *    - "PubSubClient"                     (von Nick O'Leary)
 *  Board: "ESP32 Dev Module"  (ESP32-WROOM-32U)
 *
 *  MQTT-Vertrag (siehe server.py):
 *    Topic  : baeckerei/display/<filialname klein>   z.B. baeckerei/display/penny
 *    Payload: Menge als Text ("3"); "0" = Display aus
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>

// ========================================================
// 1) EINSTELLUNGEN – PRO KISTE ANPASSEN
// ========================================================
const char* ssid         = "DEIN_BACKSTUBEN_WLAN_NAME";
const char* password     = "DEIN_WLAN_PASSWORT";
const char* mqtt_server  = "192.168.0.180";   // LAN-IP deines PCs/Brokers (NICHT 172.x!)
const int   mqtt_port    = 1883;

// Filialname EXAKT wie in der PDF, klein geschrieben (inkl. Umlaute/Punkte/Leerzeichen):
//   z.B. "penny", "berga", "auma", "poelzig"/"pölzig", "neustä.", "benz 4"
const char* filiale_name = "penny";
// ========================================================

// ========================================================
// 2) PIN-VARIANTE DES SEENGREAT-ADAPTERS WÄHLEN
//    Steht auf der Platine (V1.x oder V2.x). Standard hier: V2.x
// ========================================================
#define ADAPTER_V2   // <- fuer V1.x diese Zeile auskommentieren

#ifdef ADAPTER_V2
  #define R1_PIN 18
  #define G1_PIN 17
  #define B1_PIN 19
  #define R2_PIN 21
  #define G2_PIN 23
  #define B2_PIN 27
  #define A_PIN  26
  #define B_PIN  16
  #define C_PIN  25
  #define D_PIN   4
  #define E_PIN  22
  #define CLK_PIN 33
  #define LAT_PIN  2
  #define OE_PIN  32
#else  // V1.x
  #define R1_PIN 18
  #define G1_PIN 25
  #define B1_PIN  5
  #define R2_PIN 17
  #define G2_PIN 33
  #define B2_PIN 16
  #define A_PIN   4
  #define B_PIN   3
  #define C_PIN   0
  #define D_PIN  21
  #define E_PIN  32
  #define CLK_PIN 2
  #define LAT_PIN 19
  #define OE_PIN  15
#endif

// ========================================================
// 3) PANEL-GEOMETRIE
// ========================================================
#define PANEL_RES_X 64
#define PANEL_RES_Y 32
#define PANEL_CHAIN 1     // 1 Panel pro Kiste. Zwei Panels aneinander: 2 (ergibt 128x32)

MatrixPanel_I2S_DMA* dma_display = nullptr;

WiFiClient   espClient;
PubSubClient client(espClient);
String       topic_sub = "baeckerei/display/" + String(filiale_name);

// ---- Zahl gross + zentriert anzeigen (bzw. dunkel bei 0) ----
void showNumber(int n) {
  if (!dma_display) return;
  dma_display->clearScreen();
  if (n <= 0) return;                       // Posten erledigt -> Panel bleibt dunkel

  String s = String(n);
  uint8_t size = (s.length() <= 2) ? 4 : (s.length() == 3 ? 3 : 2);
  dma_display->setTextWrap(false);
  dma_display->setTextSize(size);

  int16_t x1, y1; uint16_t w, h;
  dma_display->getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  int16_t x = (PANEL_RES_X - (int)w) / 2 - x1;
  int16_t y = (PANEL_RES_Y - (int)h) / 2 - y1;

  dma_display->setTextColor(dma_display->color565(0, 255, 0));  // kraeftiges Gruen
  dma_display->setCursor(x, y);
  dma_display->print(s);
}

void setup_display() {
  HUB75_I2S_CFG::i2s_pins _pins = {
    R1_PIN, G1_PIN, B1_PIN, R2_PIN, G2_PIN, B2_PIN,
    A_PIN,  B_PIN,  C_PIN,  D_PIN,  E_PIN,
    LAT_PIN, OE_PIN, CLK_PIN
  };
  HUB75_I2S_CFG mxconfig(PANEL_RES_X, PANEL_RES_Y, PANEL_CHAIN, _pins);
  mxconfig.clkphase = false;   // bei 64x32 meist noetig; falls Bild "verschoben": auf true
  // mxconfig.driver = HUB75_I2S_CFG::FM6126A;  // NUR falls dein Panel den FM6126A-Chip hat

  dma_display = new MatrixPanel_I2S_DMA(mxconfig);
  dma_display->begin();
  dma_display->setBrightness8(160);   // 0..255
  dma_display->clearScreen();
}

void setup_wifi() {
  delay(10);
  Serial.print("\nVerbinde mit "); Serial.println(ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.print("\nWLAN verbunden, IP: "); Serial.println(WiFi.localIP());
}

void callback(char* topic, byte* payload, unsigned int length) {
  String message = "";
  for (unsigned int i = 0; i < length; i++) message += (char)payload[i];
  int stueckzahl = message.toInt();
  Serial.printf("Signal %s -> Menge: %d\n", filiale_name, stueckzahl);
  showNumber(stueckzahl);
}

void reconnect() {
  while (!client.connected()) {
    Serial.print("MQTT verbinden...");
    String clientId = "ESP32-" + String(filiale_name);
    if (client.connect(clientId.c_str())) {
      Serial.println("ok");
      client.subscribe(topic_sub.c_str());
      Serial.print("Abonniert: "); Serial.println(topic_sub);
    } else {
      Serial.printf("Fehler rc=%d, neuer Versuch in 5s\n", client.state());
      delay(5000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  setup_display();       // Display zuerst -> dunkel/bereit
  setup_wifi();
  client.setServer(mqtt_server, mqtt_port);
  client.setCallback(callback);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) setup_wifi();
  if (!client.connected()) reconnect();
  client.loop();
}
