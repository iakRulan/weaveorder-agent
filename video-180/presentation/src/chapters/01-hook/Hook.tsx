import { MaskReveal } from "../../components/MaskReveal";
import type { ChapterStepProps } from "../../registry/types";
import "./Hook.css";

/**
 * 第 1 章 · hook — 八十分钟对一点五秒。
 *
 * 六步：双数字对撞 → 1.5s 定格 → 为什么慢 → 字段四类 → 信号分散 → 算错无人发现。
 * 全部走主题 token，无硬编码色 / 字体。
 */
const FIELDS = [
  { k: "额度", v: "能借多少", en: "LIMIT" },
  { k: "还款状态 ×6", v: "有没有逾期", en: "PAY STATUS" },
  { k: "账单金额 ×6", v: "花了多少", en: "BILL AMOUNT" },
  { k: "还款金额 ×6", v: "还了多少", en: "PAYMENT" },
];

const MONTHS = ["2005-04", "2005-05", "2005-06", "2005-07", "2005-08", "2005-09"];

export default function Hook({ step }: ChapterStepProps) {
  /* step 0 — 80 min vs 1.5 s */
  if (step === 0) {
    return (
      <div className="hk-scene scene-pad">
        <div className="kicker">智能信贷风控 · 一次真实运行的对照</div>
        <div className="hk-duel">
          <div className="hk-side">
            <MaskReveal show duration={900}>
              <span className="hero-num hk-big">80</span>
            </MaskReveal>
            <div className="hk-unit label-mono">
              <span>MINUTES</span>
              <span className="hk-who">人工分析</span>
            </div>
          </div>
          <div className="hk-divider">
            <span className="hk-rule rule" />
            <span className="hk-vs label-mono">VS</span>
          </div>
          <div className="hk-side hk-side-new">
            <MaskReveal show delay={420} duration={900}>
              <span className="hero-num hk-big hk-accent">1.5</span>
            </MaskReveal>
            <div className="hk-unit label-mono">
              <span>SECONDS</span>
              <span className="hk-who">织序智能体</span>
            </div>
          </div>
        </div>
        <div className="hk-foot label-mono">同一批三万户授信客户 · 同一个分析问题</div>
      </div>
    );
  }

  /* step 1 — 1.5s 定格 */
  if (step === 1) {
    return (
      <div className="hk-scene scene-pad hk-center">
        <div className="kicker">同一批客户 · 同一个问题</div>
        <div className="hk-huge-wrap">
          <span className="hero-num hk-huge">
            1.5<span className="hk-huge-unit">s</span>
          </span>
          <span className="hk-huge-rule rule-accent" />
        </div>
        <div className="hk-caption">
          端到端耗时 · 含八次工具调用与一次大模型理解
        </div>
      </div>
    );
  }

  /* step 2 — 为什么慢 */
  if (step === 2) {
    return (
      <div className="hk-scene scene-pad hk-center">
        <div className="kicker">为什么人工要八十分钟</div>
        <h2 className="hk-h2">
          <MaskReveal show duration={900}>
            <span className="serif-cn">一笔授信，要看&nbsp;</span>
          </MaskReveal>
          <MaskReveal show delay={420} duration={900}>
            <span className="serif-cn hk-em">二十五个字段</span>
          </MaskReveal>
          <MaskReveal show delay={900} duration={600}>
            <span className="serif-cn">。</span>
          </MaskReveal>
        </h2>
        <div className="hk-h2-sub label-mono">UCI · DEFAULT OF CREDIT CARD CLIENTS</div>
      </div>
    );
  }

  /* step 3 — 字段四类（一个整体构成） */
  if (step === 3) {
    return (
      <div className="hk-scene scene-pad">
        <div className="kicker">二十五个字段，分成四类</div>
        <div className="hk-grid">
          {FIELDS.map((f) => (
            <div className="hk-cell card" key={f.en}>
              <div className="hk-cell-en label-mono">{f.en}</div>
              <div className="hk-cell-k serif-cn">{f.k}</div>
              <div className="hk-cell-v">{f.v}</div>
            </div>
          ))}
        </div>
        <div className="hk-foot label-mono">25 = 1 + 6 + 6 + 6 + 6</div>
      </div>
    );
  }

  /* step 4 — 信号分散 */
  if (step === 4) {
    return (
      <div className="hk-scene scene-pad">
        <div className="kicker">风险信号在哪里</div>
        <div className="hk-timeline">
          <div className="hk-axis">
            <span className="hk-axis-line rule" />
            {MONTHS.map((m, i) => (
              <span className="hk-month label-mono" key={m} style={{ left: `${(i / (MONTHS.length - 1)) * 100}%` }}>
                {m}
              </span>
            ))}
          </div>
          <div className="hk-dots">
            {MONTHS.map((m, i) => (
              <span
                key={m}
                className={"hk-dot" + (i >= 4 ? " hk-dot-hot" : "")}
                style={{ left: `${(i / (MONTHS.length - 1)) * 100}%`, animationDelay: `${i * 260}ms` }}
              />
            ))}
          </div>
          <div className="hk-h2 hk-h2-left">
            <MaskReveal show duration={900}>
              <span className="serif-cn">散在多期行为里，靠经验&nbsp;</span>
            </MaskReveal>
            <MaskReveal show delay={380} duration={900}>
              <span className="serif-cn hk-em">容易漏</span>
            </MaskReveal>
            <MaskReveal show delay={760} duration={500}>
              <span className="serif-cn">。</span>
            </MaskReveal>
          </div>
        </div>
      </div>
    );
  }

  /* step 5 — 算错没人发现 */
  return (
    <div className="hk-scene scene-pad">
      <div className="kicker">更要命的是</div>
      <div className="hk-report card">
        <div className="hk-report-head label-mono">风险分析报告 · 人工填写</div>
        <div className="hk-report-row">
          <span className="hk-rk">整体违约率</span>
          <span className="hk-rv hk-rv-bad">18.6%</span>
          <span className="hk-rx hk-rx-stamp">✕</span>
        </div>
        <div className="hk-report-row">
          <span className="hk-rk">最近逾期两期违约率</span>
          <span className="hk-rv">69.14%</span>
          <span className="hk-rx hk-rx-ok">✓</span>
        </div>
        <div className="hk-report-row">
          <span className="hk-rk">历史逾期三期以上</span>
          <span className="hk-rv">62.87%</span>
          <span className="hk-rx hk-rx-ok">✓</span>
        </div>
      </div>
      <div className="hk-h2 hk-h2-left hk-h2-tight">
        <MaskReveal show duration={900}>
          <span className="serif-cn">人工算错的数字，&nbsp;</span>
        </MaskReveal>
        <MaskReveal show delay={420} duration={900}>
          <span className="serif-cn hk-em">没人发现</span>
        </MaskReveal>
        <MaskReveal show delay={840} duration={500}>
          <span className="serif-cn">。</span>
        </MaskReveal>
      </div>
    </div>
  );
}
