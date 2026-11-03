import type { Narration } from "../../registry/types";

/**
 * 第 1 章 · hook — 单点试用，办不了事（6 steps）
 *
 * 数组长度 === 章节 step 数；index i === `step === i` 的口播文本。
 * 音频合成与 Auto 模式都以本文件为唯一真相源。
 */
export const narrations: Narration[] = [
  // step 0 — 开场判断
  "今天的企业 AI，大多停在单点试用。能问答，不能办事。",
  // step 1 — 反差
  "听起来很聪明，用起来很鸡肋。",
  // step 2 — 引出三处问题
  "问题出在三处。",
  // step 3 — 三处问题并列
  "系统难连接，流程难闭环，价值难量化。",
  // step 4 — 数据分散
  "数据散在很多张表里，得人工跨表关联。",
  // step 5 — 流程断点
  "从提问题到拿结论，中间全是断点。",
];
