#include "BluetoothSerial.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <IRremote.h>
#include <ArduinoJson.h>
#include <Arduino.h>

#define RECV_PIN 23
#define RAW_BUFFER_SIZE 1200
#define BUZZER_PIN 2
uint16_t myArray[1200];  // Holds up to 1200 elements
int arrayLength = 0;
//power
const int whitePin = 36;
//bluetooth
const int redPin1 = 21;
const int greenPin1 = 22;
const int bluePin1 = 15;
//wifi
const int redPin2 = 12;
const int greenPin2 = 13;
const int bluePin2 = 14;
//emitter
const int yellowPin = 39;

uint16_t rawMicro[RAW_BUFFER_SIZE];
uint16_t rawDataLength = 0;

BluetoothSerial SerialBT;
String receivedData = "";
String data1 = "", data2 = "";
bool dataReceived = false;

const char* serverUrl = "http://ruissmarthome.pythonanywhere.com";

void conn_bluetooth() {
  Serial.println("entered conn blue");
  Serial.println("Bluetooth Started. Waiting for data...");
  if (SerialBT.hasClient()) {
    setColor1(0, 0, 255);  //blue
  } else {
    return;
  }
  if (SerialBT.available()) {
    Serial.println("entered bt.avail");
    char incomingChar = SerialBT.read();
    if (incomingChar == '\n') {
      int separatorIndex = receivedData.indexOf('#');
      Serial.print("sep index#: ");
      Serial.println(separatorIndex);
      if (separatorIndex != -1) {
        data1 = receivedData.substring(0, separatorIndex);
        data2 = receivedData.substring(separatorIndex + 1);
        dataReceived = true;
        data1.trim();
        data2.trim();
        Serial.println("Data1: " + data1);
        Serial.println("Data2: " + data2);
      }
      receivedData = "";
    } else {
      receivedData += incomingChar;
    }
  }
}

void conn_wifi() {

  Serial.print("Connecting to WiFi...");
  WiFi.begin(data1.c_str(), data2.c_str());

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi connected.");
}
void setColor1(int red, int green, int blue) {
  ledcWrite(0, red);
  ledcWrite(1, green);
  ledcWrite(2, blue);
}
void setColor2(int red, int green, int blue) {
  ledcWrite(3, red);
  ledcWrite(4, green);
  ledcWrite(5, blue);
}

void setup() {

  pinMode(BUZZER_PIN, OUTPUT);
  ledcAttachPin(redPin1, 0);
  ledcAttachPin(greenPin1, 1);
  ledcAttachPin(bluePin1, 2);

  ledcAttachPin(redPin2, 3);
  ledcAttachPin(greenPin2, 4);
  ledcAttachPin(bluePin2, 5);

  ledcAttachPin(whitePin, 6);
  ledcAttachPin(yellowPin, 7);

  ledcSetup(0, 5000, 8);  // Channel 0, 5 kHz, 8-bit resolution
  ledcSetup(1, 5000, 8);
  ledcSetup(2, 5000, 8);
  ledcSetup(3, 5000, 8);
  ledcSetup(4, 5000, 8);  // Channel 0, 5 kHz, 8-bit resolution
  ledcSetup(5, 5000, 8);
  ledcSetup(6, 5000, 8);
  ledcSetup(7, 5000, 8);

  //glow power_led
  ledcWrite(6, 255);
  digitalWrite(BUZZER_PIN, HIGH);
  delay(750);
  digitalWrite(BUZZER_PIN, LOW);  // Turn buzzer ON
  Serial.begin(115200);
  SerialBT.begin("Ruis Smart Home 2.0");
  //glow bluetooth rgb1 to yellow
  setColor1(255, 100, 0);

  while (!dataReceived) {
    conn_bluetooth();
    delay(100);
  }

  if (dataReceived) {
    Serial.println("Disabling Bluetooth...");
    SerialBT.end();
    btStop();  // Fully stop Bluetooth controller
    //bluetooth led off 3 red burst
    for (int i = 1; i <= 3; i++) {
      setColor1(255, 0, 0);
      delay(200);
      setColor1(0, 0, 0);
      delay(200);
    }
    dataReceived = false;

    WiFi.setAutoReconnect(true);
    while (WiFi.status() != WL_CONNECTED) {
      conn_wifi();
      delay(1000);
    }
    //wifi to yellow
    setColor2(255, 100, 0);
    //Serial.println(WiFi.localIP());

    // Initialize IR Receiver (only once)
    IrReceiver.begin(RECV_PIN, ENABLE_LED_FEEDBACK);
  }
}

void loop() {
  if (WiFi.status() != WL_CONNECTED && dataReceived) {
    Serial.println("WiFi disconnected. Reconnecting...");
    WiFi.disconnect();
    WiFi.begin(data1.c_str(), data2.c_str());
  }

  HTTPClient http;
  char buff[100];
  snprintf(buff, sizeof(buff), "%s/esp_check_nxt_sig/78", serverUrl);
  Serial.println(buff);

  http.begin(buff);
  int httpResponseCode = http.GET();

  if (httpResponseCode > 0) {
    Serial.print("HTTP Response code: ");
    Serial.println(httpResponseCode);

    String payload = http.getString();
    Serial.println("Received string:");
    Serial.println(payload);
    http.end();
    Serial.println(payload.substring(2));
    Serial.println(payload.charAt(0));
    if (payload.substring(2).startsWith("recv")) {
      Serial.println("Ready to capture signal.waiting for 30s enter your key");
      delay(30000);
      if (IrReceiver.decode()) {
        rawDataLength = 0;

        for (uint16_t i = 1; i < IrReceiver.decodedIRData.rawDataPtr->rawlen && rawDataLength < RAW_BUFFER_SIZE; i++) {
          rawMicro[rawDataLength++] = IrReceiver.decodedIRData.rawDataPtr->rawbuf[i] * USECPERTICK;
        }

        IrReceiver.resume();

        DynamicJsonDocument doc(15360);  // ~15KB buffer
        doc["btn_no"] = payload.charAt(0) - '0';
        JsonArray arr = doc.createNestedArray("raw");

        for (uint16_t i = 0; i < rawDataLength; i++) {
          arr.add(rawMicro[i]);
          Serial.print(rawMicro[i]);
          Serial.print(", ");
        }

        //hex view start
        if (!IrReceiver.decodedIRData.flags & IRDATA_FLAGS_IS_REPEAT) {
          Serial.println();
          Serial.print("Protocol : ");
          Serial.println(IrReceiver.getProtocolString());

          Serial.print("Address  : 0x");
          Serial.println(IrReceiver.decodedIRData.address, HEX);

          Serial.print("Command  : 0x");
          Serial.println(IrReceiver.decodedIRData.command, HEX);

          Serial.print("Decoded HEX : 0x");
          Serial.println(IrReceiver.decodedIRData.decodedRawData, HEX);
        }
        delay(6000);
        //hex view end
        String output;
        serializeJson(doc, output);

        HTTPClient http2;
        char buff2[100];
        snprintf(buff2, sizeof(buff2), "%s/esp_sends_ir_data", serverUrl);
        Serial.println(buff2);

        http2.begin(buff2);
        http2.addHeader("Content-Type", "application/json");
        Serial.println(output);
        int responseCode = http2.POST(output);
        if (responseCode > 0) {
          Serial.print("Server response: ");
          Serial.println(responseCode);
          Serial.println(http2.getString());
        } else {
          Serial.print("Error sending: ");
          Serial.println(responseCode);
        }

        http2.end();
      }
    }
    //added later
    else {
      HTTPClient http3;
      char buff3[100];
      char btn_no=payload.charAt(0);
      snprintf(buff3, sizeof(buff3), "%s/esp_req_ir_data/%c", serverUrl, btn_no);

      //snprintf(buff3, sizeof(buff3), btn_no, buff3);
      Serial.println(buff3);
      http3.begin(buff3);
      int httpCode = http3.GET();

      if (httpCode == 200) {
        String response = http3.getString();
        response.trim();
        response = response.substring(1, response.length() - 1);
        Serial.print("Received JSON payload: >>");
        Serial.print(response);
        Serial.println("<<");

        Serial.print("First char: ");
        Serial.println(response[0]);
        Serial.print("ASCII: ");
        Serial.println((int)response[0]);
        delay(6000);
        // For up to 1200 uint16_t values, this is safe
        //const size_t capacity = JSON_ARRAY_SIZE(1200) + 1024;
        DynamicJsonDocument doc(16*1024);//16kb
        Serial.print("arrayLength1: ");
        Serial.println(arrayLength);
        DeserializationError error = deserializeJson(doc, response);

        Serial.println(doc.size());
        if (!error) {
          Serial.print(doc[2].as<uint16_t>());
          Serial.print(doc[3].as<uint16_t>());
          Serial.println(doc[4].as<uint16_t>());
          arrayLength = doc.size();
          Serial.print("arrayLength2: ");
          Serial.println(arrayLength);
          if (arrayLength > 1200) arrayLength = 1200;

          for (int i = 0; i < arrayLength; i++) {
            myArray[i] = doc[i].as<uint16_t>();
          }

          Serial.println("Parsed uint16_t array:");
          for (int i = 0; i < arrayLength; i++) {
            Serial.print(myArray[i]);
            Serial.print(", ");
          }
          Serial.print("\n");
          IrSender.begin(4);
          IrSender.sendRaw(myArray, sizeof(myArray) / sizeof(myArray[0]), 38);
          Serial.println("sent.");
        } else {
          Serial.print("Failed to parse JSON: ");
          Serial.println(error.c_str());
        }
      } else {
        Serial.print("HTTP GET failed, error code: ");
        Serial.println(httpCode);
      }

      http3.end();
    }

  } else {
    Serial.print("Error on HTTP request: ");
    Serial.println(httpResponseCode);
    http.end();
  }

  delay(1000);
}
