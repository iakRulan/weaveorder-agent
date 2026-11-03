import type { CSSProperties, ReactNode } from "react";
import type { ChapterStepProps } from "../../registry/types";
import "./Solution.css";

/** 允许在 style 里传 --d（动画延迟），供 keyframes 使用。 */
type AnimStyle = CSSProperties & { "--d"?: string };

/** 五段闭环（顺序即流程顺序）。 */
const SEGMENTS = [
  { no: "01", zh: "任务输入", en: "INTAKE" },
  { no: "02", zh: "信息理解", en: "UNDERSTAND" },
  { no: "03", zh: "流程判断", en: "PLAN" },
  { no: "04", zh: "系统调用", en: "INVOKE" },
  { no: "05", zh: "结果输出", en: "OUTPUT" },
] as const;

/** 真实运行：7 步工具调用（file_read ×1 / sql_query ×5 / report_render ×1）。 */
const RUN_STEPS = [
  { no: "01", tool: "file_read", desc: "读取 orders / returns / people 三表结构" },
  { no: "02", tool: "sql_query", desc: "子品类盈亏聚合" },
  { no: "03", tool: "sql_query", desc: "折扣档位盈亏聚合" },
  { no: "04", tool: "sql_query", desc: "跨表退货率 · LEFT JOIN" },
  { no: "05", tool: "sql_query", desc: "品类汇总" },
  { no: "06", tool: "sql_query", desc: "整体指标" },
  { no: "07", tool: "report_render", desc: "渲染 Markdown 报告与明细表" },
] as const;

/** 事件溯源：34 条事件的类型序列。 */
const EVENTS = [
  "run_start",
  "intake",
  "plan",
  "step",
  "tool_call",
  "tool_result",
  "verify",
  "run_end",
] as const;

/** 实测识别的四类意图。 */
const INTENTS = ["profit_diagnosis", "trend", "region", "category_overview"] as const;

/** 5 类 SQL 聚合。 */
const SQL_AGG = ["子品类盈亏", "折扣档位", "跨表退货率", "品类汇总", "整体指标"] as const;

/** 评测覆盖的四个维度。 */
const EVAL_CHIPS = ["意图识别", "黄金值核验", "注入拦截", "消融对照"] as const;

/* ─────────────────────────── 原子组件 ─────────────────────────── */

function Kicker({ children }: { children: ReactNode }) {
  return <div className="sl-kicker kicker">{children}</div>;
}

/** 顶部五段位置指示：lit 为当前讲述中的段。 */
function MiniChain({ lit }: { lit: number[] }) {
  return (
    <div className="sl-minichain" aria-hidden>
      {SEGMENTS.map((s, i) => {
        const on = lit.includes(i);
        return (
          <div className="sl-miniseg" key={s.no}>
            <span className={`sl-minidot${on ? " is-lit" : ""}`} />
            <span className={`sl-minizh serif-cn${on ? " is-lit" : ""}`}>{s.zh}</span>
            {i < SEGMENTS.length - 1 && (
              <span className={`sl-minilink${on ? " is-lit" : ""}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

/** step 0：五个空槽位 + 一道扫过的光。 */
function GhostChain() {
  return (
    <div className="sl-ghosts" aria-hidden>
      {SEGMENTS.map((s, i) => (
        <div className="sl-ghost" style={{ "--d": `${i * 140}ms` } as AnimStyle} key={s.no}>
          <span className="sl-ghost-slot" />
          <span className="sl-ghost-no label-mono">{s.no}</span>
        </div>
      ))}
      <span className="sl-scan" />
    </div>
  );
}

/** step 1：五段主链 —— 节点依次点亮，连线逐段自绘。 */
function MainChain() {
  return (
    <div className="sl-chain" aria-hidden>
      {SEGMENTS.map((s, i) => (
        <div className="sl-seg" key={s.no}>
          <div className="sl-node" style={{ "--d": `${i * 260}ms` } as AnimStyle}>
            <span className="sl-badge hero-num">{s.no}</span>
            <span className="sl-node-zh serif-cn">{s.zh}</span>
            <span className="sl-node-en label-mono">{s.en}</span>
          </div>
          {i < SEGMENTS.length - 1 && (
            <div
              className="sl-link"
              style={{ "--d": `${i * 260 + 240}ms` } as AnimStyle}
            />
          )}
        </div>
      ))}
    </div>
  );
}

/** step 2：三段 stage 卡。 */
function StageCard({
  title,
  sub,
  lines,
  delay,
}: {
  title: string;
  sub: string;
  lines: string[];
  delay: number;
}) {
  return (
    <div className="sl-card" style={{ "--d": `${delay}ms` } as AnimStyle}>
      <div className="sl-card-title serif-cn">{title}</div>
      <div className="sl-card-sub label-mono">{sub}</div>
      <div className="sl-card-lines">
        {lines.map((l) => (
          <div className="sl-card-line" key={l}>
            {l}
          </div>
        ))}
      </div>
    </div>
  );
}

/** step 3：数据流转管道站。 */
function Station({
  title,
  sub,
  lines,
  accent,
  delay,
}: {
  title: string;
  sub: string;
  lines: string[];
  accent?: boolean;
  delay: number;
}) {
  return (
    <div
      className={`sl-station${accent ? " is-accent" : ""}`}
      style={{ "--d": `${delay}ms` } as AnimStyle}
    >
      <div className="sl-station-title serif-cn">{title}</div>
      <div className="sl-station-sub label-mono">{sub}</div>
      <div className="sl-card-lines">
        {lines.map((l) => (
          <div className="sl-station-line" key={l}>
            {l}
          </div>
        ))}
      </div>
    </div>
  );
}

/** step 3：校验条 —— 报告值与重算值对齐才能发布。 */
function VerifyStrip() {
  return (
    <div className="sl-verify">
      <span className="sl-verify-cell label-mono">报告值</span>
      <span className="sl-verify-op serif-cn">＝</span>
      <span className="sl-verify-cell label-mono">重算值</span>
      <span className="sl-verify-op serif-cn">→</span>
      <span className="sl-verify-pass serif-cn">一致 · 发布</span>
    </div>
  );
}

/** step 4：确定性模板生成的分析 DAG（SVG 连线自绘）。 */
function DagNode({
  x,
  y,
  w,
  h,
  label,
  accent,
  delay,
}: {
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  accent?: boolean;
  delay: number;
}) {
  return (
    <g className="sl-dag-group" style={{ "--d": `${delay}ms` } as AnimStyle}>
      <rect
        className={`sl-dag-node${accent ? " is-accent" : ""}`}
        x={x}
        y={y}
        width={w}
        height={h}
        rx={8}
      />
      <text
        className={`sl-dag-text${accent ? " is-accent" : ""}`}
        x={x + w / 2}
        y={y + h / 2}
        textAnchor="middle"
        dominantBaseline="middle"
      >
        {label}
      </text>
    </g>
  );
}

function Dag() {
  return (
    <svg
      className="sl-dag"
      viewBox="0 0 720 350"
      role="img"
      aria-label="按意图生成的确定性分析 DAG"
    >
      {SQL_AGG.map((label, i) => (
        <line
          key={`t-${label}`}
          className="sl-dag-edge"
          x1={360}
          y1={60}
          x2={135}
          y2={76 + i * 54}
          pathLength={1}
          style={{ "--d": `${320 + i * 120}ms` } as AnimStyle}
        />
      ))}
      {SQL_AGG.map((label, i) => (
        <line
          key={`s-${label}`}
          className="sl-dag-edge"
          x1={250}
          y1={98 + i * 54}
          x2={470}
          y2={176}
          pathLength={1}
          style={{ "--d": `${1000 + i * 110}ms` } as AnimStyle}
        />
      ))}
      <line
        className="sl-dag-edge"
        x1={580}
        y1={202}
        x2={580}
        y2={252}
        pathLength={1}
        style={{ "--d": "1660ms" } as AnimStyle}
      />

      <DagNode x={250} y={8} w={220} h={52} label="意图 → 分析模板" accent delay={0} />
      {SQL_AGG.map((label, i) => (
        <DagNode
          key={label}
          x={20}
          y={76 + i * 54}
          w={230}
          h={44}
          label={label}
          delay={200 + i * 120}
        />
      ))}
      <DagNode x={470} y={150} w={220} h={52} label="指标引用解析" delay={980} />
      <DagNode x={470} y={252} w={220} h={52} label="报告输出" accent delay={1500} />
    </svg>
  );
}

/* ─────────────────────────── 章节主体 ─────────────────────────── */

/**
 * 第 3 章 · solution — 五段闭环与真实运行。
 *
 * 八步：接手 → 五段点名 → 理解/规划/取数 → 报表/校验 → 关键设计 →
 *       真实运行 7 步 → 345 tokens / 1.4 秒 → 评测 7/7。
 * 每步一个独占整屏的场景；颜色/字体全部走主题 token，动画用 CSS keyframes。
 */
export default function Solution({ step }: ChapterStepProps) {
  if (step === 0) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>织序智能体 · WEAVEORDER AGENT</Kicker>
        <h1 className="sl-h1 serif-cn">
          织序智能体接手
          <br />
          <span className="sl-em">这五段闭环</span>。
        </h1>
        <GhostChain />
        <p className="sl-note serif-cn">从一句需求，到一份报告。</p>
      </div>
    );
  }

  if (step === 1) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>五段闭环 · FIVE STAGES</Kicker>
        <h2 className="sl-h2 serif-cn">五个阶段，一条闭环</h2>
        <MainChain />
        <p className="sl-note serif-cn">每个阶段的产出，都是下一阶段的输入。</p>
      </div>
    );
  }

  if (step === 2) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>前三段 · 理解 → 规划 → 取数</Kicker>
        <MiniChain lit={[1, 2, 3]} />
        <div className="sl-trio">
          <StageCard
            title="信息理解"
            sub="大模型解析"
            lines={["意图 / 焦点 / 维度 / 指标"]}
            delay={0}
          />
          <span className="sl-arrow" aria-hidden />
          <StageCard
            title="流程判断"
            sub="确定性 DAG"
            lines={["可复现 · 可审计"]}
            delay={180}
          />
          <span className="sl-arrow" aria-hidden />
          <StageCard
            title="系统调用"
            sub="数据服务"
            lines={["SQLite · 只读 SELECT"]}
            delay={360}
          />
        </div>
      </div>
    );
  }

  if (step === 3) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>后两段 · 报表 → 校验</Kicker>
        <MiniChain lit={[3, 4]} />
        <div className="sl-pipe">
          <Station
            title="数据服务"
            sub="SQLite 引擎"
            lines={["只读 SELECT", "5 类 SQL 聚合"]}
            delay={0}
          />
          <span className="sl-arrow" aria-hidden />
          <Station
            title="报表服务"
            sub="指标引用解析"
            lines={["生成 Markdown 报告", "不手抄数字"]}
            delay={180}
          />
          <span className="sl-arrow" aria-hidden />
          <Station
            title="结果输出"
            sub="报告 + 明细表"
            lines={["结构化交付"]}
            delay={360}
          />
          <span className="sl-arrow" aria-hidden />
          <Station
            title="校验器"
            sub="独立重算"
            lines={["每个指标重算", "不一致即拦截"]}
            accent
            delay={540}
          />
        </div>
        <VerifyStrip />
      </div>
    );
  }

  if (step === 4) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>关键设计 · 分工</Kicker>
        <h2 className="sl-h2 serif-cn">
          理解靠<span className="sl-em">大模型</span>，规划用
          <span className="sl-em">确定性模板</span>
        </h2>
        <div className="sl-split">
          <div className="sl-panel">
            <div className="sl-panel-tag label-mono">理解 · 大模型</div>
            <div className="sl-panel-title serif-cn">自然语言 → 结构化要点</div>
            <div className="sl-panel-line">意图 / 焦点 / 维度 / 指标</div>
            <div className="sl-chips">
              {INTENTS.map((t) => (
                <span className="sl-chip label-mono" key={t}>
                  {t}
                </span>
              ))}
            </div>
            <div className="sl-panel-note serif-cn">实测识别四类意图</div>
          </div>
          <div className="sl-panel">
            <div className="sl-panel-tag label-mono">规划 · 确定性模板</div>
            <div className="sl-panel-title serif-cn">按意图生成分析 DAG</div>
            <Dag />
            <div className="sl-panel-note serif-cn">可复现 · 可审计</div>
          </div>
        </div>
      </div>
    );
  }

  if (step === 5) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>真实运行 · RUN ARCHIVE</Kicker>
        <h2 className="sl-h2 serif-cn">我们让它跑了一遍</h2>
        <div className="sl-run">
          <div className="sl-timeline">
            {RUN_STEPS.map((s, i) => (
              <div
                className="sl-trow"
                style={{ "--d": `${i * 200}ms` } as AnimStyle}
                key={s.no}
              >
                <span className="sl-tno hero-num">{s.no}</span>
                <span className="sl-ttool label-mono">{s.tool}</span>
                <span className="sl-tdesc serif-cn">{s.desc}</span>
                <span className="sl-tcheck" aria-hidden>
                  <svg viewBox="0 0 24 24">
                    <path d="M4 12.5 L10 18 L20 6" />
                  </svg>
                </span>
              </div>
            ))}
          </div>
          <div className="sl-runrail">
            <div className="sl-bignum hero-num">
              7<span className="sl-unit serif-cn">/7</span>
            </div>
            <div className="sl-rail-line serif-cn">7 步全部成功 · 7 次工具调用</div>
            <div className="sl-rail-bar">
              <span />
            </div>
            <div className="sl-events label-mono">
              {EVENTS.map((e) => (
                <span key={e}>{e}</span>
              ))}
            </div>
            <div className="sl-events-note serif-cn">34 条事件 · 事件溯源</div>
          </div>
        </div>
      </div>
    );
  }

  if (step === 6) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>运行指标 · METRICS</Kicker>
        <div className="sl-metrics">
          <div className="sl-metric">
            <div className="sl-metric-num hero-num">345</div>
            <div className="sl-metric-label label-mono">tokens</div>
            <div className="sl-stack">
              <span className="sl-stack-a" />
              <span className="sl-stack-b" />
            </div>
            <div className="sl-legend">
              <span className="sl-legend-item">
                <i className="sl-swatch sl-swatch-a" />
                prompt 240
              </span>
              <span className="sl-legend-item">
                <i className="sl-swatch sl-swatch-b" />
                completion 105
              </span>
            </div>
          </div>
          <div className="sl-metric">
            <div className="sl-metric-num hero-num">
              1.4<span className="sl-metric-unit serif-cn">秒</span>
            </div>
            <div className="sl-metric-label label-mono">端到端耗时</div>
            <div className="sl-duration">
              <span />
            </div>
            <div className="sl-legend">
              <span className="sl-legend-item">含 1 次大模型调用</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (step === 7) {
    return (
      <div className="sl-scene scene-pad">
        <Kicker>评测 · EVALUATION</Kicker>
        <div className="sl-eval">
          <div className="sl-eval-num hero-num">7/7</div>
          <div className="sl-ticks">
            {Array.from({ length: 7 }, (_, i) => (
              <span
                className="sl-tick"
                style={{ "--d": `${i * 160}ms` } as AnimStyle}
                key={i}
                aria-hidden
              >
                <svg viewBox="0 0 24 24">
                  <path d="M4 12.5 L10 18 L20 6" />
                </svg>
              </span>
            ))}
          </div>
        </div>
        <p className="sl-eval-line serif-cn">
          黄金值由脚本从 CSV 独立重算，与被测实现零共享代码。
        </p>
        <div className="sl-chips">
          {EVAL_CHIPS.map((c) => (
            <span className="sl-chip serif-cn" key={c}>
              {c}
            </span>
          ))}
        </div>
      </div>
    );
  }

  return null;
}
