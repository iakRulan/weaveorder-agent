import type { ChapterStepProps } from "../../registry/types";
import "./Trust.css";

/**
 * 第 5 章 · trust — 不止快，还要可信。
 *
 * 九步：论断 → 错误注入 → 校验器开启（拦截）→ 两个数字对不上 → 报告不发布
 *        → 校验器关闭（消融对照，错误发布）→ 同一引擎换工具 → 三个场景 → 落款。
 * 每步一个独占整屏的场景；颜色/字体全部走主题 token，动画用 CSS keyframes。
 */

/* ── 共用小件 ──────────────────────────────────────────────────────── */

/** 校验器开关 —— 纯展示，状态由所在 step 决定（开 / 关两条对照路径） */
function ValidatorSwitch({ on }: { on: boolean }) {
  return (
    <div className={`tr-switch ${on ? "is-on" : "is-off"}`} aria-hidden="true">
      <span className="tr-switch-track">
        <span className="tr-switch-thumb" />
      </span>
      <span className="tr-switch-label tr-label">{on ? "校验器 · 开" : "校验器 · 关"}</span>
    </div>
  );
}

/** 校验器闸门 —— 开时闸门落下（拦截），关时闸门升起（放行） */
function Gate({ open }: { open: boolean }) {
  return (
    <div className="tr-gate" aria-hidden="true">
      <span className="tr-label">确定性校验</span>
      <div className="tr-gate-track">
        <div className={`tr-gate-beam ${open ? "is-open" : ""}`} />
      </div>
      <svg className="tr-gate-icon" viewBox="0 0 32 32">
        <path
          d="M16 3 L27 7 V16 C27 23 22 27.5 16 29 C10 27.5 5 23 5 16 V7 Z"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="2"
          strokeLinejoin="round"
        />
        {open ? (
          <polyline
            points="10.5,16 14.5,20 22,11.5"
            fill="none"
            stroke="var(--accent)"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ) : (
          <line
            x1="10.5"
            y1="21.5"
            x2="21.5"
            y2="10.5"
            stroke="var(--accent)"
            strokeWidth="2"
            strokeLinecap="round"
          />
        )}
      </svg>
    </div>
  );
}

/** 报告单 —— 承载被注入错误的关键指标 */
function ReportCard({
  value,
  tag,
  cracked = false,
}: {
  value: string;
  tag: string;
  cracked?: boolean;
}) {
  return (
    <div className="tr-report card">
      <div className="tr-report-top">
        <span className="tr-label">报告 · 经营分析</span>
        <span className="tr-report-tag tr-label">{tag}</span>
      </div>
      <div className="rule tr-report-rule" />
      <div className="tr-report-metric">
        <span className="tr-report-metric-name serif-cn">利润率</span>
        <span className="tr-report-metric-value hero-num">{value}</span>
      </div>
      <div className="tr-report-rows" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      {cracked ? (
        <svg
          className="tr-crack"
          viewBox="0 0 240 200"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <polyline
            points="30,6 78,52 60,78 128,122 112,150"
            fill="none"
            stroke="var(--accent)"
            strokeWidth="3"
            strokeLinejoin="round"
          />
          <polyline
            points="168,4 182,42 166,66 218,108 206,140"
            fill="none"
            stroke="var(--accent)"
            strokeWidth="2.4"
            strokeLinejoin="round"
            opacity="0.7"
          />
        </svg>
      ) : null}
    </div>
  );
}

/* ── step 0 · 它不止快 ────────────────────────────────────────────── */

function Scene0() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">第 5 章 · 可信</div>
      <h1 className="tr-h1 serif-cn">
        它不止<span className="tr-em">快</span>。
      </h1>
      <div className="tr-scale" aria-hidden="true">
        <svg viewBox="0 0 560 300">
          <line x1="280" y1="52" x2="280" y2="248" stroke="var(--rule)" strokeWidth="2" />
          <line x1="212" y1="248" x2="348" y2="248" stroke="var(--rule)" strokeWidth="2" />
          <circle cx="280" cy="90" r="5" fill="var(--accent)" />
          <g className="tr-scale-beam">
            <line
              x1="104"
              y1="90"
              x2="456"
              y2="90"
              stroke="var(--text)"
              strokeWidth="3"
              strokeLinecap="round"
            />
          </g>
          <g className="tr-scale-pan tr-scale-pan-l">
            <line x1="112" y1="90" x2="78" y2="168" stroke="var(--rule)" strokeWidth="2" />
            <line x1="112" y1="90" x2="146" y2="168" stroke="var(--rule)" strokeWidth="2" />
            <path
              d="M 72 168 A 40 24 0 0 0 152 168 Z"
              fill="var(--surface-3)"
              stroke="var(--rule)"
              strokeWidth="2"
            />
          </g>
          <g className="tr-scale-pan tr-scale-pan-r">
            <line x1="448" y1="90" x2="414" y2="168" stroke="var(--rule)" strokeWidth="2" />
            <line x1="448" y1="90" x2="482" y2="168" stroke="var(--rule)" strokeWidth="2" />
            <path
              d="M 408 168 A 40 24 0 0 0 488 168 Z"
              fill="var(--surface-3)"
              stroke="var(--rule)"
              strokeWidth="2"
            />
          </g>
        </svg>
        <div className="tr-scale-labels">
          <span className="tr-scale-label">快</span>
          <span className="tr-scale-label tr-scale-label-heavy">可信</span>
        </div>
      </div>
      <p className="tr-sub serif-cn">还要可信。</p>
    </div>
  );
}

/* ── step 1 · 错误注入实验 ────────────────────────────────────────── */

function Scene1() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">错误注入实验</div>
      <h2 className="tr-h2 serif-cn">
        把利润率<span className="tr-em">算错一半</span>
      </h2>
      <div className="tr-inject">
        <div className="tr-fact card">
          <span className="tr-label">数据源真值</span>
          <span className="tr-fact-value hero-num">12.47%</span>
          <span className="tr-fact-note tr-label">利润率 · 不变</span>
        </div>
        <div className="tr-inject-arrow" aria-hidden="true">
          <span className="tr-inject-badge tr-label">错误注入 × 0.5</span>
          <svg viewBox="0 0 200 24">
            <line
              x1="0"
              y1="12"
              x2="176"
              y2="12"
              stroke="var(--accent)"
              strokeWidth="2.5"
              pathLength={100}
              className="tr-draw"
            />
            <polyline
              points="168,4 184,12 168,20"
              fill="none"
              stroke="var(--accent)"
              strokeWidth="2.5"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <ReportCard value="6.235%" tag="注入后" cracked />
      </div>
    </div>
  );
}

/* ── step 2 · 校验器开启：拦截 ────────────────────────────────────── */

function Scene2() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">路径 A · 校验器开启</div>
      <ValidatorSwitch on />
      <div className="tr-gate-row">
        <ReportCard value="6.235%" tag="failed" cracked />
        <Gate open={false} />
        <div className="tr-publish card tr-publish-blocked">
          <span className="tr-publish-name serif-cn">发布</span>
          <span className="tr-label">blocked</span>
        </div>
      </div>
      <div className="tr-stamp">已拦截</div>
      <h3 className="tr-h3 serif-cn">错误数字，当场被拦截。</h3>
    </div>
  );
}

/* ── step 3 · 两个数字对不上 ──────────────────────────────────────── */

function Scene3() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">拦截依据</div>
      <div className="tr-vs">
        <div className="tr-vs-col">
          <span className="tr-label">报告值</span>
          <span className="tr-vs-num hero-num tr-bad">6.235%</span>
        </div>
        <div className="tr-vs-mark serif-cn" aria-hidden="true">
          ≠
        </div>
        <div className="tr-vs-col">
          <span className="tr-label">重算值</span>
          <span className="tr-vs-num hero-num">12.47%</span>
        </div>
      </div>
      <div className="tr-stamp tr-stamp-lg">直接拦下</div>
      <p className="tr-note serif-cn">同一指标，两条路径算不出同一个数。</p>
    </div>
  );
}

/* ── step 4 · 报告不发布 ──────────────────────────────────────────── */

function Scene4() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">路径 A · 结果</div>
      <div className="tr-pipe" aria-hidden="true">
        <span className="tr-pipe-node serif-cn">报告</span>
        <span className="tr-pipe-link" />
        <span className="tr-pipe-node tr-pipe-node-ok serif-cn">校验</span>
        <span className="tr-pipe-link tr-pipe-link-block" />
        <span className="tr-pipe-node tr-pipe-node-block serif-cn">发布</span>
      </div>
      <h2 className="tr-h2 serif-cn">
        报告<span className="tr-em">不发布</span>。
      </h2>
      <div className="tr-status tr-label">status · failed</div>
      <p className="tr-note serif-cn">人工达到同等保障，需额外交叉核对约 10 分钟。</p>
    </div>
  );
}

/* ── step 5 · 校验器关闭：错误直接发布（消融对照） ────────────────── */

function Scene5() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">路径 B · 校验器关闭</div>
      <ValidatorSwitch on={false} />
      <div className="tr-gate-row">
        <ReportCard value="6.235%" tag="published" cracked />
        <Gate open />
        <div className="tr-publish card tr-publish-ok">
          <span className="tr-publish-name serif-cn">发布</span>
          <span className="tr-label">published</span>
        </div>
      </div>
      <div className="tr-ablation">
        <span className="tr-ablation-bad hero-num">+6.2%</span>
        <span className="tr-ablation-sep tr-label">已发布</span>
        <span className="tr-ablation-good hero-num">+12.5%</span>
        <span className="tr-ablation-sep tr-label">正确值</span>
      </div>
      <h3 className="tr-h3 serif-cn">错误数字，直接发出去了。</h3>
    </div>
  );
}

/* ── step 6 · 同一套引擎，换一套工具 ──────────────────────────────── */

function Scene6() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">可复制性</div>
      <h2 className="tr-h2 serif-cn">
        同一套引擎，<span className="tr-em">换一套工具</span>。
      </h2>
      <div className="tr-engine">
        <div className="tr-core card">
          <span className="tr-label">引擎原语 · 不变</span>
          <div className="rule tr-core-rule" />
          <ul className="tr-core-list">
            <li className="serif-cn">工具注册表</li>
            <li className="serif-cn">事件溯源</li>
            <li className="serif-cn">确定性校验</li>
            <li className="serif-cn">产物布局</li>
          </ul>
        </div>
        <div className="tr-tools">
          <span className="tr-label">工具集 · 可替换</span>
          <div className="tr-tool-slots">
            <div className="tr-tool">
              <span className="tr-tool-line" />
              <span className="tr-tool-chip serif-cn">SQL 查询</span>
            </div>
            <div className="tr-tool">
              <span className="tr-tool-line" />
              <span className="tr-tool-chip serif-cn">差旅 API</span>
            </div>
            <div className="tr-tool">
              <span className="tr-tool-line" />
              <span className="tr-tool-chip serif-cn">报表渲染</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── step 7 · 三个已验证场景 ──────────────────────────────────────── */

function Scene7() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-kicker">三个已验证场景</div>
      <div className="tr-scenarios">
        <div className="tr-scenario card">
          <svg className="tr-glyph" viewBox="0 0 48 48" aria-hidden="true">
            <polyline
              points="13,31 21,17 35,28"
              fill="none"
              stroke="var(--accent)"
              strokeWidth="2.5"
              strokeLinejoin="round"
            />
            <circle cx="10" cy="34" r="4" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
            <circle cx="24" cy="14" r="4" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
            <circle cx="38" cy="30" r="4" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
          </svg>
          <span className="tr-scenario-name serif-cn">差旅编排</span>
          <span className="tr-scenario-tag tr-label">已验证</span>
          <span className="tr-scenario-desc tr-label">跨系统流程协同</span>
        </div>
        <div className="tr-scenario card">
          <svg className="tr-glyph" viewBox="0 0 48 48" aria-hidden="true">
            <line x1="8" y1="38" x2="40" y2="38" stroke="var(--rule)" strokeWidth="2.5" />
            <rect x="12" y="24" width="6" height="14" fill="var(--accent)" />
            <rect x="21" y="16" width="6" height="22" fill="var(--accent)" />
            <rect x="30" y="8" width="6" height="30" fill="var(--accent)" />
          </svg>
          <span className="tr-scenario-name serif-cn">经营分析</span>
          <span className="tr-scenario-tag tr-label">已验证</span>
          <span className="tr-scenario-desc tr-label">五段闭环 · 确定性校验</span>
        </div>
        <div className="tr-scenario card">
          <svg className="tr-glyph" viewBox="0 0 48 48" aria-hidden="true">
            <rect x="9" y="9" width="30" height="30" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
            <line x1="9" y1="19" x2="39" y2="19" stroke="var(--accent)" strokeWidth="2" />
            <line x1="9" y1="29" x2="39" y2="29" stroke="var(--accent)" strokeWidth="2" />
            <line x1="19" y1="9" x2="19" y2="39" stroke="var(--accent)" strokeWidth="2" />
            <line x1="29" y1="9" x2="29" y2="39" stroke="var(--accent)" strokeWidth="2" />
          </svg>
          <span className="tr-scenario-name serif-cn">零售分析</span>
          <span className="tr-scenario-tag tr-label">已验证</span>
          <span className="tr-scenario-desc tr-label">9,994 笔订单 · 7/7 通过</span>
        </div>
      </div>
      <p className="tr-note serif-cn">同一套引擎原语，只替换工具集与数据源。</p>
    </div>
  );
}

/* ── step 8 · 收束金句 + 落款 ─────────────────────────────────────── */

function Scene8() {
  return (
    <div className="tr-scene scene-pad">
      <div className="tr-path" aria-hidden="true">
        <span className="tr-path-node" />
        <svg
          className="tr-path-line"
          viewBox="0 0 200 20"
          preserveAspectRatio="none"
          vectorEffect="non-scaling-stroke"
        >
          <line
            x1="0"
            y1="10"
            x2="200"
            y2="10"
            stroke="var(--accent)"
            strokeWidth="2.5"
            pathLength={100}
            className="tr-draw"
            vectorEffect="non-scaling-stroke"
          />
        </svg>
        <span className="tr-path-node tr-path-node-to" />
      </div>
      <h2 className="tr-quote serif-cn">从单点辅助，到流程智能。</h2>
      <p className="tr-sub serif-cn">织序，让企业 AI 敢用、愿用。</p>
      <div className="tr-brand">
        <div className="rule tr-brand-rule" />
        <div className="tr-brand-row">
          <span className="tr-brand-cn serif-cn">织序</span>
          <span className="tr-brand-en">WeaveOrder</span>
        </div>
      </div>
    </div>
  );
}

/* ── 章节入口 ─────────────────────────────────────────────────────── */

export default function Trust({ step }: ChapterStepProps) {
  if (step === 0) return <Scene0 />;
  if (step === 1) return <Scene1 />;
  if (step === 2) return <Scene2 />;
  if (step === 3) return <Scene3 />;
  if (step === 4) return <Scene4 />;
  if (step === 5) return <Scene5 />;
  if (step === 6) return <Scene6 />;
  if (step === 7) return <Scene7 />;
  if (step === 8) return <Scene8 />;
  return null;
}
