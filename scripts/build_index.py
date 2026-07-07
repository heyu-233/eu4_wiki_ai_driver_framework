#!/usr/bin/env python3
"""Build a lightweight SQLite FTS index for the local EU4 wiki dump."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
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
    re.compile(r"^List_", re.IGNORECASE),
    re.compile(r".*disambiguation.*", re.IGNORECASE),
    re.compile(r".*Achievement.*", re.IGNORECASE),
    re.compile(r".*当前任务.*", re.IGNORECASE),
    re.compile(r".*补完向.*", re.IGNORECASE),
    re.compile(r".*修复向.*", re.IGNORECASE),
)

SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas"}
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


@dataclass
class Chunk:
    section: str
    text: str


class WikiHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self._in_title = False
        self._skip_depth = 0
        self._content_depth = 0
        self._capture_all = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "meta" and attrs_dict.get("name", "").lower() == "description":
            self.meta_description = html.unescape(attrs_dict.get("content", "")).strip()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        class_attr = attrs_dict.get("class", "")
        id_attr = attrs_dict.get("id", "")
        if (
            "liberty-content-main" in class_attr
            or id_attr in {"mw-content-text", "bodyContent", "content"}
        ):
            self._content_depth += 1
        elif self._content_depth:
            self._content_depth += 1
        if (self._content_depth or self._capture_all) and tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._content_depth:
            if tag in BLOCK_TAGS:
                self._parts.append("\n")
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if self._content_depth or self._capture_all:
            self._parts.append(data)

    @property
    def content_text(self) -> str:
        return normalize_text(" ".join(self._parts))


def normalize_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_title(raw_title: str, fallback: str) -> str:
    title = normalize_text(raw_title)
    title = re.sub(r"\s*-\s*欧陆风云4百科.*$", "", title)
    title = re.sub(r"\s*-\s*欧陸風雲4百科.*$", "", title)
    return title or fallback


def is_candidate(path: Path, wiki_dir: Path) -> bool:
    rel = path.relative_to(wiki_dir).as_posix()
    first = rel.split("/", 1)[0]
    if path.suffix.lower() != ".html" or first.startswith(EXCLUDED_PREFIXES):
        return False
    stem = path.stem
    return not any(pattern.match(stem) for pattern in EXCLUDED_NAME_PATTERNS)


def read_html(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def extract_page(path: Path) -> tuple[str, str]:
    source = read_html(path)
    if not source:
        return "", ""
    fast_title, fast_text = fast_extract_head(source, path)
    if len(fast_text) >= 120:
        return fast_title, strip_boilerplate(fast_text)
    parser = WikiHTMLParser()
    try:
        parser.feed(source)
    except Exception:
        pass
    fallback_title = path.stem.replace("_", " ")
    title = clean_title(parser.title, fallback_title)
    text = parser.content_text
    if len(text) < 80 and parser.meta_description:
        text = normalize_text(parser.meta_description)
    text = strip_boilerplate(text)
    return title, text


def fast_extract_head(source: str, path: Path) -> tuple[str, str]:
    fallback_title = path.stem.replace("_", " ")
    title_match = re.search(r"<title>(.*?)</title>", source, flags=re.IGNORECASE | re.DOTALL)
    title = clean_title(title_match.group(1), fallback_title) if title_match else fallback_title
    desc_match = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']\s*/?>',
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not desc_match:
        desc_match = re.search(
            r'<meta\s+content=["\'](.*?)["\']\s+name=["\']description["\']\s*/?>',
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
    text = normalize_text(desc_match.group(1)) if desc_match else ""
    return title, text


def strip_boilerplate(text: str) -> str:
    noisy_patterns = [
        r"Notice: Undefined index:.*?(?=\n|$)",
        r"Retrieved from .*?(?=\n|$)",
        r"除非另有声明.*?(?=\n|$)",
        r"隐私政策.*?(?=\n|$)",
        r"这条信息可能已不适合当前\s*版本\s*，?最后更新于[0-9.]+。?",
        r"此信息可能已落后\s*版本\s*，?最后更新于[0-9.]+。?",
    ]
    for pattern in noisy_patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return normalize_text(text)


def is_low_value_page(rel_path: str, title: str, text: str) -> bool:
    lowered_path = rel_path.lower()
    if title in {"成就", "Achievement", "Achievements"}:
        return True
    if "成就" in title and "达成条件" in text and "开局条件" in text:
        return True
    if any(bit in lowered_path for bit in ("achievement", "achievements", "disambiguation")):
        return True
    if any(bit in rel_path for bit in ("列表", "总决议列表", "当前任务", "补完向", "修复向", "消歧义")):
        return True
    if title in {"总决议列表", "列表", "索引"}:
        return True
    if title.startswith("欧陆风云4百科:"):
        return True
    return False


def split_chunks(text: str, max_chars: int = 1800, overlap: int = 80) -> list[Chunk]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[Chunk] = []
    section = "正文"
    buf: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal buf, current_len
        joined = normalize_text("\n".join(buf))
        if len(joined) >= 40:
            chunks.append(Chunk(section=section, text=joined[:max_chars]))
        if joined and overlap > 0:
            tail = joined[-overlap:]
            buf = [tail]
            current_len = len(tail)
        else:
            buf = []
            current_len = 0

    for line in lines:
        if is_section_heading(line):
            if buf:
                flush()
                buf = []
                current_len = 0
            section = line[:120]
            continue
        if current_len + len(line) + 1 > max_chars and buf:
            flush()
        buf.append(line)
        current_len += len(line) + 1
    if buf:
        flush()
    if not chunks and len(text) >= 40:
        chunks.append(Chunk(section="正文", text=text[:max_chars]))
    return chunks


def is_section_heading(line: str) -> bool:
    if len(line) > 80:
        return False
    if re.match(r"^\d+(\.\d+)*\s+\S+", line):
        return True
    return bool(re.match(r"^[\u4e00-\u9fffA-Za-z0-9 .:：·-]{2,40}$", line)) and not line.endswith(("。", "，", ",", "."))


def init_db(out: Path) -> tuple[sqlite3.Connection, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (out, out.with_name(out.name + "-wal"), out.with_name(out.name + "-shm")):
        if candidate.exists():
            candidate.unlink()
    con = sqlite3.connect(out)
    con.execute("pragma journal_mode=wal")
    con.executescript(
        """
        create table pages (
            id integer primary key,
            path text not null unique,
            title text not null,
            text_chars integer not null,
            chunk_count integer not null
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
            create virtual table chunks using fts5(
                title,
                section,
                body,
                path unindexed,
                page_id unindexed,
                tokenize='trigram'
            )
            """
        )
    except sqlite3.Error:
        tokenizer = "unicode61"
        con.execute(
            """
            create virtual table chunks using fts5(
            title,
            section,
            body,
            path unindexed,
            page_id unindexed,
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


def build_index(wiki_dir: Path, out: Path, min_coverage: float) -> int:
    wiki_dir = wiki_dir.resolve()
    paths = sorted(p for p in wiki_dir.rglob("*.html") if is_candidate(p, wiki_dir))
    con, tokenizer = init_db(out)
    candidate_pages = 0
    indexed_pages = 0
    indexed_chunks = 0
    skipped_low_value = 0
    failures: list[dict[str, str]] = []

    for i, path in enumerate(paths, start=1):
        title, text = extract_page(path)
        rel = path.relative_to(wiki_dir).as_posix()
        if title and is_low_value_page(rel, title, text):
            skipped_low_value += 1
            continue
        candidate_pages += 1
        chunks = split_chunks(text)
        if title and chunks:
            cur = con.execute(
                "insert into pages(path, title, text_chars, chunk_count) values (?, ?, ?, ?)",
                (rel, title, len(text), len(chunks)),
            )
            page_id = cur.lastrowid
            con.executemany(
                "insert into chunks(title, section, body, path, page_id) values (?, ?, ?, ?, ?)",
                [(title, c.section, c.text, rel, page_id) for c in chunks],
            )
            indexed_pages += 1
            indexed_chunks += len(chunks)
        elif len(failures) < 100:
            failures.append({"path": rel, "reason": "no extractable title/text"})

        if i % 500 == 0:
            con.commit()
            print(f"processed {i}/{len(paths)} pages, indexed {indexed_pages}", flush=True)

    coverage = indexed_pages / candidate_pages if candidate_pages else 0.0
    set_stat(con, "wiki_dir", str(wiki_dir))
    set_stat(con, "candidate_pages", candidate_pages)
    set_stat(con, "indexed_pages", indexed_pages)
    set_stat(con, "indexed_chunks", indexed_chunks)
    set_stat(con, "skipped_low_value_pages", skipped_low_value)
    set_stat(con, "coverage", coverage)
    set_stat(con, "min_coverage", min_coverage)
    set_stat(con, "tokenizer", tokenizer)
    set_stat(con, "built_at", datetime.now(timezone.utc).isoformat())
    set_stat(con, "failures", failures)
    con.commit()
    con.close()

    print(
        f"Indexed {indexed_pages}/{candidate_pages} candidate pages "
        f"({coverage:.1%}), {indexed_chunks} chunks -> {out}"
    )
    if coverage < min_coverage:
        print(f"ERROR: coverage {coverage:.1%} is below required {min_coverage:.1%}", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-dir", default="wiki", type=Path)
    parser.add_argument("--out", default=Path("data/wiki_index.sqlite"), type=Path)
    parser.add_argument("--min-coverage", default=0.8, type=float)
    args = parser.parse_args()
    return build_index(args.wiki_dir, args.out, args.min_coverage)


if __name__ == "__main__":
    raise SystemExit(main())
