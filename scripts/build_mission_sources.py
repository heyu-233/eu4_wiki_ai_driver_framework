#!/usr/bin/env python3
"""Build a structured mission lookup index for the local EU4 wiki."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, Tag


EXCLUDED_PREFIXES = (
    "File_",
    "Special_",
    "Talk_",
    "Template_",
    "User_",
    "MediaWiki_",
    "Category_",
)

EXCLUDED_STEM_BITS = (
    "当前任务",
    "补完后",
    "修复后",
    "消歧义",
    "Achievement",
    "成就",
    "modding",
    "Modding",
)

MISSION_HEADER_WORDS = ("任务", "Mission")
CONDITION_HEADER_WORDS = ("完成条件", "条件", "Requirements")
EFFECT_HEADER_WORDS = ("效果", "奖励", "Effects")
PREREQ_HEADER_WORDS = ("前置任务", "前置", "Required missions", "Prerequisites")


@dataclass
class MissionSource:
    page_title: str
    country_or_tree: str
    mission_title: str
    mission_id: str
    slot_or_section: str
    description: str
    conditions: str
    effects: str
    prerequisites: str
    version_note: str
    page_path: str
    raw_text: str


def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\u202f", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compact(text: str, max_len: int) -> str:
    text = normalize_text(text)
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def clean_cell(cell: Tag | None) -> str:
    if cell is None:
        return ""
    return normalize_text(cell.get_text("\n", strip=True))


def page_title(soup: BeautifulSoup, path: Path) -> str:
    title = soup.find("title")
    if title:
        text = normalize_text(title.get_text(" ", strip=True))
        text = re.sub(r"\s*-\s*欧陆风云4百科.*$", "", text)
        if text:
            return text
    heading = soup.find("h1")
    if heading:
        text = normalize_text(heading.get_text(" ", strip=True))
        if text:
            return text
    return path.stem.replace("_", " ")


def is_candidate(path: Path, wiki_dir: Path) -> bool:
    if path.suffix.lower() != ".html":
        return False
    rel = path.relative_to(wiki_dir).as_posix()
    first = rel.split("/", 1)[0]
    if first.startswith(EXCLUDED_PREFIXES):
        return False
    stem = path.stem
    if any(bit in stem for bit in EXCLUDED_STEM_BITS):
        return False
    lowered = stem.lower()
    return "任务" in stem or "mission" in lowered or "missions" in lowered


def infer_country_or_tree(title: str, path: Path) -> str:
    stem = path.stem.replace("_", " ")
    candidates = [title, stem]
    for value in candidates:
        value = normalize_text(value)
        value = re.sub(r"\s*(任务|任務|missions?|Mission tree)\s*$", "", value, flags=re.IGNORECASE)
        if value:
            return value
    return stem


def table_headers(table: Tag) -> list[str]:
    first_row = table.find("tr")
    if not first_row:
        return []
    cells = first_row.find_all(["th", "td"], recursive=False)
    return [clean_cell(cell).replace("\n", " ") for cell in cells]


def looks_like_mission_table(table: Tag) -> bool:
    headers = table_headers(table)
    if len(headers) < 3:
        return False
    joined = " ".join(headers)
    return (
        any(word in joined for word in MISSION_HEADER_WORDS)
        and any(word in joined for word in CONDITION_HEADER_WORDS)
        and any(word in joined for word in EFFECT_HEADER_WORDS)
    )


def previous_heading(table: Tag) -> str:
    for prev in table.find_all_previous(["h4", "h3", "h2"], limit=8):
        text = normalize_text(prev.get_text(" ", strip=True))
        text = re.sub(r"\[.*?\]", "", text).strip()
        if text:
            return text
    return ""


def nearby_version_note(table: Tag) -> str:
    notes: list[str] = []
    for prev in table.find_all_previous(["div", "p"], limit=8):
        text = normalize_text(prev.get_text(" ", strip=True))
        if "最后更新于" in text or "落后" in text or "outdated" in text.lower():
            notes.append(compact(text, 180))
    return "；".join(list(dict.fromkeys(notes))[:2])


def mission_title_and_description(cell: Tag) -> tuple[str, str, str]:
    span = cell.find("span", id=True)
    mission_id = normalize_text(span.get("id", "")) if span else ""

    title_node = None
    for div in cell.find_all("div"):
        style = div.get("style", "")
        if "larger" in style or "font-weight: bold" in style:
            text = normalize_text(div.get_text(" ", strip=True))
            if text and len(text) <= 120:
                title_node = div
    if title_node is not None:
        title = normalize_text(title_node.get_text(" ", strip=True))
        title = title.replace(mission_id, "").strip() or title
    else:
        lines = [line.strip() for line in clean_cell(cell).splitlines() if line.strip()]
        title = lines[0] if lines else ""

    all_lines = [line.strip() for line in clean_cell(cell).splitlines() if line.strip()]
    description_lines = []
    title_seen = False
    for line in all_lines:
        if not title_seen and line == title:
            title_seen = True
            continue
        if title_seen:
            description_lines.append(line)
    description = compact("\n".join(description_lines), 900)
    return compact(title, 180), mission_id, description


def find_column(headers: list[str], words: tuple[str, ...], default: int) -> int:
    for index, header in enumerate(headers):
        if any(word.lower() in header.lower() for word in words):
            return index
    return default


def extract_missions_from_page(path: Path, wiki_dir: Path) -> list[MissionSource]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    soup = BeautifulSoup(source, "html.parser")
    title = page_title(soup, path)
    country_or_tree = infer_country_or_tree(title, path)
    rel_path = path.relative_to(wiki_dir).as_posix()
    missions: list[MissionSource] = []

    for table in soup.find_all("table"):
        if not looks_like_mission_table(table):
            continue
        headers = table_headers(table)
        mission_col = find_column(headers, MISSION_HEADER_WORDS, 0)
        condition_col = find_column(headers, CONDITION_HEADER_WORDS, 1)
        effect_col = find_column(headers, EFFECT_HEADER_WORDS, 2)
        prereq_col = find_column(headers, PREREQ_HEADER_WORDS, 3)
        slot = previous_heading(table)
        version = nearby_version_note(table)
        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 3 or mission_col >= len(cells):
                continue
            mission_title, mission_id, description = mission_title_and_description(cells[mission_col])
            if not mission_title or mission_title in {"任务", "Mission"}:
                continue
            conditions = clean_cell(cells[condition_col]) if condition_col < len(cells) else ""
            effects = clean_cell(cells[effect_col]) if effect_col < len(cells) else ""
            prerequisites = clean_cell(cells[prereq_col]) if prereq_col < len(cells) else ""
            raw_text = compact(
                "\n".join(
                    bit
                    for bit in (
                        title,
                        country_or_tree,
                        slot,
                        mission_title,
                        mission_id,
                        description,
                        "完成条件:\n" + conditions if conditions else "",
                        "效果:\n" + effects if effects else "",
                        "前置任务:\n" + prerequisites if prerequisites else "",
                        version,
                    )
                    if bit
                ),
                4500,
            )
            if len(raw_text) < 80:
                continue
            missions.append(
                MissionSource(
                    page_title=compact(title, 180),
                    country_or_tree=compact(country_or_tree, 180),
                    mission_title=mission_title,
                    mission_id=compact(mission_id, 180),
                    slot_or_section=compact(slot, 180),
                    description=description,
                    conditions=compact(conditions, 1400),
                    effects=compact(effects, 1800),
                    prerequisites=compact(prerequisites, 500),
                    version_note=compact(version, 260),
                    page_path=rel_path,
                    raw_text=raw_text,
                )
            )
    return missions


def page_has_mission_table(path: Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    soup = BeautifulSoup(source, "html.parser")
    return any(looks_like_mission_table(table) for table in soup.find_all("table"))


def init_db(out: Path) -> tuple[sqlite3.Connection, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (out, out.with_name(out.name + "-wal"), out.with_name(out.name + "-shm")):
        if candidate.exists():
            candidate.unlink()
    con = sqlite3.connect(out)
    con.execute("pragma journal_mode=wal")
    con.executescript(
        """
        create table mission_sources (
            id integer primary key,
            page_title text not null,
            country_or_tree text not null,
            mission_title text not null,
            mission_id text not null,
            slot_or_section text not null,
            description text not null,
            conditions text not null,
            effects text not null,
            prerequisites text not null,
            version_note text not null,
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
            create virtual table mission_sources_fts using fts5(
                page_title,
                country_or_tree,
                mission_title,
                mission_id,
                slot_or_section,
                description,
                conditions,
                effects,
                prerequisites,
                version_note,
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
            create virtual table mission_sources_fts using fts5(
                page_title,
                country_or_tree,
                mission_title,
                mission_id,
                slot_or_section,
                description,
                conditions,
                effects,
                prerequisites,
                version_note,
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


def insert_missions(con: sqlite3.Connection, missions: list[MissionSource]) -> None:
    rows = [
        (
            item.page_title,
            item.country_or_tree,
            item.mission_title,
            item.mission_id,
            item.slot_or_section,
            item.description,
            item.conditions,
            item.effects,
            item.prerequisites,
            item.version_note,
            item.page_path,
            item.raw_text,
        )
        for item in missions
    ]
    con.executemany(
        """
        insert into mission_sources(
            page_title, country_or_tree, mission_title, mission_id, slot_or_section,
            description, conditions, effects, prerequisites, version_note, page_path, raw_text
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.executemany(
        """
        insert into mission_sources_fts(
            page_title, country_or_tree, mission_title, mission_id, slot_or_section,
            description, conditions, effects, prerequisites, version_note, page_path, raw_text
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def build_mission_sources(wiki_dir: Path, out: Path) -> int:
    wiki_dir = wiki_dir.resolve()
    paths = sorted(p for p in wiki_dir.rglob("*.html") if is_candidate(p, wiki_dir))
    con, tokenizer = init_db(out)
    scanned_pages = len(paths)
    candidate_pages = 0
    indexed_pages = 0
    indexed_missions = 0
    failures: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for index, path in enumerate(paths, start=1):
        if not page_has_mission_table(path):
            if len(skipped) < 100:
                skipped.append({"path": path.relative_to(wiki_dir).as_posix(), "reason": "no mission table"})
            continue
        candidate_pages += 1
        missions = extract_missions_from_page(path, wiki_dir)
        if missions:
            insert_missions(con, missions)
            indexed_pages += 1
            indexed_missions += len(missions)
        elif len(failures) < 100:
            failures.append({"path": path.relative_to(wiki_dir).as_posix(), "reason": "no mission table rows"})
        if index % 200 == 0:
            con.commit()
            print(
                f"scanned {index}/{scanned_pages} html pages, found {candidate_pages} mission-table pages, "
                f"indexed {indexed_missions} missions",
                flush=True,
            )

    coverage = indexed_pages / candidate_pages if candidate_pages else 0
    set_stat(con, "wiki_dir", str(wiki_dir))
    set_stat(con, "scanned_pages", scanned_pages)
    set_stat(con, "candidate_pages", candidate_pages)
    set_stat(con, "indexed_pages", indexed_pages)
    set_stat(con, "indexed_missions", indexed_missions)
    set_stat(con, "coverage", coverage)
    set_stat(con, "tokenizer", tokenizer)
    set_stat(con, "built_at", datetime.now(timezone.utc).isoformat())
    set_stat(con, "failures", failures)
    set_stat(con, "skipped", skipped)
    con.commit()
    con.close()
    print(f"Indexed {indexed_missions} missions from {indexed_pages}/{candidate_pages} pages -> {out}")
    return 0 if indexed_missions else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-dir", default=Path("wiki"), type=Path)
    parser.add_argument("--out", default=Path("data/mission_sources.sqlite"), type=Path)
    args = parser.parse_args()
    return build_mission_sources(args.wiki_dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
