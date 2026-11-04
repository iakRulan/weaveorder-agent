import type { Narration } from "../../registry/types";

/**
 * 第 5 章 · interface — 真实运行界面（4 steps）
 *
 * 数组长度 === 章节 step 数；index i === `step === i` 的口播文本。
 */
export const narrations: Narration[] = [
  // step 0 — 任务输入与信息理解
  "这是织序的真实运行界面。一次分析，从一句自然语言开始。",
  // step 1 — 流程判断
  "系统先识别意图，再排出确定性的分析流程。每一步调用哪个工具，都清清楚楚。",
  // step 2 — 系统调用与校验
  "所有工具调用和返回结果，全程留痕。报告里的每个数字，都由校验器独立重算。",
  // step 3 — 结果输出
  "最终输出一份可追溯的分析报告。从提问到交付，全在同一个界面里。",
];
