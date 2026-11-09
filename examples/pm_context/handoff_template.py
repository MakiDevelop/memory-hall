"""Canonical, bounded handoff chapters shared by every client."""

from __future__ import annotations

import re

from safety import require_safe

SECTIONS = (
    "先做的三件事",
    "角色與規則（只放變動）",
    "機器與腳本位置",
    "進行中的工作",
    "待決事項／待問",
    "已知坑與教訓",
    "哪些是「未驗」的",
    "相關檔案指標",
)
FIRST_SECTION = SECTIONS[0]
FIRST_SECTION_BYTES = 1_536
_CHAPTER = re.compile(r"^##\s+(?:(\d+)[.、）)]\s*)?(.+?)\s*$")
_ACTION = re.compile(r"^(?:[-*+]|\d+[.)、])\s+\S.*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def template() -> str:
    parts = ["# Memory Hall 3.1 交接", ""]
    for index, title in enumerate(SECTIONS):
        parts.extend([f"## {title}", ""])
        if index == 0:
            parts.extend(
                ["1. 待填第一項具體動作。", "2. 待填第二項具體動作。", "3. 待填第三項具體動作。"]
            )
        elif index != 6:
            parts.append("待填，沒有事項時註明「無」。")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def canonicalize(body: str) -> str:
    """Remove one BOM, use LF and one final newline, and preserve content codepoints."""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Handoff body is required.")
    try:
        body.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Handoff must be valid UTF-8 text.") from None
    body = body.removeprefix("\ufeff")
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", body):
        raise ValueError("Handoff contains unsupported control characters.")
    if not body.strip():
        raise ValueError("Handoff body is required.")
    return body.rstrip("\n") + "\n"


def validate_handoff(body: str) -> dict[str, str]:
    """Return exactly eight chapter bodies, including the bounded first three actions."""
    canonical = canonicalize(body)
    require_safe(canonical)
    chapters: dict[str, list[str]] = {}
    current: str | None = None
    fence: str | None = None
    preamble: list[str] = []
    for line in canonical.splitlines():
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        match = _CHAPTER.match(line) if fence is None and marker is None else None
        if match:
            title = match.group(2)
            if len(chapters) >= len(SECTIONS) or title != SECTIONS[len(chapters)]:
                raise ValueError("Handoff must contain the eight required chapters in order.")
            if match.group(1) and int(match.group(1)) != len(chapters) + 1:
                raise ValueError("Handoff chapter numbering must follow the required order.")
            current = title
            chapters[title] = []
        elif current is None:
            preamble.append(line)
        else:
            chapters[current].append(line)
    if tuple(chapters) != SECTIONS or fence is not None:
        raise ValueError("Handoff must contain eight complete chapters and closed code fences.")
    intro = [line for line in preamble if line.strip()]
    if len(intro) > 1 or (intro and not intro[0].startswith("# ")):
        raise ValueError("Only one document title may precede the handoff chapters.")
    sections = {title: "\n".join(lines).strip() for title, lines in chapters.items()}
    if any(not sections[title] for title in SECTIONS if title != SECTIONS[6]):
        raise ValueError("Required handoff chapters must contain text or an explicit none marker.")
    actions = [line.strip() for line in sections[FIRST_SECTION].splitlines() if line.strip()]
    if len(actions) != 3 or any(_ACTION.fullmatch(line) is None for line in actions):
        raise ValueError("The first chapter requires exactly three nonempty one-line actions.")
    if len(sections[FIRST_SECTION].encode("utf-8")) > FIRST_SECTION_BYTES:
        raise ValueError("The first chapter exceeds its 1536-byte UTF-8 limit.")
    return sections
