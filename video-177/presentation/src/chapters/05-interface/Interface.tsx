import type { ChapterStepProps } from "../../registry/types";
import "./Interface.css";

/**
 * 第 5 章 · interface — 真实运行界面。
 *
 * 四步各展示一张真实界面截图（运行档案页的四个区段），
 * 用缓慢的推近/平移让画面"活"起来，并配一行说明。
 */
const SHOTS = [
  { src: "interface/01-intake.png", cap: "任务输入 · 信息理解", hint: "自然语言需求 → 意图 / 维度 / 指标" },
  { src: "interface/02-plan.png", cap: "流程判断（确定性规划）", hint: "分析 DAG：步骤 · 目标 · 工具 · 依赖" },
  { src: "interface/03-tools.png", cap: "系统调用 · 确定性校验", hint: "工具调用留痕 → 校验器独立重算" },
  { src: "interface/04-report.png", cap: "结果输出（可追溯报告）", hint: "指标 · 明细表 · 结论，全部可回溯" },
];

export default function Interface({ step }: ChapterStepProps) {
  const shot = SHOTS[Math.min(step, SHOTS.length - 1)]!;
  return (
    <div className="if-scene scene-pad">
      <div className="if-kicker kicker">真实运行界面</div>
      <div className="if-frame">
        <img
          key={shot.src}
          className="if-shot if-shot-zoom"
          src={`${import.meta.env.BASE_URL}${shot.src}`}
          alt={shot.cap}
        />
        <div className="if-cap">
          <span className="if-cap-t serif-cn">{shot.cap}</span>
          <span className="if-cap-h">{shot.hint}</span>
        </div>
      </div>
    </div>
  );
}
