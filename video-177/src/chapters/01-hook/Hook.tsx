import type { ChapterStepProps } from "../../registry/types";
import "./Hook.css";

/**
 * 第 1 章 · hook — 单点试用，办不了事。
 *
 * 六步：开场判断 → 反差 → 引出三处问题 → 三处并列 → 数据分散 → 流程断点。
 * 每步一个独占整屏的场景；颜色/字体全部走主题 token，动画用 CSS keyframes。
 */
export default function Hook({ step }: ChapterStepProps) {
  if (step === 0) {
    return (
      <div className="hk-scene scene-pad">
        <div className="hk-kicker kicker">企业 AI 现状</div>
        <h1 className="hk-h1 serif-cn">
          企业 AI，大多停在
          <span className="hk-em">单点试用</span>。
        </h1>
        <p className="hk-sub serif-cn">能问答，不能办事。</p>
        <div className="hk-tags">
          <span className="hk-tag">文案生成</span>
          <span className="hk-tag">知识问答</span>
          <span className="hk-tag">图像创作</span>
        </div>
      </div>
    );
  }

  if (step === 1) {
    return (
      <div className="hk-scene scene-pad">
        <div className="hk-vs">
          <div className="hk-vs-row">
            <span className="hk-vs-key">听起来</span>
            <span className="hk-vs-val hk-good serif-cn">很聪明</span>
          </div>
          <div className="hk-vs-row">
            <span className="hk-vs-key">用起来</span>
            <span className="hk-vs-val hk-bad serif-cn">很鸡肋</span>
          </div>
        </div>
      </div>
    );
  }

  if (step === 2) {
    return (
      <div className="hk-scene scene-pad">
        <div className="hk-kicker kicker">问题出在哪里</div>
        <h2 className="hk-h2 serif-cn">
          问题出在<span className="hk-em">三处</span>。
        </h2>
      </div>
    );
  }

  if (step === 3) {
    return (
      <div className="hk-scene scene-pad">
        <div className="hk-trio">
          <div className="hk-trio-item">
            <span className="hk-trio-no hero-num">01</span>
            <span className="hk-trio-txt serif-cn">系统难连接</span>
          </div>
          <div className="hk-trio-item">
            <span className="hk-trio-no hero-num">02</span>
            <span className="hk-trio-txt serif-cn">流程难闭环</span>
          </div>
          <div className="hk-trio-item">
            <span className="hk-trio-no hero-num">03</span>
            <span className="hk-trio-txt serif-cn">价值难量化</span>
          </div>
        </div>
      </div>
    );
  }

  if (step === 4) {
    return (
      <div className="hk-scene scene-pad">
        <div className="hk-kicker kicker">第一处 · 系统难连接</div>
        <h3 className="hk-h3 serif-cn">数据散在很多张表里</h3>
        <div className="hk-tables" aria-hidden>
          <div className="hk-tbl hk-tbl-1">
            <span className="hk-tbl-cap label-mono">orders</span>
            <span className="hk-tbl-row" />
            <span className="hk-tbl-row" />
          </div>
          <div className="hk-tbl hk-tbl-2">
            <span className="hk-tbl-cap label-mono">returns</span>
            <span className="hk-tbl-row" />
            <span className="hk-tbl-row" />
          </div>
          <div className="hk-tbl hk-tbl-3">
            <span className="hk-tbl-cap label-mono">people</span>
            <span className="hk-tbl-row" />
            <span className="hk-tbl-row" />
          </div>
        </div>
        <p className="hk-note serif-cn">得人工跨表关联</p>
      </div>
    );
  }

  return (
    <div className="hk-scene scene-pad">
      <div className="hk-kicker kicker">第二处 · 流程难闭环</div>
      <h3 className="hk-h3 serif-cn">从提问题到拿结论</h3>
      <div className="hk-chain" aria-hidden>
        <span className="hk-node hk-node-ok">提问</span>
        <span className="hk-link hk-link-ok" />
        <span className="hk-node hk-node-ok">取数</span>
        <span className="hk-link hk-link-break" />
        <span className="hk-node">清洗</span>
        <span className="hk-link hk-link-break" />
        <span className="hk-node">统计</span>
        <span className="hk-link hk-link-break" />
        <span className="hk-node">核对</span>
        <span className="hk-link hk-link-break" />
        <span className="hk-node">成文</span>
      </div>
      <p className="hk-note serif-cn">中间全是断点</p>
    </div>
  );
}
