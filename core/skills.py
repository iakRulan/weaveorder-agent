"""Agent Skills 加载器（agentskills.io 开放标准的轻量实现）。

扫描 ``skills/<name>/SKILL.md``，解析 YAML frontmatter（只支持顶层键值对，
不引入任何第三方依赖，保持 Python 3.9+ 兼容与零重依赖的提交约束）。

用途：
- 自主 Agent 模式的系统提示注入（``skill_context``）；
- A2A AgentCard 的 ``skills`` 数组（``Skill.to_card``）；
- 外部 harness（Claude Code 等）可直接读取同一份 SKILL.md。

规范要点（agentskills.io/specification）：
- SKILL.md = YAML frontmatter + Markdown 正文；
- ``name`` 仅小写字母/数字/连字符，最长 64 字符，需与目录名一致；
- ``description`` 说明“做什么、何时用”，供 Agent 路由判断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    meta: Dict[str, str] = field(default_factory=dict)

    def to_card(self) -> Dict[str, Any]:
        """转成 A2A AgentCard 的 AgentSkill 结构。"""
        tags = [t.strip() for t in (self.meta.get("tags") or "skill").split(",") if t.strip()]
        return {
            "id": self.name,
            "name": self.meta.get("display_name") or self.name,
            "description": self.description[:1000],
            "tags": tags[:8],
            "examples": [
                line.strip("- ").strip()
                for line in self.body.splitlines()
                if line.strip().startswith("- ") and len(line) < 120
            ][:3],
        }


def _parse_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: Dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        if line[:1].isspace():  # 嵌套结构暂不进 meta
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, parts[2].lstrip("\n")


def load_skill(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or path.parent.name
    if not NAME_RE.match(name):
        return None
    return Skill(
        name=name,
        description=meta.get("description", ""),
        body=body,
        path=path,
        meta=meta,
    )


def list_skills() -> List[Skill]:
    if not SKILLS_DIR.is_dir():
        return []
    skills: List[Skill] = []
    for directory in sorted(SKILLS_DIR.iterdir()):
        skill_md = directory / "SKILL.md"
        if not skill_md.is_file():
            continue
        skill = load_skill(skill_md)
        if skill is not None:
            skills.append(skill)
    return skills


def skill_context(names: List[str] | None = None, max_chars: int = 1200) -> str:
    """把技能摘要拼成提示词片段；超长时按顺序截断。"""
    digest: List[str] = []
    used = 0
    for skill in list_skills():
        if names and skill.name not in names:
            continue
        block = f"### {skill.name}\n{skill.description}\n"
        body = re.sub(r"\n{2,}", "\n", skill.body)
        body = re.sub(r"^#.*$", "", body, flags=re.MULTILINE)  # 去掉正文大标题
        quota = max(0, max_chars - used - len(block))
        if quota <= 0:
            break
        digest.append(block + body[:quota].strip())
        used += len(digest[-1]) + 2
    return "\n\n".join(digest).strip()
