/**
 * 177 演示视频 · 装配
 *  1. record/frames/*.jpg → 视频（按实测时长算帧率）
 *  2. 按 transitions.json 的实测切换时刻，把每步 mp3 铺到时间轴上（补齐间隔差）
 *  3. 混流出片
 */
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const ROOT = "/Users/ryan/Desktop/兴智杯/weaveorder-177-video";
const REC = path.join(ROOT, "record");
const PRES = path.join(ROOT, "presentation");
const FRAMES = path.join(REC, "frames");
const TMP = path.join(REC, "tmp");

fs.mkdirSync(TMP, { recursive: true });

function sh(cmd, args) {
  return execFileSync(cmd, args, { encoding: "buffer", maxBuffer: 1 << 28 });
}
function log(...a) { console.log(new Date().toISOString().slice(11, 19), ...a); }

const meta = JSON.parse(fs.readFileSync(path.join(REC, "transitions.json"), "utf8"));
const { duration, transitions, audioDur, totalSteps } = meta;

// ── 1. 帧 → 视频 ─────────────────────────────────────────────
const frameFiles = fs.readdirSync(FRAMES).filter((f) => f.endsWith(".jpg")).sort();
const fps = frameFiles.length / duration;
log(`帧数 ${frameFiles.length}，时长 ${duration.toFixed(1)}s → 帧率 ${fps.toFixed(2)}`);

// 丢掉点击启动前后的几帧（避免闸门残影）
const skip = Math.max(0, Math.round(fps * 0.25));
const keep = frameFiles.slice(skip);
const startIdx = skip;
const listFile = path.join(TMP, "frames.txt");
fs.writeFileSync(listFile, keep.map((f) => `file '${path.join(FRAMES, f)}'\n`).join(""));

const videoOnly = path.join(TMP, "video.mp4");
execFileSync("ffmpeg", ["-y", "-v", "error",
  "-framerate", fps.toFixed(4), "-start_number", String(startIdx),
  "-i", path.join(FRAMES, "f%06d.jpg"),
  "-frames:v", String(keep.length),
  "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
  videoOnly]);
log("视频已生成（无声）");

// ── 2. 按实测时间轴铺音频 ────────────────────────────────────
const SR = 24000;
const totalSamples = Math.ceil(duration * SR);
const mix = new Int16Array(totalSamples);

transitions.forEach((tr, i) => {
  const seg = path.join(PRES, "public", "audio", segmentsAudio(i));
  if (!fs.existsSync(seg)) { log(`缺音频: step ${i}`); return; }
  const raw = path.join(TMP, `step-${i}.raw`);
  sh("ffmpeg", ["-v", "error", "-y", "-i", seg, "-ac", "1", "-ar", String(SR), "-f", "s16le", raw]);
  const pcm = fs.readFileSync(raw);
  const off = Math.round(tr.t * SR);
  const src = new Int16Array(pcm.buffer, pcm.byteOffset, pcm.length >> 1);
  for (let s = 0; s < src.length; s++) {
    const p = off + s;
    if (p < 0 || p >= totalSamples) break;
    const v = mix[p] + src[s];
    mix[p] = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
  }
});

const wavPath = path.join(TMP, "audio.wav");
const wav = Buffer.alloc(44 + mix.length * 2);
wav.write("RIFF", 0); wav.writeUInt32LE(36 + mix.length * 2, 4); wav.write("WAVE", 8);
wav.write("fmt ", 12); wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20);
wav.writeUInt16LE(1, 22); wav.writeUInt32LE(SR, 24); wav.writeUInt32LE(SR * 2, 28);
wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
wav.write("data", 36); wav.writeUInt32LE(mix.length * 2, 40);
Buffer.from(mix.buffer).copy(wav, 44);
fs.writeFileSync(wavPath, wav);
log("音轨已按实测切换铺好");

// ── 3. 混流 ─────────────────────────────────────────────────
const out = path.join(ROOT, "weaveorder-177-demo-v2.mp4");
execFileSync("ffmpeg", ["-y", "-v", "error", "-i", videoOnly, "-i", wavPath,
  "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", String(SR), "-shortest", out]);
log("成片 →", out);

const dur = parseFloat(execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration",
  "-of", "csv=p=0", out], { encoding: "utf8" }));
const size = (fs.statSync(out).size / 1048576).toFixed(1);
log(`时长 ${dur.toFixed(1)}s · 大小 ${size}MB`);

function segmentsAudio(i) {
  // audio-segments.json 的顺序 == 全局 step 顺序
  const segs = JSON.parse(fs.readFileSync(path.join(PRES, "audio-segments.json"), "utf8"));
  return segs[i].audio;
}
