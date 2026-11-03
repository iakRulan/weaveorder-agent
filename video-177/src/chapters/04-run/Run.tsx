import type { CSSProperties } from "react";
import type { ChapterStepProps } from "../../registry/types";
import "./Run.css";

/**
 * 第 4 章 · run — 结果：亏损归因。
 *
 * 七步：桌椅 −8.6% → 书柜 −3.0% → 提问（折扣>30%）→ −48.2% →
 *       不打折 +29.5% → 整体 +12.5% → 亏的是结构，不是大盘。
 * 每步一个独占整屏的场景；数据全部来自真实 SQL 结果（article §6）。
 * 颜色 / 字体全部走主题 token，动画用 CSS keyframes。
 */

/* ── 真实 SQL 结果（article §6） ── */

interface Subcat {
  en: string;
  cn: string;
  sales: string;
  profit: string;
  margin: string;
  orders: string;
  abs: number; // |利润率|，用于跨表统一刻度
  w: number; // 榜单内相对宽度（以 Tables 为 1）
}

interface Band {
  label: string;
  orders: string;
  sales: string;
  profit: string;
  margin: string;
  abs: number;
  w: number; // 以 >30% 的 48.2 为 1
}

const SUBCATS: Subcat[] = [
  { en: "Tables", cn: "桌椅", sales: "206,966", profit: "−17,725", margin: "−8.6", orders: "319", abs: 8.6, w: 1 },
  { en: "Bookcases", cn: "书柜", sales: "114,880", profit: "−3,473", margin: "−3.0", orders: "228", abs: 3.0, w: 0.196 },
  { en: "Supplies", cn: "用品", sales: "46,674", profit: "−1,189", margin: "−2.5", orders: "190", abs: 2.5, w: 0.067 },
];

const BANDS: Band[] = [
  { label: "0%", orders: "4,798", sales: "1,087,908", profit: "320,988", margin: "+29.5", abs: 29.5, w: 0.612 },
  { label: "1–20%", orders: "3,803", sales: "846,522", profit: "100,785", margin: "+11.9", abs: 11.9, w: 0.247 },
  { label: "21–30%", orders: "227", sales: "103,227", profit: "−10,369", margin: "−10.1", abs: 10.1, w: 0.21 },
  { label: ">30%", orders: "1,166", sales: "259,543", profit: "−125,007", margin: "−48.2", abs: 48.2, w: 1 },
];

const MAX_ABS = 48.2; // 全章统一刻度：折扣 >30% 的 48.2%
const DIGITS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"];

type Sign = 1 | -1;

/* ── 纯 CSS 数字卷轴 ── */

function Reel({ digit, delay = 0 }: { digit: string; delay?: number }) {
  return (
    <span
      className="rn-reel"
      aria-hidden
      style={{ "--d": Number(digit), "--delay": `${delay}ms` } as CSSProperties}
    >
      <span className="rn-reel-strip">
        {DIGITS.map((d) => (
          <span key={d} className="rn-reel-digit">
            {d}
          </span>
        ))}
      </span>
    </span>
  );
}

function RollNumber({ value, className = "" }: { value: string; className?: string }) {
  const head = value.charAt(0);
  const sign = head === "−" || head === "+" ? head : "";
  const num = sign ? value.slice(1) : value;
  const dot = num.indexOf(".");
  const intPart = dot === -1 ? num : num.slice(0, dot);
  const decPart = dot === -1 ? "" : num.slice(dot + 1);
  return (
    <span className={`hero-num rn-roll ${className}`}>
      {sign ? <span className="rn-roll-sign">{sign}</span> : null}
      {intPart.split("").map((d, i) => (
        <Reel key={`i${i}`} digit={d} delay={i * 110} />
      ))}
      {decPart ? <span className="rn-roll-dot">.</span> : null}
      {decPart
        ? decPart.split("").map((d, i) => (
            <Reel key={`d${i}`} digit={d} delay={(intPart.length + i) * 110} />
          ))
        : null}
      <span className="rn-roll-pct">%</span>
    </span>
  );
}

/* ── 双向条形（零轴居中，负左正右） ── */

function DivergeBar({
  w,
  sign,
  tone,
  delay = 0,
}: {
  w: number;
  sign: Sign;
  tone: "accent" | "text" | "mute";
  delay?: number;
}) {
  return (
    <span className="rn-bar">
      <span className="rn-bar-zero" />
      <span
        className={`rn-bar-fill ${sign < 0 ? "rn-bar-neg" : "rn-bar-pos"} rn-bar-${tone}`}
        style={{ "--w": w, animationDelay: `${delay}ms` } as CSSProperties}
      />
    </span>
  );
}

/* ── 子品类榜单行 ── */

function BoardRow({ rank, data, state }: { rank: string; data: Subcat; state: "hero" | "dim" | "more" }) {
  return (
    <div className={`rn-row rn-row-${state}`}>
      <div className="rn-row-id">
        <span className="rn-rank">{rank}</span>
        <span className="rn-name">
          <span className="rn-name-en">{data.en}</span>
          <span className="rn-name-cn">{data.cn}</span>
        </span>
      </div>
      <div className="rn-row-main">
        <DivergeBar w={data.w} sign={-1} tone="accent" />
        <div className="rn-facts">
          <span>
            销售额<b>{data.sales}</b>
          </span>
          <span>
            利润<b className="rn-neg">{data.profit}</b>
          </span>
          <span>
            订单<b>{data.orders}</b>
          </span>
        </div>
      </div>
      <div className="rn-row-margin">
        <RollNumber value={data.margin} />
      </div>
    </div>
  );
}

/* ── 折扣档位梯 ── */

function Ladder({ hot, revealAll }: { hot: ">30%" | "0%"; revealAll: boolean }) {
  return (
    <div className="rn-ladder">
      {BANDS.map((b, i) => {
        const isHot = b.label === hot;
        const neg = b.margin.charAt(0) === "−";
        const show = revealAll || isHot;
        return (
          <div
            key={b.label}
            className={`rn-lad-row${isHot ? " rn-lad-hot" : ""}${neg ? " rn-lad-neg" : ""}`}
            style={{ animationDelay: `${i * 110}ms` }}
          >
            <span className="rn-lad-label">{b.label}</span>
            <span className="rn-lad-bar">
              {show ? (
                <DivergeBar
                  w={b.w}
                  sign={neg ? -1 : 1}
                  tone={isHot ? "accent" : "mute"}
                  delay={isHot ? 260 : 0}
                />
              ) : (
                <span className="rn-lad-empty" />
              )}
            </span>
            <span className="rn-lad-val">{show ? b.margin : ""}</span>
            <span className="rn-lad-orders">
              {b.orders} 单
            </span>
          </div>
        );
      })}
    </div>
  );
}

/* ── step 0 · 桌椅 −8.6% ── */

function Scene0() {
  return (
    <div className="rn-scene scene-pad">
      <div className="rn-kicker kicker">结果 · 亏损归因</div>
      <div className="rn-board">
        <div className="rn-board-title">
          <span className="rn-board-name">子品类盈亏榜</span>
          <span className="rn-board-meta">按利润升序 · 真实 SQL 结果</span>
        </div>
        <div className="rn-board-head">
          <span>子品类</span>
          <span>盈亏</span>
          <span>利润率</span>
        </div>
        <BoardRow rank="01" data={SUBCATS[0]} state="hero" />
      </div>
      <p className="rn-note serif-cn">报告直接给出答案</p>
    </div>
  );
}

/* ── step 1 · 书柜 −3.0% ── */

function Scene1() {
  return (
    <div className="rn-scene scene-pad">
      <div className="rn-kicker kicker">结果 · 亏损归因</div>
      <div className="rn-board">
        <div className="rn-board-title">
          <span className="rn-board-name">子品类盈亏榜</span>
          <span className="rn-board-meta">按利润升序 · 真实 SQL 结果</span>
        </div>
        <div className="rn-board-head">
          <span>子品类</span>
          <span>盈亏</span>
          <span>利润率</span>
        </div>
        <BoardRow rank="01" data={SUBCATS[0]} state="dim" />
        <BoardRow rank="02" data={SUBCATS[1]} state="hero" />
        <BoardRow rank="03" data={SUBCATS[2]} state="more" />
      </div>
      <div className="rn-board-foot">
        <span className="rn-tag">亏损子品类 3 个</span>
      </div>
    </div>
  );
}

/* ── step 2 · 折扣超过三成的订单呢？ ── */

function Scene2() {
  return (
    <div className="rn-scene scene-pad">
      <div className="rn-kicker kicker">折扣档位</div>
      <h2 className="rn-q serif-cn">折扣超过三成的订单呢？</h2>
      <div className="rn-ruler">
        <div className="rn-ruler-trackwrap">
          <div className="rn-ruler-track">
            {BANDS.map((b) => (
              <div key={b.label} className={`rn-ruler-seg${b.label === ">30%" ? " rn-ruler-hot" : ""}`}>
                <span className="rn-ruler-num">{b.orders}</span>
              </div>
            ))}
          </div>
          <div className="rn-bracket">
            <span className="rn-qmark">?</span>
          </div>
        </div>
        <div className="rn-ruler-labels">
          {BANDS.map((b) => (
            <span key={b.label} className="rn-ruler-label">
              {b.label}
            </span>
          ))}
        </div>
      </div>
      <p className="rn-note serif-cn">四个折扣档，哪一档在亏？</p>
    </div>
  );
}

/* ── step 3 · 折扣 >30%：−48.2% ── */

function Scene3() {
  const hot = BANDS[3];
  return (
    <div className="rn-scene scene-pad">
      <div className="rn-split-wrap">
        <div className="rn-hero-col">
          <div className="rn-kicker kicker">折扣 &gt;30% 的订单</div>
          <div className="rn-hero-num">
            <RollNumber value={hot.margin} />
          </div>
          <div className="rn-hero-facts">
            <span>
              订单<b>{hot.orders}</b>
            </span>
            <span>
              销售额<b>{hot.sales}</b>
            </span>
            <span>
              利润<b className="rn-neg">{hot.profit}</b>
            </span>
          </div>
        </div>
        <Ladder hot=">30%" revealAll={false} />
      </div>
    </div>
  );
}

/* ── step 4 · 不打折的订单：+29.5% ── */

function Scene4() {
  const hot = BANDS[0];
  return (
    <div className="rn-scene scene-pad">
      <div className="rn-split-wrap">
        <Ladder hot="0%" revealAll />
        <div className="rn-hero-col">
          <div className="rn-kicker kicker">不打折的订单</div>
          <div className="rn-hero-num">
            <RollNumber value={hot.margin} />
          </div>
          <div className="rn-hero-facts">
            <span>
              订单<b>{hot.orders}</b>
            </span>
            <span>
              销售额<b>{hot.sales}</b>
            </span>
            <span>
              利润<b>{hot.profit}</b>
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── step 5 · 整体利润率 +12.5% ── */

function Scene5() {
  return (
    <div className="rn-scene scene-pad">
      <div className="rn-kicker kicker">整体指标</div>
      <div className="rn-hero-center">
        <div className="rn-hero-num rn-hero-big">
          <RollNumber value="+12.5" />
        </div>
        <div className="rn-hero-facts rn-facts-center">
          <span>
            销售额<b>2,297,201</b>
          </span>
          <span>
            利润<b>286,397</b>
          </span>
        </div>
      </div>
      <div className="rn-tiles">
        <div className="rn-tile">
          <span className="rn-tile-num hero-num">3</span>
          <span className="rn-tile-label">亏损子品类</span>
        </div>
        <div className="rn-tile">
          <span className="rn-tile-num hero-num">296</span>
          <span className="rn-tile-label">退货订单</span>
        </div>
        <div className="rn-tile rn-tile-wide">
          <span className="rn-tile-label">跨表退货率最高</span>
          <span className="rn-tile-ret">
            <b>Machines</b> 11.61%
          </span>
          <span className="rn-tile-ret rn-mute">Tables 9.77% · Appliances 8.87%</span>
        </div>
      </div>
    </div>
  );
}

/* ── step 6 · 亏的是结构，不是大盘 ── */

function Scene6() {
  return (
    <div className="rn-scene scene-pad">
      <div className="rn-kicker kicker">结论</div>
      <h2 className="rn-verdict serif-cn">
        亏的是<em className="rn-v-struct">结构</em>，不是<em className="rn-v-all">大盘</em>。
      </h2>
      <div className="rn-split">
        <div className="rn-panel rn-panel-struct">
          <div className="rn-panel-head">结构 · 亏损口袋</div>
          {SUBCATS.map((s) => (
            <div key={s.en} className="rn-mini-row">
              <span className="rn-mini-label">{s.cn}</span>
              <span className="rn-mini-bar">
                <DivergeBar w={s.abs / MAX_ABS} sign={-1} tone="accent" />
              </span>
              <span className="rn-mini-val">{s.margin}</span>
            </div>
          ))}
          <div className="rn-mini-row rn-mini-hot">
            <span className="rn-mini-label">折扣 &gt;30%</span>
            <span className="rn-mini-bar">
              <DivergeBar w={1} sign={-1} tone="accent" />
            </span>
            <span className="rn-mini-val">−48.2%</span>
          </div>
        </div>
        <div className="rn-panel rn-panel-all">
          <div className="rn-panel-head">大盘 · 整体</div>
          <div className="rn-panel-big">
            <RollNumber value="+12.5" />
          </div>
          <div className="rn-panel-bar">
            <DivergeBar w={12.5 / MAX_ABS} sign={1} tone="text" />
          </div>
          <div className="rn-panel-note">不打折的订单 +29.5%</div>
        </div>
      </div>
    </div>
  );
}

export default function Run({ step }: ChapterStepProps) {
  if (step === 0) return <Scene0 />;
  if (step === 1) return <Scene1 />;
  if (step === 2) return <Scene2 />;
  if (step === 3) return <Scene3 />;
  if (step === 4) return <Scene4 />;
  if (step === 5) return <Scene5 />;
  if (step === 6) return <Scene6 />;
  return null;
}
