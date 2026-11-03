import type { ChapterDef } from "./types";
import HookChapter from "../chapters/01-hook/Hook";
import { narrations as hookNarrations } from "../chapters/01-hook/narrations";
import BaselineChapter from "../chapters/02-baseline/Baseline";
import { narrations as baselineNarrations } from "../chapters/02-baseline/narrations";
import SolutionChapter from "../chapters/03-solution/Solution";
import { narrations as solutionNarrations } from "../chapters/03-solution/narrations";
import RunChapter from "../chapters/04-run/Run";
import { narrations as runNarrations } from "../chapters/04-run/narrations";
import TrustChapter from "../chapters/05-trust/Trust";
import { narrations as trustNarrations } from "../chapters/05-trust/narrations";

/**
 * Order = order of presentation.
 *
 * Each chapter MUST provide a `narrations: Narration[]` array. Its length
 * is the chapter's step count — there is no `totalSteps` to maintain
 * separately. This guarantees the audio synthesis pipeline, the runtime
 * stepper, and the chapter `.tsx` switch on `step` cannot drift apart.
 *
 * Visual styling (color, fonts) comes entirely from the active theme —
 * chapters never hard-code palette / font names. See THEMES.md.
 */
export const CHAPTERS: ChapterDef[] = [
  {
    id: "hook",
    title: "单点试用，办不了事",
    narrations: hookNarrations,
    Component: HookChapter,
  },
  {
    id: "baseline",
    title: "场景与人工基线",
    narrations: baselineNarrations,
    Component: BaselineChapter,
  },
  {
    id: "solution",
    title: "五段闭环与真实运行",
    narrations: solutionNarrations,
    Component: SolutionChapter,
  },
  {
    id: "run",
    title: "结果：亏损归因",
    narrations: runNarrations,
    Component: RunChapter,
  },
  {
    id: "trust",
    title: "不止快，还要可信",
    narrations: trustNarrations,
    Component: TrustChapter,
  },
];
