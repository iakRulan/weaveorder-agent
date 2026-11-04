import type { Narration } from "../../registry/types";

/**
 * 第 1 章 · hook — 八十分钟对一点五秒（6 steps）
 *
 * 数组长度 === 章节 step 数；index i === `step === i` 的口播文本。
 * 音频合成与 Auto 模式都以本文件为唯一真相源。
 */
export const narrations: Narration[] = [
  // step 0 — 双数字对撞
  "三万户客户的风险分析，人工要八十分钟。",
  // step 1 — 反差定格
  "我们把它压到了一点五秒。",
  // step 2 — 为什么慢
  "一笔授信要看二十五个字段。",
  // step 3 — 字段四类
  "额度、六期还款状态、六期账单、还款额。",
  // step 4 — 信号分散
  "风险信号散在多期行为里，靠经验容易漏。",
  // step 5 — 算错无人发现
  "更麻烦的是，人工算错的数字没人发现。",
];
