import type { Narration } from "../../registry/types";

/**
 * 第 4 章 · run — 结果：亏损归因（7 steps）
 *
 * 数组长度 === 章节 step 数；index i === `step === i` 的口播文本。
 * 音频合成与 Auto 模式都以本文件为唯一真相源，逐字来自 script.md。
 */
export const narrations: Narration[] = [
  // step 0 — 报告给出答案：桌椅
  "报告直接给你答案。桌椅利润率负百分之八点六。",
  // step 1 — 书柜
  "书柜，负百分之三点零。",
  // step 2 — 提问：折扣超过三成的订单
  "折扣超过三成的订单呢？",
  // step 3 — 答案：负 48.2%
  "利润率负百分之四十八点二。",
  // step 4 — 正对照：不打折的订单
  "不打折的订单，利润率是正的百分之二十九点五。",
  // step 5 — 整体利润率
  "整体利润率是正的，百分之十二点五。",
  // step 6 — 结论
  "亏的是结构，不是大盘。",
];
