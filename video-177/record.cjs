/**
 * 177 演示视频 · 无头自动录制
 *
 * 流程：
 *   1. 无头 Chromium 打开 ?auto=1（Auto 模式：每步播完音频自动推进）
 *   2. CDP Page.startScreencast 逐帧采集（JPEG q90）
 *   3. 按 150ms 轮询 localStorage 光标，记录每一步的实测切换时刻（transitions）
 *   4. 停止采集后：帧序列 → 视频；按 transitions 把每步 mp3 落到对应时间轴 → 混流
 *
 * 用法：node record.cjs
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const ROOT = "/Users/ryan/Desktop/兴智杯/weaveorder-177-video";
const PRES = path.join(ROOT, "presentation");
const OUT = path.join(ROOT, "record");
const FRAMES = path.join(OUT, "frames");
const URL = "http://localhost:5174/?auto=1";
const STORE_KEY = "presentation-cursor-v5";

fs.mkdirSync(FRAMES, { recursive: true });

function log(...a) { console.log(new Date().toISOString().slice(11, 19), ...a); }

(async () => {
  const segments = JSON.parse(fs.readFileSync(path.join(PRES, "audio-segments.json"), "utf8"));
  const totalSteps = segments.length;
  // 每步音频时长
  const audioDur = segments.map((s) => {
    const p = path.join(PRES, "public", "audio", s.audio);
    if (!fs.existsSync(p)) return 3; // 兜底 3s
    const out = execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration",
      "-of", "csv=p=0", p], { encoding: "utf8" }).trim();
    return parseFloat(out) || 3;
  });
  const totalAudio = audioDur.reduce((a, b) => a + b, 0);
  log(`共 ${totalSteps} 步，音频总时长 ${totalAudio.toFixed(1)}s`);

  const browser = await chromium.launch({
    headless: true,
    args: ["--force-device-scale-factor=1", "--disable-gpu-vsync", "--mute-audio"],
  });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  await page.goto(URL, { waitUntil: "domcontentloaded" });
  // 清掉历史光标，保证从第 0 步开始
  await page.evaluate((k) => localStorage.removeItem(k), STORE_KEY);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);

  const client = await context.newCDPSession(page);
  const frames = [];
  client.on("Page.screencastFrame", (ev) => {
    frames.push({ data: ev.data, ts: ev.metadata && ev.metadata.timestamp });
    client.send("Page.screencastFrameAck", { sessionId: ev.sessionId }).catch(() => {});
  });
  await client.send("Page.startScreencast", {
    format: "jpeg", quality: 90, everyNthFrame: 1, deviceScaleFactor: 1,
  });
  log("screencast 已启动");

  // 点击启动闸门（不能按 SPACE：SPACE 会同时触发"开始"和"前进一步"）
  await page.waitForSelector(".auto-gate", { timeout: 15000 });
  const t0 = Date.now();
  await page.click(".auto-gate");
  log("已点击启动闸门，开始播放");

  const transitions = [];
  let lastKey = "";
  let lastChange = Date.now();
  while (true) {
    const cur = await page.evaluate((k) => localStorage.getItem(k), STORE_KEY);
    const key = cur || "null";
    const now = Date.now();
    if (key !== lastKey) {
      transitions.push({ t: (now - t0) / 1000, cur: key });
      lastKey = key;
      lastChange = now;
    }
    // 到达最后一步后，等最后一段音频播完再停
    let atLast = false;
    try {
      const c = JSON.parse(key);
      atLast = c && c.chapter === 4 && c.step === totalSteps - 1 - 24; // trust 最后一步
    } catch { /* ignore */ }
    const elapsedSinceChange = (now - lastChange) / 1000;
    if (transitions.length >= totalSteps && elapsedSinceChange > 4) break;
    if (atLast && elapsedSinceChange > 4) break;
    if ((now - t0) / 1000 > totalAudio + 30) break;
    await page.waitForTimeout(100);
  }
  const dur = (Date.now() - t0) / 1000;
  log(`播放结束，时长 ${dur.toFixed(1)}s，切换点 ${transitions.length} 个`);

  await client.send("Page.stopScreencast").catch(() => {});
  await browser.close();

  // 落盘帧
  log(`写入 ${frames.length} 帧…`);
  frames.forEach((f, i) => {
    fs.writeFileSync(path.join(FRAMES, `f${String(i).padStart(6, "0")}.jpg`), Buffer.from(f.data, "base64"));
  });
  fs.writeFileSync(path.join(OUT, "transitions.json"),
    JSON.stringify({ duration: dur, transitions, audioDur, totalSteps }, null, 2));
  log("帧与切换点已保存 →", OUT);
})().catch((e) => { console.error(e); process.exit(1); });
