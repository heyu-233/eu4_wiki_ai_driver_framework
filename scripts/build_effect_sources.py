#!/usr/bin/env python3
"""Build a semi-structured effect-source lookup index for the local EU4 wiki."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


EXCLUDED_PREFIXES = (
    "File_",
    "Special_",
    "Talk_",
    "Template_",
    "User_",
    "MediaWiki_",
    "Category_",
)

EXCLUDED_NAME_PATTERNS = (
    re.compile(r"^1\.\d+(?:\.\d+)?(?:_版本)?$", re.IGNORECASE),
    re.compile(r"^1\.\d+\.X_版本$", re.IGNORECASE),
    re.compile(r".*_modding$", re.IGNORECASE),
    re.compile(r".*modding.*", re.IGNORECASE),
    re.compile(r".*Achievement.*", re.IGNORECASE),
    re.compile(r".*当前任务.*", re.IGNORECASE),
    re.compile(r".*补完向.*", re.IGNORECASE),
    re.compile(r".*修复向.*", re.IGNORECASE),
)

STALE_MARKERS = (
    "这条信息可能已不适合当前",
    "此信息可能已落后",
    "最后更新于1.30",
    "最后更新于1.31",
    "最后更新于1.32",
    "最后更新于1.33",
    "最后更新于1.34",
    "最后更新于1.35",
    "最后更新于1.36",
)

SOURCE_TYPES = (
    "event",
    "mission",
    "decision",
    "idea",
    "policy",
    "reform",
    "religion",
    "estate",
    "great_project",
    "modifier",
    "trait",
)

EFFECT_HINTS = (
    "效果",
    "奖励",
    "修正",
    "获得",
    "增加",
    "减少",
    "失去",
    "持续",
    "modifier",
    "impact",
    "cost",
    "efficiency",
    "reputation",
    "relation",
    "missionary",
    "absolutism",
    "autonomy",
    "core",
    "稳定",
    "外交",
    "行政",
    "传教",
    "侵略扩张",
    "改善关系",
    "造核",
    "理念",
    "政策",
    "特权",
    "特质",
    "权重",
    "技能",
)


@dataclass
class EffectSource:
    source_type: str
    source_title: str
    source_id: str
    option_or_section: str
    conditions: str
    effects: str
    duration: str
    scope: str
    page_path: str
    raw_text: str


def normalize_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compact(text: str, max_len: int) -> str:
    text = normalize_text(text)
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def clean_html_fragment(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>|<svg.*?</svg>", " ", fragment)
    fragment = re.sub(r'(?is)<abbr\s+title=["\']([^"\']+)["\'][^>]*>\s*ID\s*</abbr>', r"ID \1", fragment)
    fragment = re.sub(r'(?is)<span\s+id=["\']([A-Za-z0-9_.:-]+)["\'][^>]*>\s*</span>', r"ID \1 ", fragment)
    fragment = re.sub(r"(?i)</(td|th|tr|li|p|div|h[1-6]|table|caption)>", "\n", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    text = normalize_text(text)
    text = re.sub(r"这条信息可能已不适合当前\s*版本\s*，?最后更新于[0-9.]+。?", " ", text)
    text = re.sub(r"此信息可能已落后\s*版本\s*，?最后更新于[0-9.]+。?", " ", text)
    return normalize_text(text)


def strip_wiki_suffix(title: str, fallback: str) -> str:
    title = normalize_text(title)
    title = re.sub(r"\s*-\s*欧[陆陸]风云4百科.*$", "", title)
    return title or fallback


def page_title(source: str, path: Path) -> str:
    match = re.search(r"<title>(.*?)</title>", source, flags=re.IGNORECASE | re.DOTALL)
    fallback = path.stem.replace("_", " ")
    return strip_wiki_suffix(match.group(1), fallback) if match else fallback


def is_candidate(path: Path, wiki_dir: Path) -> bool:
    rel = path.relative_to(wiki_dir).as_posix()
    first = rel.split("/", 1)[0]
    if path.suffix.lower() != ".html" or first.startswith(EXCLUDED_PREFIXES):
        return False
    stem = path.stem
    return not any(pattern.match(stem) for pattern in EXCLUDED_NAME_PATTERNS)


def detect_source_types(path: Path, title: str) -> list[str]:
    stem = path.stem
    lowered = stem.lower()
    text = f"{stem} {title}".lower()
    types: list[str] = []

    if is_low_value_source_page(path, title):
        return []

    if lowered.endswith("_events") or "events" in lowered or "事件" in stem:
        types.append("event")
    if "任务" in stem or "missions" in lowered or lowered.endswith("_mission"):
        types.append("mission")
    if "decision" in lowered or "decisions" in lowered or "决议" in stem:
        types.append("decision")
    if (
        "ideas" in lowered
        or "idea" in lowered
        or "理念" in stem
        or stem.endswith("_Traditions")
    ) and "events" not in lowered:
        types.append("idea")
    if "polic" in lowered or "政策" in stem:
        types.append("policy")
    if "reform" in lowered or "改革" in stem:
        types.append("reform")
    if "religion" in lowered or "religious" in lowered or "宗教" in stem or "信仰" in stem or "学派" in stem:
        types.append("religion")
    if "estate" in lowered or "estates" in lowered or "阶层" in stem or "特权" in stem:
        types.append("estate")
    if "great_project" in lowered or "great projects" in text or "great_project" in text or "奇观" in stem or "伟大工程" in stem:
        types.append("great_project")
    if "modifier" in lowered or "modifiers" in lowered or "修正" in stem:
        types.append("modifier")
    if lowered == "leader_trait" or stem in {"特质", "统治者特质", "君主特质", "将领特质"}:
        types.append("trait")

    return [source_type for source_type in SOURCE_TYPES if source_type in types]


def is_low_value_source_page(path: Path, title: str) -> bool:
    stem = path.stem
    lowered = stem.lower()
    keep_list_pages = {
        "modifier_list",
        "修正列表",
        "event_modifiers",
        "永久修正",
        "permanent_modifiers",
        "great_project",
        "伟大工程",
        "policies",
        "政策",
        "estates",
        "阶层",
    }
    if lowered in keep_list_pages or stem in keep_list_pages:
        return False
    if title in {"成就", "Achievement", "Achievements"} or "achievement" in lowered:
        return True
    if any(bit in stem for bit in ("列表", "总决议列表", "当前任务", "补完向", "修复向", "消歧义")):
        return True
    if lowered.startswith("list_") or "disambiguation" in lowered:
        return True
    if title.startswith("欧陆风云4百科:"):
        return True
    return False


def read_html(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def extract_id(text: str, source_type: str) -> str:
    patterns = [
        r"\bID\s+([A-Za-z0-9_.:-]+\.\d+)\b",
        r"\b([A-Za-z_][A-Za-z0-9_:-]+\.\d+)\b",
        r"\b([a-z_]+_[a-z0-9_.:-]+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    if source_type == "modifier":
        match = re.search(r"\b([a-z][a-z0-9_]{3,})\b", text)
        if match:
            return match.group(1)
    return ""


def extract_duration(text: str) -> str:
    matches = re.findall(r"(?:持续|为期)\s*([0-9]+)\s*(?:年|个月|天)|([0-9]+)\s*(?:年|个月|天)", text)
    values = ["".join(m).strip() for m in matches if "".join(m).strip()]
    return "；".join(list(dict.fromkeys(values))[:3])


def extract_conditions(text: str) -> str:
    markers = ("触发条件", "条件", "需求", "完成条件", "潜在需求", "可用条件", "需要", "必须")
    return extract_after_markers(text, markers, 900)


def extract_effects(text: str) -> str:
    markers = ("效果", "奖励", "选项", "将会", "获得", "失去", "增加", "减少")
    extracted = extract_after_markers(text, markers, 1000)
    if extracted:
        return extracted
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    effect_lines = [line for line in lines if any(hint.lower() in line.lower() for hint in EFFECT_HINTS)]
    return compact("\n".join(effect_lines[:12]), 1000)


def extract_after_markers(text: str, markers: tuple[str, ...], max_len: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    selected: list[str] = []
    capture = False
    for line in lines:
        if any(marker in line for marker in markers):
            capture = True
            selected.append(line)
            continue
        if capture:
            if len(selected) >= 10:
                break
            if re.match(r"^[A-Z]?[0-9]?\s*[\u4e00-\u9fffA-Za-z][^。]{0,30}$", line) and not any(
                hint in line for hint in EFFECT_HINTS
            ):
                break
            selected.append(line)
    return compact("\n".join(selected), max_len)


def infer_scope(text: str, title: str, path: Path, source_type: str) -> str:
    scope_bits = [title]
    if source_type == "mission":
        for match in re.finditer(r"(?:适用|使用|国家|任务树|class=|任务组)[：:\s]*([^\n]{2,80})", text):
            scope_bits.append(match.group(1))
    scope_bits.append(path.stem.replace("_", " "))
    return compact("；".join(dict.fromkeys(bit.strip() for bit in scope_bits if bit.strip())), 260)


def event_blocks(source: str) -> list[str]:
    positions = [m.start() for m in re.finditer(r'class=["\'][^"\']*\beu4box\b', source, flags=re.IGNORECASE)]
    blocks = []
    for index, pos in enumerate(positions):
        start = source.rfind("<div", 0, pos)
        if start < 0:
            start = max(0, pos - 500)
        next_pos = positions[index + 1] if index + 1 < len(positions) else len(source)
        next_start = source.rfind("<div", 0, next_pos) if index + 1 < len(positions) else -1
        heading_pos = source.find('<h2', pos, next_pos)
        end_candidates = [candidate for candidate in (heading_pos, next_start) if candidate > pos]
        end = min(end_candidates) if end_candidates else next_pos
        blocks.append(source[start:end])
    return blocks


def table_row_blocks(source: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"(?is)<tr\b.*?</tr>", source) if len(m.group(0)) > 120]


def table_row_blocks_with_sections(source: str) -> list[tuple[str, str]]:
    row_matches = list(re.finditer(r"(?is)<tr\b.*?</tr>", source))
    heading_matches = list(re.finditer(r"(?is)<h([2-4])[^>]*>(.*?)</h\1>", source))
    rows: list[tuple[str, str]] = []
    heading_index = 0
    current_heading = ""
    for match in row_matches:
        if len(match.group(0)) <= 120:
            continue
        while heading_index < len(heading_matches) and heading_matches[heading_index].start() < match.start():
            current_heading = clean_html_fragment(heading_matches[heading_index].group(2))
            heading_index += 1
        rows.append((current_heading, match.group(0)))
    return rows


def heading_blocks(source: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"(?is)<h([2-4])[^>]*>(.*?)</h\1>")
    matches = list(pattern.finditer(source))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        heading = clean_html_fragment(match.group(2))
        fragment = source[match.start() : min(end, start + 8000)]
        blocks.append((heading, fragment))
    return blocks


def generic_blocks(source: str) -> list[tuple[str, str]]:
    blocks = heading_blocks(source)
    rows = [("", row) for row in table_row_blocks(source)]
    return blocks + rows


def block_title(text: str, fallback: str, source_type: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return fallback
    skip_prefixes = ("ID ", "触发条件", "条件", "效果", "奖励")
    skip_contains = ("这条信息可能已不适合当前", "最后更新于", "版本")
    for line in lines[:8]:
        if len(line) <= 90 and not line.startswith(skip_prefixes) and not any(bit in line for bit in skip_contains):
            return line
    return lines[0][:90]


def block_option(text: str, source_type: str) -> str:
    if source_type == "event":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            if "这条信息可能已不适合当前" in line:
                continue
            if 2 <= len(line) <= 80 and not line.startswith(("ID ", "触发条件", "平均发生时间")):
                if "。" in line or "！" in line or "？" in line or line.startswith(("好", "来", "不", "我们")):
                    return line
    return ""


def split_table_cells(fragment: str) -> list[str]:
    cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", fragment)
    return [clean_html_fragment(cell) for cell in cells]


def trait_scope(section: str) -> str:
    if "将领" in section:
        return "general admiral leader"
    if "AI" in section:
        return "ai ruler"
    return "ruler heir consort"


def trait_id(english_name: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", english_name.strip().lower()).strip("_")
    return f"{key}_personality" if key else ""


def make_trait_source(fragment: str, section_hint: str, rel_path: str) -> EffectSource | None:
    cells = split_table_cells(fragment)
    if len(cells) < 4:
        return None
    name_lines = [line.strip() for line in cells[0].splitlines() if line.strip()]
    if len(name_lines) < 2:
        return None
    english_name = name_lines[0]
    chinese_name = name_lines[1]
    description = "\n".join(name_lines[2:])
    if english_name in {"特质", "Trait"} or not chinese_name:
        return None
    effects = cells[1] if len(cells) > 1 else ""
    ai_behavior = cells[2] if len(cells) > 2 else ""
    conditions = cells[3] if len(cells) > 3 else ""
    weight = cells[4] if len(cells) > 4 else ""
    title = f"{english_name} / {chinese_name}"
    scope = trait_scope(section_hint)
    raw_text = compact(
        "\n".join(
            bit
            for bit in (
                title,
                section_hint,
                description,
                f"效果：{effects}" if effects else "",
                f"AI行为：{ai_behavior}" if ai_behavior else "",
                f"冲突/前提/权重修正：{conditions}" if conditions else "",
                f"权重：{weight}" if weight else "",
                f"scope: {scope}",
            )
            if bit
        ),
        3500,
    )
    return EffectSource(
        source_type="trait",
        source_title=compact(title, 180),
        source_id=trait_id(english_name),
        option_or_section=section_hint or "特质",
        conditions=compact(
            "\n".join(bit for bit in (conditions, f"权重：{weight}" if weight else "") if bit),
            1000,
        ),
        effects=compact(effects, 1000),
        duration="",
        scope=scope,
        page_path=rel_path,
        raw_text=raw_text,
    )


def should_keep(text: str, source_type: str) -> bool:
    if len(text) < 50:
        return False
    head = "\n".join(text.splitlines()[:4])
    if "目录" in head and not any(hint in text for hint in ("效果", "奖励", "触发条件", "获得", "修正")):
        return False
    if any(marker in text[:260] for marker in STALE_MARKERS):
        return False
    if source_type in {"decision", "event", "mission"} and re.search(r"\bmod(?:ding)?\b|mod文件夹|创建.*mod", text, flags=re.IGNORECASE):
        return False
    if source_type == "trait":
        return True
    if source_type in {"event", "mission", "decision"}:
        return True
    lowered = text.lower()
    return any(hint.lower() in lowered for hint in EFFECT_HINTS)


def html_block_heading(fragment: str) -> str:
    patterns = [
        r'(?is)<span\s+class=["\']mw-headline["\'][^>]*>(.*?)</span>',
        r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>",
        r'(?is)<div\s+class=["\']heading["\'][^>]*>(.*?)</div>',
    ]
    for pattern in patterns:
        match = re.search(pattern, fragment)
        if match:
            heading = clean_html_fragment(match.group(1))
            heading = re.sub(r"^ID\s+[A-Za-z0-9_.:-]+\s*", "", heading).strip()
            if heading and len(heading) <= 160:
                return heading
    return ""


def html_block_option(fragment: str) -> str:
    patterns = [
        r'(?is)<div\s+class=["\']option_title["\'][^>]*>(.*?)</div>',
        r'(?is)<div[^>]+class=["\'][^"\']*option_title[^"\']*["\'][^>]*>(.*?)</div>',
    ]
    for pattern in patterns:
        match = re.search(pattern, fragment)
        if match:
            option = clean_html_fragment(match.group(1))
            if option and len(option) <= 160:
                return option
    return ""


def make_source(
    source_type: str,
    text: str,
    page_title_value: str,
    rel_path: str,
    path: Path,
    section_hint: str = "",
    option_hint: str = "",
) -> EffectSource | None:
    text = compact(text, 3500)
    if not should_keep(text, source_type):
        return None
    title = section_hint or block_title(text, page_title_value, source_type)
    source_id = extract_id(text, source_type)
    return EffectSource(
        source_type=source_type,
        source_title=compact(title, 180),
        source_id=source_id,
        option_or_section=compact(option_hint or block_option(text, source_type) or section_hint, 220),
        conditions=extract_conditions(text),
        effects=extract_effects(text),
        duration=extract_duration(text),
        scope=infer_scope(text, page_title_value, path, source_type),
        page_path=rel_path,
        raw_text=text,
    )


def extract_sources(path: Path, wiki_dir: Path) -> list[EffectSource]:
    source = read_html(path)
    if not source:
        return []
    title = page_title(source, path)
    source_types = detect_source_types(path, title)
    if not source_types:
        return []
    rel_path = path.relative_to(wiki_dir).as_posix()
    extracted: list[EffectSource] = []
    for source_type in source_types:
        if source_type == "event":
            raw_blocks = [("", block) for block in event_blocks(source)]
            if not raw_blocks:
                raw_blocks = generic_blocks(source)
        elif source_type == "mission":
            raw_blocks = [("", row) for row in table_row_blocks(source)] or generic_blocks(source)
        elif source_type == "trait":
            for section_hint, fragment in table_row_blocks_with_sections(source):
                item = make_trait_source(fragment, section_hint, rel_path)
                if item:
                    extracted.append(item)
            continue
        elif source_type in {"decision", "policy", "idea", "reform", "religion", "estate", "great_project", "modifier"}:
            raw_blocks = generic_blocks(source)
        else:
            raw_blocks = []

        for section_hint, fragment in raw_blocks:
            text = clean_html_fragment(fragment)
            html_heading = html_block_heading(fragment)
            html_option = html_block_option(fragment)
            item = make_source(source_type, text, title, rel_path, path, section_hint or html_heading, html_option)
            if item:
                extracted.append(item)
    return extracted


def init_db(out: Path) -> tuple[sqlite3.Connection, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (out, out.with_name(out.name + "-wal"), out.with_name(out.name + "-shm")):
        if candidate.exists():
            candidate.unlink()
    con = sqlite3.connect(out)
    con.execute("pragma journal_mode=wal")
    con.executescript(
        """
        create table effect_sources (
            id integer primary key,
            source_type text not null,
            source_title text not null,
            source_id text not null,
            option_or_section text not null,
            conditions text not null,
            effects text not null,
            duration text not null,
            scope text not null,
            page_path text not null,
            raw_text text not null
        );
        create table build_stats (
            key text primary key,
            value text not null
        );
        """
    )
    tokenizer = "trigram"
    try:
        con.execute(
            """
            create virtual table effect_sources_fts using fts5(
                source_type,
                source_title,
                source_id,
                option_or_section,
                conditions,
                effects,
                scope,
                page_path,
                raw_text,
                tokenize='trigram'
            )
            """
        )
    except sqlite3.Error:
        tokenizer = "unicode61"
        con.execute(
            """
            create virtual table effect_sources_fts using fts5(
                source_type,
                source_title,
                source_id,
                option_or_section,
                conditions,
                effects,
                scope,
                page_path,
                raw_text,
                tokenize='unicode61'
            )
            """
        )
    return con, tokenizer


def set_stat(con: sqlite3.Connection, key: str, value: object) -> None:
    con.execute(
        "insert or replace into build_stats(key, value) values (?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def insert_sources(con: sqlite3.Connection, sources: list[EffectSource]) -> None:
    rows = [
        (
            item.source_type,
            item.source_title,
            item.source_id,
            item.option_or_section,
            item.conditions,
            item.effects,
            item.duration,
            item.scope,
            item.page_path,
            item.raw_text,
        )
        for item in sources
    ]
    con.executemany(
        """
        insert into effect_sources(
            source_type, source_title, source_id, option_or_section, conditions,
            effects, duration, scope, page_path, raw_text
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.executemany(
        """
        insert into effect_sources_fts(
            source_type, source_title, source_id, option_or_section, conditions,
            effects, scope, page_path, raw_text
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.source_type,
                item.source_title,
                item.source_id,
                item.option_or_section,
                item.conditions,
                item.effects,
                item.scope,
                item.page_path,
                item.raw_text,
            )
            for item in sources
        ],
    )


def build_effect_sources(wiki_dir: Path, out: Path) -> int:
    wiki_dir = wiki_dir.resolve()
    paths = sorted(p for p in wiki_dir.rglob("*.html") if is_candidate(p, wiki_dir))
    con, tokenizer = init_db(out)
    candidate_pages = 0
    indexed_pages = 0
    indexed_sources = 0
    type_counts = {source_type: 0 for source_type in SOURCE_TYPES}
    failures: list[dict[str, str]] = []

    for i, path in enumerate(paths, start=1):
        title_hint = path.stem.replace("_", " ")
        if not detect_source_types(path, title_hint):
            continue
        candidate_pages += 1
        sources = extract_sources(path, wiki_dir)
        if sources:
            insert_sources(con, sources)
            indexed_pages += 1
            indexed_sources += len(sources)
            for item in sources:
                type_counts[item.source_type] += 1
        elif len(failures) < 100:
            failures.append({"path": path.relative_to(wiki_dir).as_posix(), "reason": "no effect-source blocks"})

        if i % 1000 == 0:
            con.commit()
            print(f"scanned {i}/{len(paths)} html files, indexed {indexed_sources} effect sources", flush=True)

    set_stat(con, "wiki_dir", str(wiki_dir))
    set_stat(con, "candidate_pages", candidate_pages)
    set_stat(con, "indexed_pages", indexed_pages)
    set_stat(con, "indexed_sources", indexed_sources)
    set_stat(con, "type_counts", type_counts)
    set_stat(con, "tokenizer", tokenizer)
    set_stat(con, "built_at", datetime.now(timezone.utc).isoformat())
    set_stat(con, "failures", failures)
    con.commit()
    con.close()
    print(
        f"Indexed {indexed_sources} effect sources from {indexed_pages}/{candidate_pages} candidate pages -> {out}"
    )
    print("Type counts: " + ", ".join(f"{k}={v}" for k, v in type_counts.items()))
    return 0 if indexed_sources else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-dir", default=Path("wiki"), type=Path)
    parser.add_argument("--out", default=Path("data/effect_sources.sqlite"), type=Path)
    args = parser.parse_args()
    return build_effect_sources(args.wiki_dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
