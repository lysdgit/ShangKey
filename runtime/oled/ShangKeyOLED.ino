/*
 * ShangKey 小屏固件 —— ESP8266(ESP-01S) + SSD1306 128x64 OLED
 *
 * 用途：把电脑端 ShangKey 输入法的实时画面（拼音串 / 候选 / 中英状态）
 *       镜像到键盘旁边的小屏上，打字时不用抬头看显示器。
 *
 * 工作方式
 * --------
 *   1) 上电后按子网广播地址发 UDP 探测包（<网段>.255:61825），
 *      电脑端应答 "IP:端口"，于是本机 IP 变了也不用重新烧固件。
 *   2) 每 POLL_MS 毫秒用 HTTP GET 拉一帧 1024 字节的原始位图
 *      (http://<电脑IP>:61824/frame.bin)。
 *   3) 这 1024 字节正是 SSD1306 的页序显存布局，直接按页写入 GDDRAM。
 *   4) 画面没变时电脑回 304，固件跳过刷屏（省电、省带宽）。
 *
 * 为什么不用 Adafruit_SSD1306 / U8g2 库
 * ------------------------------------
 * 画面在电脑端用 PIL 渲染好（微软雅黑/黑体，中文清晰），小屏只当"显示管子"，
 * 因此固件零第三方库依赖：只用 ESP8266 core 自带的 WiFi / HTTP / Wire，
 * Arduino IDE 装好 ESP8266 开发板包即可编译烧录，不用再装 U8g2。
 *
 * 接线（ESP-01S 只有 8 脚，注意挑脚）
 * ------------------------------------
 *   ESP-01S            SSD1306
 *   -------            -------
 *   3V3   ----------->  VCC
 *   GND   ----------->  GND
 *   GPIO2  ---------->  SDA      (I2C 数据)
 *   GPIO0  ---------->  SCL      (I2C 时钟)
 *
 *   注意：GPIO0 兼作启动模式选择脚，下载固件时需要拉低；
 *   下载完成后它就是普通 GPIO，接 I2C 时钟没问题。
 *   如果上电时误进下载模式（OLED 模块的 SCL 上拉电阻把它拉住了），
 *   把 SCL 改接到 GPIO4 即可（改下方 SCL_PIN 常量，重新烧录）。
 *
 *   ESP-01S 没有 3.3V 输出能力，OLED 供电请用独立 3.3V，
 *   或从开发板 3.3V 分出来（电流要够，OLED 约 20mA）。
 *
 * 编译设置：Tools -> Board -> ESP8266 Generic (1MB)
 *           Flash Size -> 1MB (1MB FS)   视实际模块而定
 * 库依赖：无（全部来自 ESP8266 core）
 */

#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <Wire.h>
#include <WiFiUDP.h>

// ---------------- 用户可调参数 ----------------
static const char* WIFI_SSID = "你的WiFi名字";
static const char* WIFI_PASS = "你的WiFi密码";

// 固定电脑 IP（可选）。填了就不做广播发现，直接连这个地址；
// 留空则用 UDP 广播自动发现，推荐留空。
static const char* FIXED_PC_IP = "";     // 例如 "192.168.2.201"

static const uint16_t OLED_I2C_ADDR = 0x3C;
static const uint16_t DISCOVERY_PORT = 61825;
static const uint16_t HTTP_PORT      = 61824;
static const uint8_t  SDA_PIN        = 2;   // GPIO2
static const uint8_t  SCL_PIN        = 0;   // GPIO0

static const uint16_t POLL_MS  = 200;   // 拉帧间隔
static const uint16_t WIDTH    = 128;
static const uint16_t HEIGHT   = 64;
static const uint16_t PAGES    = HEIGHT / 8;
static const uint16_t FRAME_LEN = WIDTH * HEIGHT / 8;  // 1024

// --------------------------------------------

static uint8_t frameBuf[FRAME_LEN];
static WiFiUDP udp;
static String pcHost;        // 电脑 IP（不含端口）
static String lastEtag;      // 上一帧的 ETag，用于判断是否需要重刷
static uint32_t lastPoll = 0;
static uint32_t lastDiscovery = 0;
static bool    pcFound = false;

// ---------- SSD1306 底层（自己实现，省一个库） ----------

static void oledCmd(uint8_t c) {
  Wire.beginTransmission(OLED_I2C_ADDR);
  Wire.write(0x00);       // Co=0, D/C#=0 -> 命令
  Wire.write(c);
  Wire.endTransmission();
}

static void oledInit() {
  Wire.begin(SDA_PIN, SCL_PIN);
  // ESP8266 的 I2C 是软件模拟的（GPIO 上手写时序），
  // 400kHz 在自选引脚上不稳，100kHz 才可靠。
  Wire.setClock(100000);
  Wire.beginTransmission(OLED_I2C_ADDR);
  Wire.write(0x00);
  static const uint8_t initSeq[] = {
    0xAE,             // display off
    0xD5, 0x80,       // clock divide
    0xA8, 0x3F,       // multiplex = 64
    0xD3, 0x00,       // display offset
    0x40,             // start line 0
    0x8D, 0x14,       // charge pump on
    0x20, 0x00,       // horizontal addressing
    0xA1,             // segment remap
    0xC8,             // COM scan direction
    0xDA, 0x12,       // COM pins
    0x81, 0xCF,       // contrast
    0xD9, 0xF1,       // pre-charge
    0xDB, 0x40,       // VCOMH deselect
    0xA4,             // resume from RAM
    0xA6,             // normal (not inverted)
    0xAF              // display on
  };
  Wire.write(initSeq, sizeof(initSeq));
  Wire.endTransmission();
  delay(50);
}

// 把 1024 字节页序显存按页推给屏幕
static void oledFlush() {
  for (uint8_t p = 0; p < PAGES; p++) {
    oledCmd(0xB0 | p);        // set page
    oledCmd(0x00);            // column low  = 0
    oledCmd(0x10);            // column high = 0
    Wire.beginTransmission(OLED_I2C_ADDR);
    Wire.write(0x40);         // Co=0, D/C#=1 -> 数据
    Wire.write(&frameBuf[p * WIDTH], WIDTH);
    Wire.endTransmission();
  }
}

// 全屏清零（未拿到帧时先显示空白，避免显示随机显存内容）
static void oledClear() {
  memset(frameBuf, 0, FRAME_LEN);
  oledFlush();
}

// ---------- UDP 广播发现 ----------

// 算出本子网的广播地址：IP | ~子网掩码
// 不能直接用 255.255.255.255 —— ESP8266 官方 issue #1252：
// 新版 SDK 只接受「网络部分与本机 IP 一致」的定向广播，
// 255.255.255.255 发出去对端收不到。
static IPAddress subnetBroadcast() {
  IPAddress ip = WiFi.localIP();
  IPAddress mask = WiFi.subnetMask();
  IPAddress bcast(255, 255, 255, 255);
  if (mask == IPAddress(0, 0, 0, 0)) {
    // 拿不到掩码（少见），退回受限广播
    return bcast;
  }
  for (int i = 0; i < 4; i++) {
    bcast[i] = ip[i] | (uint8_t)(~mask[i]);
  }
  return bcast;
}

static void sendDiscovery() {
  uint8_t probe[4] = {'S', 'K', 'Y', '1'};
  IPAddress bcast = subnetBroadcast();
  udp.beginPacket(bcast, DISCOVERY_PORT);
  udp.write(probe, sizeof(probe));
  udp.endPacket();
}

// 收电脑应答 "IP:端口"
static void handleDiscovery(uint32_t now) {
  int size = udp.parsePacket();
  if (size > 0) {
    char buf[64];
    int n = udp.read(buf, sizeof(buf) - 1);
    if (n > 0) {
      buf[n] = '\0';
      String reply(buf);
      int colon = reply.indexOf(':');
      String ip = (colon > 0) ? reply.substring(0, colon) : reply;
      if (ip.length() > 0 && ip != pcHost) {
        pcHost = ip;
        pcFound = true;
        Serial.printf("[oled] 发现电脑: %s\n", pcHost.c_str());
      }
    }
  }
  // 每 5 秒重发一次探测，防止电脑重启后失联
  if (!pcFound || (now - lastDiscovery) > 5000) {
    lastDiscovery = now;
    sendDiscovery();
  }
}

// ---------- HTTP 拉帧 ----------

enum FetchResult { FETCH_OK, FETCH_UNCHANGED, FETCH_FAILED };

static FetchResult fetchFrame() {
  if (pcHost.length() == 0) return FETCH_FAILED;
  String url = "http://" + pcHost + ":" + String(HTTP_PORT) + "/frame.bin";
  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, url)) return FETCH_FAILED;
  http.setTimeout(3000);
  // 带上上一帧的 ETag：画面没变时电脑回 304，不传数据
  if (lastEtag.length() > 0) {
    http.addHeader("If-None-Match", lastEtag);
  }
  int code = http.GET();
  FetchResult res = FETCH_FAILED;
  if (code == 304) {
    res = FETCH_UNCHANGED;   // 画面没变，不用刷
  } else if (code == 200) {
    int len = http.getSize();
    // getSize() 在 chunked 响应下返回 -1，此时按实际收到的字节数走
    if (len == FRAME_LEN || len == -1) {
      WiFiClient *stream = http.getStreamPtr();
      if (stream) stream->setTimeout(3000);
      uint16_t got = 0;
      uint32_t t0 = millis();
      while (got < FRAME_LEN && (millis() - t0) < 3000) {
        int avail = stream->available();
        if (avail > 0) {
          int r = stream->readBytes(&frameBuf[got], FRAME_LEN - got);
          if (r <= 0) break;
          got += r;
        } else {
          delay(1);
        }
      }
      if (got == FRAME_LEN) {
        res = FETCH_OK;
        if (http.hasHeader("ETag")) {
          lastEtag = http.getHeader("ETag");
        }
      }
    }
  }
  http.end();
  return res;
}

// ---------- 显示状态页（未连上电脑时） ----------

// 画一个会呼吸的边框：表示「活着，但在等电脑」
static void oledShowWaiting(uint32_t now) {
  // 每 500ms 翻转一次明暗
  bool on = ((now / 500) % 2) == 0;
  memset(frameBuf, 0, FRAME_LEN);
  if (!on) {
    oledFlush();
    return;
  }
  // 上下边：page0 的 bit0（第 0 行）、page7 的 bit7（第 63 行）
  for (uint16_t x = 0; x < WIDTH; x++) {
    frameBuf[x] |= 0x01;                              // 第 0 行
    frameBuf[(PAGES - 1) * WIDTH + x] |= 0x80;        // 第 63 行
  }
  // 左右边：同一列的 8 个像素分布在 8 个 page 的同一 bit 上
  // 页 p 的第 b 行 = byte[p*WIDTH + col] 的 bit b，
  // 所以一整列需要每个 page 都置 0xFF
  for (uint8_t p = 0; p < PAGES; p++) {
    frameBuf[p * WIDTH] |= 0xFF;                              // 左列
    frameBuf[p * WIDTH + WIDTH - 1] |= 0xFF;                  // 右列
  }
  oledFlush();
}

// ---------------- 主流程 ----------------

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("[oled] ShangKey 小屏固件启动");

  oledInit();
  oledClear();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[oled] 连接 WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.printf("\n[oled] WiFi 已连接，IP=%s\n", WiFi.localIP().toString().c_str());

  udp.begin(DISCOVERY_PORT);

  if (FIXED_PC_IP[0] != '\0') {
    pcHost = FIXED_PC_IP;
    pcFound = true;
    Serial.printf("[oled] 使用固定电脑地址: %s\n", pcHost.c_str());
  } else {
    sendDiscovery();
    lastDiscovery = millis();
  }
}

void loop() {
  uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    oledShowWaiting(now);
    delay(1000);
    return;
  }

  if (!pcFound) {
    handleDiscovery(now);
    oledShowWaiting(now);
    delay(50);
    return;
  }

  // 已发现电脑：定期重发探测，防止电脑 IP 变化
  if ((now - lastDiscovery) > 5000) {
    lastDiscovery = now;
    sendDiscovery();
  }

  if ((now - lastPoll) >= POLL_MS) {
    lastPoll = now;
    FetchResult r = fetchFrame();
    if (r == FETCH_OK) {
      oledFlush();
    } else if (r == FETCH_FAILED) {
      // 取不到帧：多半是电脑服务没开或防火墙拦了
      // 重新广播一次，等电脑端服务恢复后自动接上
      pcFound = false;
      lastEtag = "";
      Serial.println("[oled] 取帧失败，重新发现电脑");
    }
    // FETCH_UNCHANGED：画面没变，304 空体，跳过刷屏（省电）
  }

  delay(20);
}
