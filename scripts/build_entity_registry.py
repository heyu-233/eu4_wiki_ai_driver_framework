#!/usr/bin/env python3
"""Build a lightweight entity registry from existing EU4 wiki indices."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "entity_registry.sqlite"
DEFAULT_WIKI_INDEX = ROOT / "data" / "wiki_index.sqlite"
DEFAULT_EFFECT_INDEX = ROOT / "data" / "effect_sources.sqlite"
DEFAULT_ACHIEVEMENT_INDEX = ROOT / "data" / "achievements.sqlite"
DEFAULT_MISSION_INDEX = ROOT / "data" / "mission_sources.sqlite"


COUNTRY_ALIASES = {
    "austria": ("奥地利", "Austria", "Austrian", "HAB"),
    "riga": ("里加", "Riga", "RIG"),
    "saluzzo": ("萨卢佐", "Saluzzo", "SLZ"),
}

GENERIC_ALIASES = {
    "任务",
    "mission",
    "missions",
    "事件",
    "event",
    "events",
    "特质",
    "trait",
    "traits",
    "成就",
    "achievement",
    "achievements",
    "列表",
    "index",
}

MANUAL_ALIASES = (
    {
        "entity_type": "trait",
        "canonical": "charismatic_negotiator_personality",
        "display_name": "Charismatic Negotiator / 魅力非凡的说客",
        "aliases": ("外交家", "外交官", "Diplomat", "diplomat"),
        "page_path": "Leader_trait.html",
        "source_id": "charismatic_negotiator_personality",
        "target_kind": "trait",
        "confidence": 0.97,
    },
    {
        "entity_type": "event",
        "canonical": "incidents_bur_inheritance.5",
        "display_name": "勃艮第女公爵去世",
        "aliases": ("玛丽小姐坠马", "玛丽坠马", "勃艮第女公爵去世", "Mary horse accident"),
        "page_path": "勃艮第事件.html",
        "source_id": "incidents_bur_inheritance.5",
        "target_kind": "event",
        "confidence": 0.98,
    },
)


CONCEPT_ALIASES = (
    {
        "entity_type": "mission_page",
        "canonical": "幕府和大名",
        "display_name": "幕府和大名任务",
        "aliases": ("日本幕府", "幕府", "Shogunate", "Japanese Shogunate"),
        "page_path": "幕府和大名任务.html",
        "source_id": "",
        "target_kind": "mission_page",
        "confidence": 0.96,
    },
    {
        "entity_type": "religion",
        "canonical": "hussite",
        "display_name": "胡斯派 / Hussite",
        "aliases": ("胡斯派", "胡斯教", "Hussite", "Hussites"),
        "page_path": "宗教信条.html",
        "source_id": "hussite",
        "target_kind": "religion",
        "confidence": 0.96,
    },
)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def clean_mission_name(text: str) -> str:
    text = (text or "").replace("_", " ").strip()
    text = re.sub(r"\.html$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:任务|missions?|mission)$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"(?:ian|ese|ish|ic)$", "", text, flags=re.IGNORECASE).strip()
    fixes = {"austr": "Austria", "austrian": "Austria"}
    return fixes.get(text.lower(), text)


def add_entity(
    con: sqlite3.Connection,
    entity_type: str,
    canonical: str,
    alias: str,
    *,
    display_name: str = "",
    page_path: str = "",
    source_id: str = "",
    target_kind: str = "",
    confidence: float = 0.7,
    metadata: dict | None = None,
) -> None:
    alias = (alias or "").strip()
    canonical = (canonical or alias).strip()
    if len(alias) < 2 or norm(alias) in GENERIC_ALIASES or not canonical:
        return
    con.execute(
        """
        insert or ignore into entity_aliases
        (entity_type, canonical, display_name, alias, normalized_alias, page_path, source_id, target_kind, confidence, metadata)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            canonical,
            display_name or canonical,
            alias,
            norm(alias),
            page_path,
            source_id,
            target_kind,
            confidence,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute(
        """
        create table entity_aliases (
            id integer primary key,
            entity_type text not null,
            canonical text not null,
            display_name text not null,
            alias text not null,
            normalized_alias text not null,
            page_path text not null default '',
            source_id text not null default '',
            target_kind text not null default '',
            confidence real not null default 0.7,
            metadata text not null default '{}',
            unique(entity_type, canonical, normalized_alias, page_path, source_id, target_kind)
        )
        """
    )
    con.execute(
        """
        create virtual table entity_aliases_fts using fts5(
            entity_type,
            canonical,
            display_name,
            alias,
            page_path,
            source_id,
            target_kind,
            content='entity_aliases',
            content_rowid='id'
        )
        """
    )
    con.execute("create table build_stats (key text primary key, value text not null)")
    return con


def add_country_aliases(con: sqlite3.Connection) -> None:
    for canonical, aliases in COUNTRY_ALIASES.items():
        display = aliases[0]
        page = f"{canonical.capitalize()}.html"
        mission_page = {
            "austria": "Austrian_missions.html",
            "saluzzo": "意大利小国任务.html",
        }.get(canonical, f"{display}任务.html")
        for alias in aliases:
            add_entity(con, "country", canonical, alias, display_name=display, page_path=page, target_kind="country", confidence=0.98)
            add_entity(
                con,
                "mission_page",
                canonical,
                alias,
                display_name=display,
                page_path=mission_page,
                target_kind="mission_page",
                confidence=0.98,
            )


def add_manual_aliases(con: sqlite3.Connection) -> None:
    for item in MANUAL_ALIASES + CONCEPT_ALIASES:
        for alias in item["aliases"]:
            add_entity(
                con,
                item["entity_type"],
                item["canonical"],
                alias,
                display_name=item["display_name"],
                page_path=item["page_path"],
                source_id=item["source_id"],
                target_kind=item["target_kind"],
                confidence=item["confidence"],
            )


def title_concept_aliases(title: str, source_type: str) -> set[str]:
    title = (title or "").strip()
    aliases: set[str] = set()
    if not title:
        return aliases

    bracket_match = re.match(r"^(.+?)\s*[（(]\s*(.+?)\s*[）)]\s*$", title)
    if bracket_match:
        aliases.add(bracket_match.group(1).strip())
        aliases.add(bracket_match.group(2).strip())

    religion_match = re.search(r"(?:改变宗教为|改信|采纳)\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z\s-]{1,24})", title)
    if religion_match and source_type in {"religion", "decision", "reform"}:
        religion_name = religion_match.group(1).strip()
        religion_name = re.split(r"\s{2,}|[，,。；;:：]", religion_name)[0].strip()
        aliases.add(religion_name)
        if religion_name.endswith("派") and len(religion_name) <= 6:
            aliases.add(religion_name[:-1] + "教")

    return {alias for alias in aliases if 2 <= len(alias) <= 32}


def build_from_wiki_pages(con: sqlite3.Connection, wiki_index: Path) -> int:
    if not wiki_index.exists():
        return 0
    src = sqlite3.connect(wiki_index)
    src.row_factory = sqlite3.Row
    count = 0
    for row in src.execute("select path, title from pages where path like '%.html'"):
        path = row["path"]
        title = row["title"] or Path(path).stem
        stem = Path(path).stem.replace("_", " ")
        is_mission = "任务" in title or "任务" in path or "mission" in path.lower()
        if is_mission:
            canonical = clean_mission_name(title) or clean_mission_name(stem)
            aliases = {canonical, title, clean_mission_name(stem), stem}
            for alias in aliases:
                add_entity(
                    con,
                    "mission_page",
                    canonical,
                    alias,
                    display_name=title,
                    page_path=path,
                    target_kind="mission_page",
                    confidence=0.82,
                )
                count += 1
        page_entity = clean_mission_name(title)
        if 2 <= len(page_entity) <= 32 and not is_mission:
            add_entity(con, "page", page_entity, title, display_name=title, page_path=path, target_kind="page", confidence=0.55)
            count += 1
    src.close()
    return count


def build_from_missions(con: sqlite3.Connection, mission_index: Path) -> int:
    if not mission_index.exists():
        return 0
    src = sqlite3.connect(mission_index)
    src.row_factory = sqlite3.Row
    count = 0
    rows = src.execute("select distinct page_path, page_title, country_or_tree from mission_sources").fetchall()
    for row in rows:
        page_path = row["page_path"]
        page_title = row["page_title"]
        country = row["country_or_tree"] or clean_mission_name(page_title)
        aliases = {country, page_title, clean_mission_name(page_title), clean_mission_name(Path(page_path).stem)}
        for alias in aliases:
            add_entity(
                con,
                "mission_page",
                country,
                alias,
                display_name=page_title,
                page_path=page_path,
                target_kind="mission_page",
                confidence=0.95,
            )
            count += 1
    src.close()
    return count


def build_from_effects(con: sqlite3.Connection, effect_index: Path) -> int:
    if not effect_index.exists():
        return 0
    src = sqlite3.connect(effect_index)
    src.row_factory = sqlite3.Row
    count = 0
    rows = src.execute(
        "select distinct source_type, source_title, source_id, page_path, scope from effect_sources "
        "where source_type in ('trait','event','decision','idea','policy','reform','religion','estate','great_project','modifier')"
    ).fetchall()
    for row in rows:
        source_type = row["source_type"]
        title = row["source_title"] or row["source_id"]
        canonical = row["source_id"] or title
        aliases = {title, row["source_id"], clean_mission_name(title)}
        aliases.update(title_concept_aliases(title, source_type))
        # Split common bilingual titles like "Careful / 谨慎".
        for part in re.split(r"\s*/\s*|\s+/\s+", title or ""):
            aliases.add(part.strip())
        for alias in aliases:
            add_entity(
                con,
                source_type,
                canonical,
                alias,
                display_name=title,
                page_path=row["page_path"],
                source_id=row["source_id"],
                target_kind=source_type,
                confidence=0.92 if source_type in {"trait", "event"} else 0.75,
                metadata={"scope": row["scope"] or ""},
            )
            count += 1
    src.close()
    return count


def build_from_achievements(con: sqlite3.Connection, achievement_index: Path) -> int:
    if not achievement_index.exists():
        return 0
    src = sqlite3.connect(achievement_index)
    src.row_factory = sqlite3.Row
    count = 0
    for row in src.execute("select english_name, chinese_name, page_path from achievements"):
        canonical = row["english_name"] or row["chinese_name"]
        for alias in (row["english_name"], row["chinese_name"]):
            add_entity(
                con,
                "achievement",
                canonical,
                alias,
                display_name=row["chinese_name"] or row["english_name"],
                page_path=row["page_path"],
                target_kind="achievement",
                confidence=0.9,
            )
            count += 1
    src.close()
    return count


def finalize(con: sqlite3.Connection, stats: dict) -> None:
    con.execute("insert into entity_aliases_fts(entity_aliases_fts) values('rebuild')")
    for key, value in stats.items():
        con.execute(
            "insert or replace into build_stats(key, value) values (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
    con.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--wiki-index", type=Path, default=DEFAULT_WIKI_INDEX)
    parser.add_argument("--effect-index", type=Path, default=DEFAULT_EFFECT_INDEX)
    parser.add_argument("--achievement-index", type=Path, default=DEFAULT_ACHIEVEMENT_INDEX)
    parser.add_argument("--mission-index", type=Path, default=DEFAULT_MISSION_INDEX)
    args = parser.parse_args()

    con = init_db(args.out)
    add_country_aliases(con)
    add_manual_aliases(con)
    stats = {
        "wiki_page_aliases": build_from_wiki_pages(con, args.wiki_index),
        "mission_aliases": build_from_missions(con, args.mission_index),
        "effect_aliases": build_from_effects(con, args.effect_index),
        "achievement_aliases": build_from_achievements(con, args.achievement_index),
    }
    stats["total_aliases"] = con.execute("select count(*) from entity_aliases").fetchone()[0]
    finalize(con, stats)
    con.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
