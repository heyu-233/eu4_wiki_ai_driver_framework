#!/usr/bin/env python3
"""Build a dedicated achievement lookup index for the local EU4 wiki."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def normalize_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_html_fragment(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>|<svg.*?</svg>", " ", fragment)
    fragment = re.sub(r"(?i)</(td|th|tr|li|p|div|h[1-6]|table)>", "\n", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    return normalize_text(text)


def compact(text: str, max_len: int) -> str:
    text = normalize_text(text)
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def read_html(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def extract_name_parts(cell_html: str, fallback_text: str) -> tuple[str, str, str]:
    divs = re.findall(r"(?is)<div[^>]*font-weight:\s*bold[^>]*>(.*?)</div>", cell_html)
    cleaned = [clean_html_fragment(part) for part in divs if clean_html_fragment(part)]
    english = cleaned[0] if cleaned else ""
    chinese = cleaned[1] if len(cleaned) > 1 else ""
    lines = [line.strip() for line in fallback_text.splitlines() if line.strip()]
    if not english and lines:
        english = lines[0]
    if not chinese and len(lines) > 1:
        chinese = lines[1]
    description = ""
    for line in lines:
        if line not in {english, chinese}:
            description = line
            break
    return english, chinese, description


def parse_achievement_rows(source: str) -> list[dict[str, str]]:
    rows = []
    for row_html in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", source):
        cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row_html)
        if len(cells) < 7:
            continue
        first = clean_html_fragment(cells[0])
        if first == "成就" or "完成需求" in first:
            continue
        english, chinese, description = extract_name_parts(cells[0], first)
        if not english and not chinese:
            continue
        item = {
            "english_name": compact(english, 160),
            "chinese_name": compact(chinese, 160),
            "description": compact(description, 400),
            "starting_conditions": compact(clean_html_fragment(cells[1]), 1200),
            "completion_requirements": compact(clean_html_fragment(cells[2]), 1400),
            "notes": compact(clean_html_fragment(cells[3]), 1800),
            "dlc": compact(clean_html_fragment(cells[4]), 400),
            "version": compact(clean_html_fragment(cells[5]), 80),
            "difficulty": compact(clean_html_fragment(cells[6]), 80),
        }
        raw_text = "\n".join(
            item[key]
            for key in (
                "english_name",
                "chinese_name",
                "description",
                "starting_conditions",
                "completion_requirements",
                "notes",
                "dlc",
                "version",
                "difficulty",
            )
            if item[key]
        )
        item["raw_text"] = compact(raw_text, 4000)
        rows.append(item)
    return rows


def init_db(out: Path) -> tuple[sqlite3.Connection, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (out, out.with_name(out.name + "-wal"), out.with_name(out.name + "-shm")):
        if candidate.exists():
            candidate.unlink()
    con = sqlite3.connect(out)
    con.execute("pragma journal_mode=wal")
    con.executescript(
        """
        create table achievements (
            id integer primary key,
            english_name text not null,
            chinese_name text not null,
            description text not null,
            starting_conditions text not null,
            completion_requirements text not null,
            notes text not null,
            dlc text not null,
            version text not null,
            difficulty text not null,
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
            create virtual table achievements_fts using fts5(
                english_name,
                chinese_name,
                description,
                starting_conditions,
                completion_requirements,
                notes,
                dlc,
                version,
                difficulty,
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
            create virtual table achievements_fts using fts5(
                english_name,
                chinese_name,
                description,
                starting_conditions,
                completion_requirements,
                notes,
                dlc,
                version,
                difficulty,
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


def build_achievements(wiki_dir: Path, out: Path) -> int:
    wiki_dir = wiki_dir.resolve()
    candidates = [wiki_dir / "Achievements.html", wiki_dir / "Achievement.html"]
    source_path = next((path for path in candidates if path.exists()), None)
    if source_path is None:
        print("ERROR: Achievements.html not found")
        return 2
    rows = parse_achievement_rows(read_html(source_path))
    con, tokenizer = init_db(out)
    page_path = source_path.relative_to(wiki_dir).as_posix()
    insert_rows = [
        (
            item["english_name"],
            item["chinese_name"],
            item["description"],
            item["starting_conditions"],
            item["completion_requirements"],
            item["notes"],
            item["dlc"],
            item["version"],
            item["difficulty"],
            page_path,
            item["raw_text"],
        )
        for item in rows
    ]
    con.executemany(
        """
        insert into achievements(
            english_name, chinese_name, description, starting_conditions,
            completion_requirements, notes, dlc, version, difficulty, page_path, raw_text
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        insert_rows,
    )
    con.executemany(
        """
        insert into achievements_fts(
            english_name, chinese_name, description, starting_conditions,
            completion_requirements, notes, dlc, version, difficulty, page_path, raw_text
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        insert_rows,
    )
    set_stat(con, "wiki_dir", str(wiki_dir))
    set_stat(con, "source_page", page_path)
    set_stat(con, "indexed_achievements", len(rows))
    set_stat(con, "tokenizer", tokenizer)
    set_stat(con, "built_at", datetime.now(timezone.utc).isoformat())
    con.commit()
    con.close()
    print(f"Indexed {len(rows)} achievements from {page_path} -> {out}")
    return 0 if rows else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-dir", default=Path("wiki"), type=Path)
    parser.add_argument("--out", default=Path("data/achievements.sqlite"), type=Path)
    args = parser.parse_args()
    return build_achievements(args.wiki_dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
