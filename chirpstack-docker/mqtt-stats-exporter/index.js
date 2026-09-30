import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import mqtt from "mqtt";
import client from "prom-client";

const MQTT_URL = process.env.MQTT_URL || "mqtt://mosquitto:1883";
const STATS_TOPIC = process.env.STATS_TOPIC || "+/gateway/+/event/stats";
const UPLINK_TOPIC = process.env.UPLINK_TOPIC || "application/+/device/+/event/up";
const LISTEN_PORT = Number(process.env.LISTEN_PORT || 9200);
const CSV_PATH = process.env.CSV_PATH || "/data/uplinks.csv";
// A device counts toward the Pure ALOHA collision estimate's N if it has sent
// an uplink within this many seconds - i.e. "currently contending for the channel",
// not just "registered". Tune this if devices use a much longer TX interval.
const ACTIVE_WINDOW_MS = Number(process.env.LORA_ACTIVE_WINDOW_SEC || 300) * 1000;
// Number of frequency channels devices hop across (AU915 au915_0 = 8x125kHz
// channels). Two transmissions only actually collide if they land on the same
// channel AND overlap in time - the Pure ALOHA formula below only models the
// time-overlap part, so its result is divided by this to approximate the
// channel-hop effect (P(same channel) ~= 1/CHANNEL_COUNT for uniform random
// channel selection). Adjust if the sub-band/channel plan changes.
const CHANNEL_COUNT = Number(process.env.LORA_CHANNEL_COUNT || 8);
// A device stops counting toward metrics after ACTIVE_WINDOW_MS, but its
// tracking-map entries (csvStatsByDevice, deviceNameById, deviceGatewayById,
// lastFcntByDevice) stuck around forever until now - every distinct DevEUI
// ever seen (e.g. across many test boards over weeks) accumulated in memory
// with no cleanup. After this much longer idle time, purge it from those maps
// entirely rather than just excluding it from calculations.
const MAP_TTL_MS = Number(process.env.LORA_MAP_TTL_SEC || 3600) * 1000;

const CSV_COLUMNS = [
  "timestamp", "device_name", "f_cnt", "confirmed", "data_len",
  "rssi_dbm", "snr_db", "gateway_count", "pdr_percent", "throughput_bps",
  "sf", "toa_ms", "active_devices", "collision_rate_percent",
];

function csvEscape(value) {
  const s = value === null || value === undefined ? "" : String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

const LORA_CODE_RATES = { CR_4_5: 1, CR_4_6: 2, CR_4_7: 3, CR_4_8: 4 };

// LoRa Time-on-Air, per Semtech AN1200.13. phyPayloadLen approximates the PHY
// payload (MHDR + FHDR + FPort + MIC, no FOpts) as appDataLen + 13 bytes.
function computeTimeOnAirMs(sf, bwHz, codeRate, phyPayloadLen) {
  if (!sf || !bwHz) return undefined;
  const cr = LORA_CODE_RATES[codeRate] ?? 1;
  const tSymMs = (2 ** sf / bwHz) * 1000;
  const lowDrOptimize = tSymMs > 16 ? 1 : 0;
  const preambleSymbNb = 8; // LoRaWAN default preamble length
  const crc = 1; // uplinks always have CRC enabled
  const explicitHeader = 0; // LoRaWAN always uses an explicit header
  const numerator = 8 * phyPayloadLen - 4 * sf + 28 + 16 * crc - 20 * explicitHeader;
  const denominator = 4 * (sf - 2 * lowDrOptimize);
  const payloadSymbNb = 8 + Math.max(Math.ceil(numerator / denominator) * (cr + 4), 0);
  return (preambleSymbNb + 4.25) * tSymMs + payloadSymbNb * tSymMs;
}

fs.mkdirSync(path.dirname(CSV_PATH), { recursive: true });
if (!fs.existsSync(CSV_PATH)) {
  fs.writeFileSync(CSV_PATH, CSV_COLUMNS.join(",") + "\n");
}

function appendUplinkCsv(row) {
  const line = CSV_COLUMNS.map(col => csvEscape(row[col])).join(",") + "\n";
  fs.appendFile(CSV_PATH, line, err => {
    if (err) console.error("failed to append CSV row:", err.message);
  });
}

const register = new client.Registry();
client.collectDefaultMetrics({ register });

const labelNames = ["gateway_id"];
const deviceLabelNames = ["device_id", "device_name"];
const deviceGatewayLabelNames = ["device_id", "device_name", "gateway_id"];

const gauges = {
  rxPacketsReceived: new client.Gauge({
    name: "chirpstack_gateway_rx_packets_received",
    help: "Number of radio packets received during the last stat_interval window (reset by the gateway each report)",
    labelNames,
    registers: [register],
  }),
  rxPacketsReceivedOk: new client.Gauge({
    name: "chirpstack_gateway_rx_packets_received_ok",
    help: "Number of radio packets received with a valid PHY CRC during the last stat_interval window",
    labelNames,
    registers: [register],
  }),
  txPacketsReceived: new client.Gauge({
    name: "chirpstack_gateway_tx_packets_received",
    help: "Number of downlink packets received for transmission during the last stat_interval window",
    labelNames,
    registers: [register],
  }),
  txPacketsEmitted: new client.Gauge({
    name: "chirpstack_gateway_tx_packets_emitted",
    help: "Number of downlink packets emitted during the last stat_interval window",
    labelNames,
    registers: [register],
  }),
};

const lastSeen = new client.Gauge({
  name: "chirpstack_gateway_stats_last_seen_timestamp_seconds",
  help: "Unix timestamp of the last received gateway stats event",
  labelNames,
  registers: [register],
});

// Per-device uplink metrics (fed by application/+/device/+/event/up)
const deviceRssi = new client.Gauge({
  name: "chirpstack_device_rssi_dbm",
  help: "RSSI (dBm) of the last uplink, per gateway that received it",
  labelNames: deviceGatewayLabelNames,
  registers: [register],
});
const deviceSnr = new client.Gauge({
  name: "chirpstack_device_snr_db",
  help: "SNR (dB) of the last uplink, per gateway that received it",
  labelNames: deviceGatewayLabelNames,
  registers: [register],
});
const deviceUplinkFramesTotal = new client.Counter({
  name: "chirpstack_device_uplink_frames_total",
  help: "Total number of uplink frames received from the device (used for PDR and throughput)",
  labelNames: deviceLabelNames,
  registers: [register],
});
const deviceUplinkBytesTotal = new client.Counter({
  name: "chirpstack_device_uplink_bytes_total",
  help: "Total decoded payload bytes received from the device (use rate() for throughput)",
  labelNames: deviceLabelNames,
  registers: [register],
});
const deviceFcntGapTotal = new client.Counter({
  name: "chirpstack_device_fcnt_gap_total",
  help: "Total number of missing uplink frames inferred from FCnt gaps (used for PDR)",
  labelNames: deviceLabelNames,
  registers: [register],
});
const deviceLastFcnt = new client.Gauge({
  name: "chirpstack_device_last_fcnt",
  help: "Last observed uplink frame counter (FCnt) from the device",
  labelNames: deviceLabelNames,
  registers: [register],
});
const deviceLastSeen = new client.Gauge({
  name: "chirpstack_device_uplink_last_seen_timestamp_seconds",
  help: "Unix timestamp of the last received uplink from the device",
  labelNames: deviceLabelNames,
  registers: [register],
});
const deviceCollisionRatePercent = new client.Gauge({
  name: "chirpstack_device_collision_rate_percent",
  help: "Pure ALOHA collision probability estimate for the device's last uplink (see CSV export for methodology)",
  labelNames: deviceLabelNames,
  registers: [register],
});
const activeDevicesGauge = new client.Gauge({
  name: "chirpstack_active_devices",
  help: "Number of devices with an uplink inside the collision estimate's active window",
  registers: [register],
});

// device_id -> last FCnt seen, used to compute FCnt gaps (dropped frames) for PDR
const lastFcntByDevice = new Map();

// device_id -> device_name, needed to address the collision-rate Gauge's labels
// when zeroing out a device that stopped transmitting (see sweep below).
const deviceNameById = new Map();

// device_id -> gateway_id of its most recent uplink, needed to address the
// RSSI/SNR Gauges' labels when removing a stale device's series (see sweep below).
const deviceGatewayById = new Map();

// device_id -> running state for the CSV's cumulative PDR and instantaneous
// throughput columns (kept separate from the Prometheus counters above so the
// Grafana panels are unaffected by this book-keeping).
const csvStatsByDevice = new Map();

// Number of devices with an uplink inside the last ACTIVE_WINDOW_MS - used as N
// in the Pure ALOHA collision estimate, so it grows automatically as devices join.
function countActiveDevices(nowMs) {
  let count = 0;
  for (const state of csvStatsByDevice.values()) {
    if (state.lastTsMs !== undefined && nowMs - state.lastTsMs <= ACTIVE_WINDOW_MS) count++;
  }
  return count;
}

function handleStats(topic, payload) {
  let stats;
  try {
    stats = JSON.parse(payload.toString());
  } catch (err) {
    console.error(`failed to parse payload on ${topic}:`, err.message);
    return;
  }

  const gatewayId = stats.gatewayId || topic.split("/")[1];
  if (!gatewayId) {
    console.error(`no gatewayId found in payload on ${topic}`);
    return;
  }

  const labels = { gateway_id: gatewayId };
  gauges.rxPacketsReceived.set(labels, stats.rxPacketsReceived || 0);
  gauges.rxPacketsReceivedOk.set(labels, stats.rxPacketsReceivedOk || 0);
  gauges.txPacketsReceived.set(labels, stats.txPacketsReceived || 0);
  gauges.txPacketsEmitted.set(labels, stats.txPacketsEmitted || 0);
  lastSeen.set(labels, Date.now() / 1000);
}

function handleUplink(topic, payload) {
  let up;
  try {
    up = JSON.parse(payload.toString());
  } catch (err) {
    console.error(`failed to parse payload on ${topic}:`, err.message);
    return;
  }

  const deviceId = up.deviceInfo?.devEui || topic.split("/")[3];
  if (!deviceId) {
    console.error(`no devEui found in payload on ${topic}`);
    return;
  }
  const deviceName = up.deviceInfo?.deviceName || "";
  const deviceLabels = { device_id: deviceId, device_name: deviceName };
  deviceNameById.set(deviceId, deviceName);

  const fCnt = Number.isInteger(up.fCnt) ? up.fCnt : null;
  const dataB64 = up.data || "";
  const dataBuf = Buffer.from(dataB64, "base64");
  const rxInfo = Array.isArray(up.rxInfo) ? up.rxInfo : [];

  // A confirmed-message retry re-sends the same FCnt (same logical frame); a
  // rejoin resets the device's FCnt to a value lower than what we last saw.
  // Both cases are detected once here and reused by the Prometheus and CSV
  // book-keeping below, since they share the same underlying FCnt history.
  const lastFcntSeen = lastFcntByDevice.get(deviceId);
  const isRetry = fCnt !== null && lastFcntSeen !== undefined && fCnt === lastFcntSeen;
  const isRejoin = fCnt !== null && lastFcntSeen !== undefined && fCnt < lastFcntSeen;

  for (const rx of rxInfo) {
    if (!rx.gatewayId) continue;
    const gwLabels = { ...deviceLabels, gateway_id: rx.gatewayId };
    if (typeof rx.rssi === "number") deviceRssi.set(gwLabels, rx.rssi);
    if (typeof rx.snr === "number") deviceSnr.set(gwLabels, rx.snr);
    deviceGatewayById.set(deviceId, rx.gatewayId);
  }

  // A retry is still real airtime on the channel, so bytes/last-seen still count -
  // only the "new frame received" accounting below excludes it.
  if (!isRetry) {
    deviceUplinkFramesTotal.inc(deviceLabels, 1);
  }
  deviceUplinkBytesTotal.inc(deviceLabels, dataBuf.length);
  deviceLastSeen.set(deviceLabels, Date.now() / 1000);

  if (fCnt !== null && !isRetry) {
    // always touch the counter (even by 0) so the series exists from the first
    // uplink onward - otherwise the PDR panel shows "No data" until a gap ever happens
    const gap = !isRejoin && lastFcntSeen !== undefined && fCnt > lastFcntSeen ? fCnt - lastFcntSeen - 1 : 0;
    deviceFcntGapTotal.inc(deviceLabels, Math.max(gap, 0));
    lastFcntByDevice.set(deviceId, fCnt);
    deviceLastFcnt.set(deviceLabels, fCnt);
  }

  const bestRx = rxInfo.reduce((best, rx) => (best === null || (rx.rssi ?? -Infinity) > (best.rssi ?? -Infinity) ? rx : best), null);

  const ts = new Date().toISOString();
  const payloadHex = dataBuf.toString("hex");

  const lora = up.txInfo?.modulation?.lora;
  const sf = lora?.spreadingFactor;
  const toaMs = computeTimeOnAirMs(sf, lora?.bandwidth, lora?.codeRate, dataBuf.length + 13);

  // Structured log line, one per uplink, scraped from the container's stdout by
  // Promtail and shipped to Loki - this is the "log + payload" view in Grafana.
  console.log(JSON.stringify({
    ts,
    event: "uplink",
    device_id: deviceId,
    device_name: deviceName,
    f_cnt: fCnt,
    f_port: up.fPort ?? null,
    confirmed: up.confirmed ?? null,
    data_len: dataBuf.length,
    payload_b64: dataB64,
    payload_hex: payloadHex,
    rssi_dbm: bestRx?.rssi ?? null,
    snr_db: bestRx?.snr ?? null,
    gateway_id: bestRx?.gatewayId ?? null,
    gateway_count: rxInfo.length,
  }));

  // Cumulative PDR (received / (received + FCnt-gap-inferred losses)) and
  // instantaneous throughput (this packet's bytes over time since the device's
  // previous uplink), tracked per device for the CSV row below. lastFcntSeen/
  // isRetry/isRejoin (computed above) are shared with the Prometheus block.
  let csvState = csvStatsByDevice.get(deviceId);
  if (!csvState) {
    csvState = { framesTotal: 0, gapsTotal: 0, lastTsMs: undefined, lambdaPerSec: undefined };
    csvStatsByDevice.set(deviceId, csvState);
  }
  const nowMs = Date.now();
  if (isRejoin) {
    // New session (FCnt restarted) - stop mixing this session's PDR with the last one.
    csvState.framesTotal = 0;
    csvState.gapsTotal = 0;
  }
  if (fCnt !== null && !isRetry) {
    const gap = !isRejoin && lastFcntSeen !== undefined && fCnt > lastFcntSeen ? fCnt - lastFcntSeen - 1 : 0;
    csvState.gapsTotal += Math.max(gap, 0);
  }
  if (!isRetry) {
    csvState.framesTotal += 1;
  }
  const pdrDenom = csvState.framesTotal + csvState.gapsTotal;
  const pdrPercent = pdrDenom > 0 ? ((100 * csvState.framesTotal) / pdrDenom).toFixed(1) : "";
  const elapsedSec = csvState.lastTsMs !== undefined ? (nowMs - csvState.lastTsMs) / 1000 : undefined;
  const throughputBps = elapsedSec !== undefined && elapsedSec > 0 ? (dataBuf.length / elapsedSec).toFixed(2) : "";
  if (elapsedSec !== undefined && elapsedSec > 0) {
    csvState.lambdaPerSec = 1 / elapsedSec;
  }
  csvState.lastTsMs = nowMs;

  // Pure ALOHA collision probability estimate: P = (1 - e^(-2G)) / CHANNEL_COUNT,
  // G = sum of the OTHER active devices' observed transmission rates (packets/sec)
  // times this packet's ToA - a packet can't collide with itself, so this
  // device's own rate is excluded, and each device's *actual* measured rate is
  // used instead of assuming every device transmits like this one. The result
  // is divided by CHANNEL_COUNT because 1-e^(-2G) only models time overlap;
  // an actual collision also requires landing on the same frequency channel.
  // With no other active device, G is 0 and the result is naturally 0 - no
  // special case needed.
  const activeDeviceCount = countActiveDevices(nowMs);
  let collisionRatePercent = "";
  if (elapsedSec !== undefined && elapsedSec > 0 && toaMs !== undefined) {
    let othersLambdaSum = 0;
    for (const [otherId, otherState] of csvStatsByDevice) {
      if (otherId === deviceId) continue;
      if (otherState.lastTsMs === undefined || nowMs - otherState.lastTsMs > ACTIVE_WINDOW_MS) continue;
      if (otherState.lambdaPerSec !== undefined) othersLambdaSum += otherState.lambdaPerSec;
    }
    const g = othersLambdaSum * (toaMs / 1000);
    collisionRatePercent = ((100 * (1 - Math.exp(-2 * g))) / CHANNEL_COUNT).toFixed(2);
  }
  if (collisionRatePercent !== "") {
    deviceCollisionRatePercent.set(deviceLabels, Number(collisionRatePercent));
  }
  activeDevicesGauge.set(activeDeviceCount);

  // One CSV row per uplink, persisted to a host-mounted volume for offline analysis.
  appendUplinkCsv({
    timestamp: ts,
    device_name: deviceName,
    f_cnt: fCnt,
    confirmed: up.confirmed ?? "",
    data_len: dataBuf.length,
    rssi_dbm: bestRx?.rssi ?? "",
    snr_db: bestRx?.snr ?? "",
    gateway_count: rxInfo.length,
    pdr_percent: pdrPercent,
    throughput_bps: throughputBps,
    sf: sf ?? "",
    toa_ms: toaMs !== undefined ? toaMs.toFixed(1) : "",
    active_devices: activeDeviceCount,
    collision_rate_percent: collisionRatePercent,
  });
}

// A Prometheus Gauge holds its last .set() value forever - a device that stops
// transmitting would otherwise show a frozen collision rate/RSSI/SNR on the
// Grafana graphs indefinitely, once the device falls outside the active window
// and without waiting for another uplink from it. Collision rate is zeroed
// (0% is a true statement when there's nothing left to collide with); RSSI/SNR
// are removed instead (a stale dBm/dB reading would look like a real, current
// measurement, which zero or any other frozen number would misrepresent - the
// graph should simply stop drawing a line for that device).
setInterval(() => {
  const nowMs = Date.now();
  for (const [devId, state] of csvStatsByDevice) {
    if (state.lastTsMs !== undefined && nowMs - state.lastTsMs > ACTIVE_WINDOW_MS) {
      const devLabels = { device_id: devId, device_name: deviceNameById.get(devId) || "" };
      deviceCollisionRatePercent.set(devLabels, 0);
      const gatewayId = deviceGatewayById.get(devId);
      if (gatewayId !== undefined) {
        const gwLabels = { ...devLabels, gateway_id: gatewayId };
        deviceRssi.remove(gwLabels);
        deviceSnr.remove(gwLabels);
      }
      // PDR is a raw ratio of two Counters (not rate()-based), so it doesn't
      // self-correct like the throughput panel does - remove both so the
      // device's line/legend entry disappears from the PDR panel too. If the
      // device comes back later, these simply start counting again from 0,
      // matching the CSV's own rejoin-reset behavior.
      deviceUplinkFramesTotal.remove(devLabels);
      deviceFcntGapTotal.remove(devLabels);
      deviceUplinkBytesTotal.remove(devLabels);
      deviceLastFcnt.remove(devLabels);
      deviceLastSeen.remove(devLabels);
    }
    if (state.lastTsMs !== undefined && nowMs - state.lastTsMs > MAP_TTL_MS) {
      // Long-gone device - forget it entirely instead of tracking it forever.
      csvStatsByDevice.delete(devId);
      deviceNameById.delete(devId);
      deviceGatewayById.delete(devId);
      lastFcntByDevice.delete(devId);
    }
  }
  // activeDevicesGauge is otherwise only updated as a side effect of processing
  // an uplink (inside handleUplink) - if every device goes silent at once,
  // nothing would ever trigger a recount, and it would stay frozen at its last
  // value forever. Recompute it here on every sweep tick so it reflects reality
  // even when there's no uplink traffic at all to piggyback the update on.
  activeDevicesGauge.set(countActiveDevices(nowMs));
}, 15000);

const mqttClient = mqtt.connect(MQTT_URL);

mqttClient.on("connect", () => {
  console.log(`connected to ${MQTT_URL}, subscribing to ${STATS_TOPIC} and ${UPLINK_TOPIC}`);
  mqttClient.subscribe(STATS_TOPIC, err => {
    if (err) {
      console.error("subscribe failed:", err.message);
    }
  });
  mqttClient.subscribe(UPLINK_TOPIC, err => {
    if (err) {
      console.error("subscribe failed:", err.message);
    }
  });
});

mqttClient.on("message", (topic, payload) => {
  if (topic.endsWith("/event/up")) {
    handleUplink(topic, payload);
  } else if (topic.endsWith("/event/stats")) {
    handleStats(topic, payload);
  }
});
mqttClient.on("error", err => console.error("mqtt error:", err.message));

const server = http.createServer(async (req, res) => {
  if (req.url === "/metrics") {
    res.setHeader("Content-Type", register.contentType);
    res.end(await register.metrics());
  } else if (req.url === "/health") {
    res.end("ok");
  } else {
    res.statusCode = 404;
    res.end();
  }
});

server.listen(LISTEN_PORT, () => {
  console.log(`metrics server listening on :${LISTEN_PORT}/metrics`);
});
