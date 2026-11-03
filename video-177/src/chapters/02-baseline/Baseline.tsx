import type { CSSProperties } from "react";
import type { ChapterStepProps } from "../../registry/types";
import "./Baseline.css";

/* ─────────────────────────────────────────────────────────────────────
 * 第 2 章 · baseline — 场景与人工基线
 *
 * 八步：真实场景 → 数据规模 → 业务问题 → 人工八步 → 四段 SQL
 *       → 机器执行 → 人工端到端 → 耗时段。
 * 每步一个独占整屏的场景；颜色/字体全部走主题 token，动画用 CSS keyframes。
 * 数字与步骤名全部来自 article §2 / §3 的真实内容。
 * ───────────────────────────────────────────────────────────────────── */

/** 三张表（article §2） */
const TABLES = [
  { name: "orders", rows: "9,994 行", meta: "21 个字段" },
  { name: "returns", rows: "296 单", meta: "退货清单" },
  { name: "people", rows: "4 人", meta: "区域负责人" },
] as const;

/** 人工基线 8 步 SOP（article §3） */
const SOP = [
  { name: "数据理解", op: "识别 3 张表 · 21 个字段", min: 5 },
  { name: "建库导入", op: "建分析库 · 导入两表", min: 5 },
  { name: "子品类汇总", op: "编写并调试汇总 SQL", min: 10 },
  { name: "识别亏损", op: "排序并核对亏损子品类", min: 5 },
  { name: "折扣分档", op: "编写并调试分档 SQL", min: 10 },
  { name: "指标计算", op: "手工算利润率 · 交叉核对", min: 10 },
  { name: "跨表关联", op: "LEFT JOIN 计算退货率", min: 10 },
  { name: "报告撰写", op: "整理数字 · 撰写结论", min: 15 },
] as const;

const SOP_TOTAL = 70;

/** 四段 SQL（article §3 步骤 3 / 5 / 6 / 7 的特写） */
const SQL_CARDS = [
  {
    title: "子品类汇总",
    step: "步骤 3",
    min: 10,
    op: "按子品类汇总销售额与利润",
    lines: [
      "SELECT sub_category,",
      "       SUM(sales)  AS sales,",
      "       SUM(profit) AS profit",
      "FROM orders",
      "GROUP BY sub_category",
    ],
  },
  {
    title: "折扣分档",
    step: "步骤 5",
    min: 10,
    op: "按折扣档位拆分盈亏",
    lines: [
      "SELECT CASE",
      "       WHEN discount = 0    THEN '0%'",
      "       WHEN discount <= 0.2 THEN '1-20%'",
      "       WHEN discount <= 0.3 THEN '21-30%'",
      "       ELSE '>30%'",
      "       END AS bucket,",
      "       SUM(profit) AS profit",
      "FROM orders",
      "GROUP BY bucket",
    ],
  },
  {
    title: "指标计算",
    step: "步骤 6",
    min: 10,
    op: "手工计算利润率并交叉核对",
    lines: [
      "SELECT ROUND(SUM(profit) / SUM(sales), 4)",
      "       AS margin",
      "FROM orders",
    ],
  },
  {
    title: "跨表关联",
    step: "步骤 7",
    min: 10,
    op: "LEFT JOIN 退货表算退货率",
    lines: [
      "SELECT o.sub_category,",
      "       COUNT(r.order_id) * 1.0 /",
      "        COUNT(o.order_id) AS return_rate",
      "FROM orders o",
      "LEFT JOIN returns r",
      "  ON o.order_id = r.order_id",
      "GROUP BY o.sub_category",
    ],
  },
] as const;

/** 70 分钟的去向（article §3） */
const TIME_BLOCKS = [
  { label: "理解", steps: "步骤 1 – 2", min: 10 },
  { label: "调试", steps: "步骤 3 · 5", min: 20 },
  { label: "核对", steps: "步骤 4 · 6", min: 15 },
  { label: "成文", steps: "步骤 7 · 8", min: 25 },
] as const;

/* ── SQL 语法高亮：把一行拆成 关键字 / 字符串 / 数字 / 函数 / 普通文本 ── */

type TokKind = "kw" | "str" | "num" | "fn" | "txt";
interface Tok {
  t: string;
  k: TokKind;
}

const SQL_TOKEN =
  /('[^']*'|\b\d+(?:\.\d+)?\b|\b(?:SELECT|FROM|WHERE|GROUP|BY|ORDER|AS|CASE|WHEN|THEN|ELSE|END|LEFT|JOIN|ON|ROUND|SUM|COUNT|AVG|DESC|ASC|AND|WITH|OVER|PARTITION)\b)/gi;

function tokenize(line: string): Tok[] {
  const out: Tok[] = [];
  let last = 0;
  for (const m of line.matchAll(SQL_TOKEN)) {
    const i = m.index ?? 0;
    if (i > last) out.push({ t: line.slice(last, i), k: "txt" });
    const tok = m[0];
    const k: TokKind = tok.startsWith("'")
      ? "str"
      : /^\d/.test(tok)
        ? "num"
        : /^(ROUND|SUM|COUNT|AVG)$/i.test(tok)
          ? "fn"
          : "kw";
    out.push({ t: tok, k });
    last = i + tok.length;
  }
  if (last < line.length) out.push({ t: line.slice(last), k: "txt" });
  return out;
}

const vp = (v: Record<string, string | number>) => v as CSSProperties;

/* ── 数字翻动（纯 CSS 里程表：transform 滚动，无定时器） ── */

const ODO_REPS = 2;
const ODO_SEQ: number[] = [];
for (let r = 0; r < ODO_REPS; r++) {
  for (let d = 0; d <= 9; d++) ODO_SEQ.push(d);
}
const ODO_MAX_INDEX = ODO_SEQ.length - 1;

function Odometer({
  value,
  duration = 1500,
  delay = 250,
  className = "",
}: {
  value: number;
  duration?: number;
  delay?: number;
  className?: string;
}) {
  const digits = String(value)
    .split("")
    .map((d) => Number(d));
  const withComma = digits.length > 3;

  return (
    <span className={`bl-odo${className ? ` ${className}` : ""}`} aria-label={String(value)}>
      <span className="bl-odo-vis" aria-hidden="true">
        {digits.map((target, i) => {
          // 停在最后一轮重复里的目标数字上
          const index = (ODO_REPS - 1) * 10 + target;
          const dur = Math.round((duration * (index + 1)) / (ODO_MAX_INDEX + 1));
          return (
            <span className="bl-odo-unit" key={i}>
              <span className="bl-odo-col">
                <span
                  className="bl-odo-strip"
                  style={vp({ "--index": index, "--dur": `${dur}ms`, "--delay": `${delay}ms` })}
                >
                  {ODO_SEQ.map((d, j) => (
                    <span className="bl-odo-digit" key={j}>
                      {d}
                    </span>
                  ))}
                </span>
              </span>
              {withComma && i === 0 ? <span className="bl-odo-sep">,</span> : null}
            </span>
          );
        })}
      </span>
    </span>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 0 — 真实场景：连锁零售经营分析
 * ═════════════════════════════════════════════════════════════════════ */
function SceneScenario() {
  return (
    <div className="bl-scene">
      <div className="bl-kicker kicker">场景 · 连锁零售经营分析</div>
      <h1 className="bl-h1 serif-cn">
        一个真实场景：
        <span className="bl-em">连锁零售</span>的经营分析。
      </h1>

      <div className="bl-flow" aria-hidden="true">
        <div className="bl-flow-tables">
          {TABLES.map((t, i) => (
            <div className="bl-flow-row" key={t.name}>
              <div className="bl-tbl" style={vp({ "--i": i })}>
                <span className="bl-tbl-name">{t.name}</span>
                <span className="bl-tbl-meta">{t.rows}</span>
                <span className="bl-tbl-meta">{t.meta}</span>
              </div>
              <span className="bl-link" style={vp({ "--i": i })} />
            </div>
          ))}
        </div>
        <div className="bl-flow-out">
          <span className="bl-out-label serif-cn">经营分析</span>
          <span className="bl-out-sub">跨表关联</span>
        </div>
      </div>

      <p className="bl-foot">公开 Tableau Superstore 零售数据集 · 2014 – 2017</p>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 1 — 数据规模：9,994 笔订单 / 3 张表（数字翻动）
 * ═════════════════════════════════════════════════════════════════════ */
function SceneScale() {
  return (
    <div className="bl-scene">
      <div className="bl-kicker kicker">数据规模</div>
      <h2 className="bl-h2 serif-cn">
        九千九百九十四笔订单，<span className="bl-em">三张表</span>。
      </h2>

      <div className="bl-stats">
        <div className="bl-stat">
          <Odometer value={9994} duration={1500} delay={250} />
          <span className="bl-stat-label">笔真实订单</span>
        </div>
        <span className="bl-stat-div" aria-hidden="true" />
        <div className="bl-stat">
          <Odometer value={3} duration={760} delay={1250} className="bl-odo-accent" />
          <span className="bl-stat-label">张数据表</span>
        </div>
      </div>

      <p className="bl-foot">21 个字段 · 订单 / 日期 / 区域 / 品类 / 子品类 / 销售额 / 数量 / 折扣 / 利润</p>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 2 — 业务问题：哪些子品类在亏损，为什么？（扫描 + 划线）
 * ═════════════════════════════════════════════════════════════════════ */
function SceneQuestion() {
  return (
    <div className="bl-scene">
      <div className="bl-kicker kicker">业务问题</div>

      <div className="bl-q-wrap">
        <span className="bl-scan" aria-hidden="true" />
        <h2 className="bl-q serif-cn">
          哪些子品类在<span className="bl-q-em">亏损</span>，
          <span className="bl-q-why">为什么</span>？
        </h2>
      </div>

      <p className="bl-foot">一次经营分析，从这个问题开始</p>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 3 — 人工基线：8 步 SOP（逐步揭示）
 * ═════════════════════════════════════════════════════════════════════ */
function SceneSteps() {
  return (
    <div className="bl-scene">
      <span className="bl-watermark hero-num" aria-hidden="true">
        08
      </span>
      <div className="bl-kicker kicker">人工基线 · 标准作业流程</div>
      <h2 className="bl-h2 serif-cn">
        人工做这件事，要<span className="bl-em">八步</span>。
      </h2>

      <div className="bl-steps">
        {SOP.map((s, i) => (
          <div className="bl-step" key={s.name} style={vp({ "--i": i })}>
            <span className="bl-step-no hero-num">{String(i + 1).padStart(2, "0")}</span>
            <span className="bl-step-name serif-cn">{s.name}</span>
            <span className="bl-step-op">{s.op}</span>
            <span className="bl-step-min">{s.min} 分钟</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 4 — 四段 SQL（代码卡片 + 逐行打出）
 * ═════════════════════════════════════════════════════════════════════ */
function SceneSql() {
  return (
    <div className="bl-scene">
      <div className="bl-kicker kicker">步骤 3 – 7 · 重活</div>
      <h2 className="bl-h2 serif-cn">
        写<span className="bl-em">四段 SQL</span>，跨表关联，算指标，写报告。
      </h2>

      <div className="bl-sql-grid">
        {SQL_CARDS.map((c, i) => (
          <div className="bl-sql" key={c.title} style={vp({ "--i": i })}>
            <div className="bl-sql-head">
              <span className="bl-sql-title serif-cn">{c.title}</span>
              <span className="bl-sql-min">{c.min} 分钟</span>
            </div>
            <div className="bl-sql-code">
              {c.lines.map((ln, j) => (
                <span className="bl-sql-line" key={j} style={vp({ "--j": j })}>
                  {tokenize(ln).map((t, k) => (
                    <span className={`bl-tok bl-tok-${t.k}`} key={k}>
                      {t.t}
                    </span>
                  ))}
                </span>
              ))}
              <span className="bl-caret" aria-hidden="true" />
            </div>
            <div className="bl-sql-op">{c.step} · {c.op}</div>
          </div>
        ))}
      </div>

      <p className="bl-foot">四段 SQL 共 40 分钟 · 另加识别亏损 5 分钟与报告撰写 15 分钟</p>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 5 — 机器执行：8 步瞬间跑完 + 秒表环
 * ═════════════════════════════════════════════════════════════════════ */
function SceneMachine() {
  return (
    <div className="bl-scene">
      <div className="bl-kicker kicker">机器执行 · 同样的八步</div>
      <h2 className="bl-h2 serif-cn">
        机器跑完这些，<span className="bl-em">不到一秒</span>。
      </h2>

      <div className="bl-machine">
        <div className="bl-timer">
          <svg className="bl-timer-svg" viewBox="0 0 200 200" aria-hidden="true">
            <circle className="bl-timer-track" cx="100" cy="100" r="92" />
            <circle className="bl-timer-arc" cx="100" cy="100" r="92" />
          </svg>
          <span className="bl-timer-val hero-num">&lt; 1 s</span>
        </div>

        <ol className="bl-nodes">
          {SOP.map((s, i) => (
            <li className="bl-node" key={s.name} style={vp({ "--i": i })}>
              <span className="bl-node-box" aria-hidden="true" />
              <span className="bl-node-name serif-cn">{s.name}</span>
            </li>
          ))}
        </ol>
      </div>

      <p className="bl-foot">8 步全部跑完 · 含 5 段 SQL 查询</p>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 6 — 同一件事两种耗时：机器 &lt; 1 秒 vs 人工 70 分钟（对比横条）
 * ═════════════════════════════════════════════════════════════════════ */
function SceneHuman() {
  return (
    <div className="bl-scene">
      <div className="bl-kicker kicker">同一件事 · 人工端到端</div>
      <h2 className="bl-h2 serif-cn">
        但人工端到端，大约<span className="bl-em">七十分钟</span>。
      </h2>

      <div className="bl-cmp">
        <div className="bl-cmp-row">
          <span className="bl-cmp-key">机器执行</span>
          <span className="bl-cmp-track bl-cmp-track-machine">
            <span className="bl-cmp-fill bl-cmp-fill-machine" />
          </span>
          <span className="bl-cmp-val hero-num">&lt; 1 s</span>
        </div>

        <div className="bl-cmp-row">
          <span className="bl-cmp-key">人工端到端</span>
          <span className="bl-cmp-track bl-cmp-track-human">
            {SOP.map((s, i) => (
              <span
                className="bl-cmp-seg"
                key={s.name}
                style={vp({ "--w": `${(s.min / SOP_TOTAL) * 100}%`, "--i": i })}
              >
                <span className="bl-cmp-min">{s.min >= 10 ? `${s.min}` : ""}</span>
              </span>
            ))}
          </span>
          <span className="bl-cmp-val bl-cmp-val-human hero-num">70 min</span>
        </div>
      </div>

      <p className="bl-gap serif-cn">差距在哪？</p>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════
 * 场景 7 — 耗时段：理解 / 调试 / 核对 / 成文（比例块）
 * ═════════════════════════════════════════════════════════════════════ */
function SceneTime() {
  return (
    <div className="bl-scene">
      <div className="bl-kicker kicker">70 分钟的去向</div>
      <h2 className="bl-h2 serif-cn">
        全在<span className="bl-em">理解、调试、核对、成文</span>上。
      </h2>

      <div className="bl-blocks">
        {TIME_BLOCKS.map((b, i) => (
          <div
            className="bl-block"
            key={b.label}
            style={vp({ "--w": `${(b.min / SOP_TOTAL) * 100}%`, "--i": i })}
          >
            <span className="bl-block-label serif-cn">{b.label}</span>
            <span className="bl-block-min hero-num">
              {b.min}
              <span className="bl-block-unit">min</span>
            </span>
            <span className="bl-block-steps">{b.steps}</span>
          </div>
        ))}
      </div>

      <p className="bl-foot">70 分钟里，几乎没花在“跑数”上</p>
    </div>
  );
}

/* ═════════════════════════════════════════════════════════════════════ */

export default function Baseline({ step }: ChapterStepProps) {
  if (step === 0) return <SceneScenario />;
  if (step === 1) return <SceneScale />;
  if (step === 2) return <SceneQuestion />;
  if (step === 3) return <SceneSteps />;
  if (step === 4) return <SceneSql />;
  if (step === 5) return <SceneMachine />;
  if (step === 6) return <SceneHuman />;
  if (step === 7) return <SceneTime />;
  // 越界兜底（stepper 正常不会走到）：退回最后一步
  return <SceneTime />;
}
