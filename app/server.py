#!/usr/bin/env python3
"""Local EU4 wiki knowledge-base server."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "wiki_index.sqlite"
DEFAULT_EFFECT_DB = ROOT / "data" / "effect_sources.sqlite"
DEFAULT_ACHIEVEMENT_DB = ROOT / "data" / "achievements.sqlite"
DEFAULT_MISSION_DB = ROOT / "data" / "mission_sources.sqlite"
DEFAULT_ENTITY_DB = ROOT / "data" / "entity_registry.sqlite"
DEFAULT_WIKI_DIR = ROOT / "wiki"
DEFAULT_LLM_CONFIG = ROOT / "config" / "llm_config.json"
DEFAULT_ANSWERING_SKILL = ROOT / "skills" / "eu4_wiki_answering.md"
MAX_HISTORY_TURNS = 8
MAX_TOOL_CALLS = 3
MAX_TOOL_RESULTS = 5
MAX_PLANNED_SEARCHES = 5
MAX_PLANNED_PAGE_SEARCHES = 3
MAX_PLANNED_EFFECT_SEARCHES = 5
MAX_PLANNED_ACHIEVEMENT_SEARCHES = 3
MAX_PLANNED_MISSION_SEARCHES = 3
MAX_PLANNED_TOTAL_SEARCHES = 6
MAX_PAGE_CONTEXT_MATCHES = 3
MAX_PAGE_CONTEXT_CHARS = 1800


SEARCH_WIKI_TOOL = {
    "type": "function",
    "function": {
        "name": "search_wiki",
        "description": "检索本地 EU4 中文 wiki 索引。适合在初始片段不足、需要查任务条件/奖励、机制公式、同义词或英文术语时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词。可以使用中文、英文、缩写或组合词，例如：萨卢佐 意大利小国任务 AE、shock damage pips。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回片段数量，1-5，默认 5。",
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_PAGE_CONTEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_page_context",
        "description": "在指定本地 wiki HTML 页面内按关键词检索，并返回命中位置附近的清洗文本。适合 search_wiki 找到页面但片段缺少任务完成条件、奖励表格或公式细节时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "wiki 页面路径，必须来自检索结果，例如：意大利小国任务.html 或 Battle.html。",
                },
                "query": {
                    "type": "string",
                    "description": "页面内检索关键词，例如：侵略扩张、AE、篡夺控制权、−15%、shock damage。",
                },
                "radius": {
                    "type": "integer",
                    "description": "返回命中点前后字符范围，默认 1600，最大 3000。",
                },
            },
            "required": ["path", "query"],
        },
    },
}

SEARCH_EFFECT_SOURCES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_effect_sources",
        "description": "反查 EU4 wiki 中“什么来源给什么效果/条件/权重”。覆盖事件、任务、决议、理念、政策、政府改革、宗教/信仰、阶层/特权、伟大工程、修正列表、特质。适合查询减少 AE、外交声誉、传教强度、行政效率、事件 ID、任务奖励、特质效果和特质权重等。",
        "parameters": {
            "type": "object",
            "properties": {
                "effect_query": {
                    "type": "string",
                    "description": "要反查的效果、修正 key、中文名或俗称，例如：侵略扩张影响、aggressive_expansion_impact、外交声誉、improve_relation_modifier、特质、权重、外交技能。",
                },
                "source_type": {
                    "type": "string",
                    "description": "可选来源类型：event, mission, decision, idea, policy, reform, religion, estate, great_project, modifier, trait。",
                },
                "scope": {
                    "type": "string",
                    "description": "可选范围，例如国家名、任务树、理念组、宗教名或特质对象：萨卢佐、奥地利、影响理念、天主教、ruler、heir、consort、general、admiral、ai。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量，1-8，默认 5。",
                },
            },
            "required": ["effect_query"],
        },
    },
}

SEARCH_ACHIEVEMENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_achievements",
        "description": "检索 EU4 成就索引。适合查询成就名称、开始条件、完成需求、备注、DLC、版本、难度和简易路线。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "成就名、中文名、英文名、国家、条件或路线关键词，例如：AAA级信用、Ideas Guy、大明 成就、拜占庭 成就。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量，1-8，默认 5。",
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_MISSION_SOURCES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_mission_sources",
        "description": (
            "Structured lookup for EU4 mission tables. Use this before normal wiki search when the user asks about "
            "a country's missions, mission requirements, rewards, prerequisites, or mission-tree effects. It first "
            "locates likely mission pages, then searches mission rows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Mission question or reward/effect query, e.g. '里加任务', '萨卢佐 减少AE', '外交吞并花费'.",
                },
                "scope": {
                    "type": "string",
                    "description": "Optional country, mission tree, or page hint, e.g. '里加', 'Riga', '奥地利'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of mission rows to return, 1-8.",
                },
            },
            "required": ["query"],
        },
    },
}

AVAILABLE_TOOLS = [
    SEARCH_WIKI_TOOL,
    SEARCH_PAGE_CONTEXT_TOOL,
    SEARCH_EFFECT_SOURCES_TOOL,
    SEARCH_ACHIEVEMENTS_TOOL,
    SEARCH_MISSION_SOURCES_TOOL,
]


QUERY_EXPANSIONS = {
    "萨卢佐": "Saluzzo 意大利小国任务",
    "薩盧佐": "Saluzzo",
    "Saluzzo": "萨卢佐",
    "任务": "mission Missions",
    "mission": "任务",
    "missions": "任务",
    "冲击": "shock",
    "shock": "冲击",
    "伤害": "damage casualties",
    "damage": "伤害",
    "兵种": "unit units infantry cavalry artillery",
    "点数": "pips 陆战 单位 火力 冲击 士气",
    "pips": "点数 陆战 单位 火力 冲击 士气",
    "ae": "侵略扩张 aggressive expansion",
    "AE": "侵略扩张 aggressive expansion",
    "侵略扩张": "aggressive expansion AE",
    "玛丽": "坠马 勃艮第女公爵去世 勃艮第继承危机 incidents_bur_inheritance.5 incidents_bur_inheritance.501 Burgundian inheritance",
    "玛丽小姐": "坠马 勃艮第女公爵去世 勃艮第继承危机 incidents_bur_inheritance.5 incidents_bur_inheritance.501 Burgundian inheritance",
    "坠马": "玛丽 勃艮第女公爵去世 勃艮第继承危机 incidents_bur_inheritance.5 incidents_bur_inheritance.501 Burgundian inheritance",
    "事件指令": "event 控制台指令 console command",
}

NOISE_PATH_PATTERNS = (
    re.compile(r"(^|/)1\.\d+(?:\.\d+)?(?:_版本)?\.html$", re.IGNORECASE),
    re.compile(r"(^|/)1\.\d+\.X_版本\.html$", re.IGNORECASE),
    re.compile(r"modding", re.IGNORECASE),
    re.compile(r"当前任务", re.IGNORECASE),
    re.compile(r"补完向|修复向", re.IGNORECASE),
    re.compile(r"achievement", re.IGNORECASE),
    re.compile(r"disambiguation", re.IGNORECASE),
    re.compile(r"(^|/)List_", re.IGNORECASE),
    re.compile(r"列表|总决议列表|消歧义", re.IGNORECASE),
)

NOISE_TEXT_MARKERS = (
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


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EU4 Wiki 知识库</title>
  <style>
    :root {
      color-scheme: light;
      --ink: oklch(22% 0.026 92);
      --olive-dark: oklch(33% 0.055 104);
      --olive: oklch(43% 0.067 105);
      --olive-soft: oklch(87% 0.035 101);
      --muted: oklch(49% 0.034 94);
      --line: oklch(85% 0.019 91);
      --hairline: oklch(74% 0.032 95 / 34%);
      --sand: oklch(93% 0.028 86);
      --cream: oklch(97% 0.018 87);
      --paper: oklch(98.5% 0.012 88);
      --deep: oklch(23% 0.039 87);
      --blue-ink: oklch(35% 0.055 223);
      --warn: oklch(55% 0.105 55);
      --space-xs: 4px;
      --space-sm: 8px;
      --space-md: 12px;
      --space-lg: 16px;
      --space-xl: 24px;
      --space-2xl: 32px;
      --radius: 8px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        linear-gradient(90deg, oklch(55% 0.04 98 / 5%) 1px, transparent 1px),
        linear-gradient(0deg, oklch(55% 0.04 98 / 4%) 1px, transparent 1px),
        radial-gradient(circle at 18% 8%, oklch(85% 0.036 98 / 38%), transparent 26rem),
        var(--sand);
      background-size: 44px 44px, 44px 44px, auto, auto;
      font-family: "Aptos", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
      font-size: 15px;
    }
    a { color: inherit; }
    .app-shell { width: min(100% - 32px, 1520px); margin: 0 auto; padding: var(--space-xl) 0; }
    header {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto;
      gap: var(--space-xl);
      align-items: end;
      padding: var(--space-xl) 0 var(--space-lg);
    }
    .brand { display: flex; gap: var(--space-md); align-items: center; min-width: 0; }
    .mark {
      width: 42px;
      height: 42px;
      display: grid;
      place-items: center;
      border: 1px solid var(--hairline);
      border-radius: var(--radius);
      background: color-mix(in oklch, var(--cream), var(--olive-soft) 30%);
      color: var(--olive-dark);
      font-family: Georgia, "Times New Roman", serif;
      font-size: 22px;
      font-weight: 700;
    }
    h1 {
      margin: 0;
      color: var(--olive-dark);
      font-family: Georgia, "Noto Serif SC", "SimSun", serif;
      font-size: 27px;
      font-weight: 700;
      line-height: 1.12;
      letter-spacing: 0;
    }
    .sub { margin: 6px 0 0; color: var(--muted); font-size: 13px; line-height: 1.55; }
    .top-actions { display: flex; gap: var(--sm, 8px); align-items: center; color: var(--muted); font-size: 12px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 30px;
      padding: 5px 10px;
      border: 1px solid var(--hairline);
      border-radius: 999px;
      background: oklch(98% 0.014 88 / 72%);
      white-space: nowrap;
    }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--olive); box-shadow: 0 0 0 3px oklch(62% 0.055 105 / 16%); }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(0, 1fr);
      height: calc(100vh - 150px);
      min-height: calc(100vh - 150px);
      border: 1px solid var(--hairline);
      border-radius: 14px;
      overflow: hidden;
      background: color-mix(in oklch, var(--cream), transparent 7%);
      box-shadow: 0 24px 80px oklch(31% 0.04 89 / 10%);
    }
    section { min-width: 0; }
    .query-panel {
      display: flex;
      flex-direction: column;
      gap: var(--space-lg);
      padding: var(--space-xl);
      border-color: var(--hairline);
      border-style: solid;
      border-width: 0 1px 0 0;
      background: oklch(96% 0.019 88 / 70%);
    }
    .workspace {
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
      min-height: 0;
      background: var(--paper);
    }
    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-md);
      margin: 0;
      color: var(--olive-dark);
      font-size: 11px;
      font-weight: 750;
      letter-spacing: .13em;
      text-transform: uppercase;
    }
    label { display: block; margin: 0 0 var(--space-sm); color: var(--muted); font-size: 12px; font-weight: 650; }
    textarea, input {
      width: 100%;
      border: 1px solid var(--hairline);
      border-radius: var(--radius);
      padding: 12px 13px;
      font: inherit;
      background: oklch(99% 0.008 88);
      color: var(--ink);
      outline: none;
      transition: border-color 160ms ease, background 160ms ease, box-shadow 160ms ease;
    }
    textarea:focus, input:focus {
      border-color: oklch(49% 0.067 105 / 76%);
      background: var(--paper);
      box-shadow: 0 0 0 3px oklch(65% 0.055 105 / 13%);
    }
    textarea { min-height: 154px; resize: vertical; line-height: 1.58; }
    .row { display: flex; flex-wrap: wrap; gap: var(--space-sm); align-items: center; }
    button {
      min-height: 36px;
      border: 1px solid var(--olive);
      background: var(--olive);
      color: var(--cream);
      border-radius: 999px;
      padding: 8px 15px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: transform 160ms ease, background 160ms ease, border-color 160ms ease, opacity 160ms ease;
    }
    button:hover { background: var(--olive-dark); transform: translateY(-1px); }
    button.secondary { color: var(--olive-dark); background: oklch(98% 0.012 88 / 70%); border-color: var(--hairline); }
    button.secondary:hover { background: var(--paper); border-color: oklch(57% 0.055 105 / 42%); }
    button:disabled { opacity: .58; cursor: wait; transform: none; }
    .status {
      padding: 12px;
      border: 1px solid var(--hairline);
      border-radius: var(--radius);
      background: oklch(98% 0.014 88 / 66%);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }
    .quick-prompts { display: grid; gap: var(--space-sm); }
    .quick-prompts button {
      width: 100%;
      justify-content: flex-start;
      border-radius: var(--radius);
      padding: 9px 11px;
      text-align: left;
      color: var(--muted);
      background: transparent;
      border-color: var(--hairline);
      font-size: 12px;
      font-weight: 650;
    }
    .quick-prompts button:hover { color: var(--olive-dark); background: oklch(99% 0.009 88); }
    .answer-head, .results-head {
      padding: var(--space-lg) var(--space-xl);
      border-bottom: 1px solid var(--hairline);
      background: color-mix(in oklch, var(--paper), var(--cream) 45%);
    }
    .answer-wrap { padding: var(--space-xl); border-bottom: 1px solid var(--hairline); }
    .answer {
      max-width: 82ch;
      white-space: normal;
      line-height: 1.72;
      overflow-wrap: anywhere;
      color: oklch(27% 0.028 88);
    }
    .answer h3, .answer h4, .answer h5 { margin: 16px 0 8px; color: var(--olive-dark); line-height: 1.32; }
    .answer p { margin: 8px 0; }
    .answer ul { margin: 8px 0 10px 22px; padding: 0; }
    .answer ol { margin: 8px 0 10px 24px; padding: 0; }
    .answer li { margin: 4px 0; }
    .answer blockquote {
      margin: 10px 0;
      padding: 10px 12px;
      border: 1px solid var(--hairline);
      border-radius: var(--radius);
      background: oklch(97% 0.018 88);
      color: var(--muted);
    }
    .answer code { padding: 1px 5px; border-radius: 5px; background: oklch(92% 0.024 90); font-family: "Cascadia Mono", Consolas, monospace; font-size: .92em; }
    .answer pre { margin: 12px 0; padding: 13px 14px; border: 1px solid oklch(35% 0.035 88); border-radius: var(--radius); background: var(--deep); color: var(--cream); overflow: auto; white-space: pre; }
    .answer pre code { padding: 0; background: transparent; color: inherit; font-size: 13px; }
    .answer .table-wrap { margin: 12px 0; overflow-x: auto; border: 1px solid var(--hairline); border-radius: var(--radius); }
    .answer table { width: 100%; border-collapse: collapse; min-width: 420px; background: var(--paper); white-space: normal; }
    .answer th, .answer td { padding: 9px 11px; border-bottom: 1px solid var(--hairline); border-right: 1px solid var(--hairline); vertical-align: top; text-align: left; }
    .answer th:last-child, .answer td:last-child { border-right: 0; }
    .answer tr:last-child td { border-bottom: 0; }
    .answer th { background: oklch(94% 0.02 89); color: var(--olive-dark); font-weight: 750; }
    .answer .math-block { margin: 12px 0; padding: 12px 13px; border: 1px solid var(--hairline); border-radius: var(--radius); background: oklch(97% 0.022 86); color: oklch(31% 0.038 83); font-family: Cambria Math, "Times New Roman", serif; overflow-x: auto; white-space: pre-wrap; }
    .answer .math-inline { padding: 1px 5px; border-radius: 5px; background: oklch(94% 0.027 86); font-family: Cambria Math, "Times New Roman", serif; }
    .answer hr { border: 0; border-top: 1px solid var(--hairline); margin: 14px 0; }
    .citations { display: flex; flex-wrap: wrap; gap: var(--space-sm); margin-top: var(--space-md); }
    .citation {
      display: inline-flex;
      max-width: 100%;
      padding: 5px 10px;
      border: 1px solid var(--hairline);
      border-radius: 999px;
      background: oklch(96% 0.018 89);
      color: var(--olive-dark);
      text-decoration: none;
      font-size: 12px;
      font-weight: 650;
    }
    .sources-region { min-height: 0; display: flex; flex-direction: column; }
    .results-scroll { min-height: 0; overflow: auto; padding: 0 var(--space-xl) var(--space-xl); }
    .result {
      display: grid;
      gap: 6px;
      padding: 14px 0;
      border-bottom: 1px solid var(--hairline);
    }
    .result h3 { margin: 0; color: var(--olive-dark); font-size: 15px; line-height: 1.35; }
    .result h3 span { color: var(--muted); font-weight: 500; }
    .result .meta { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .result .meta a { color: var(--blue-ink); text-decoration-color: oklch(60% 0.055 223 / 36%); text-underline-offset: 3px; }
    .result p { margin: 0; color: oklch(34% 0.024 88); font-size: 13px; line-height: 1.6; }
    .empty { color: var(--muted); font-size: 13px; }
    @media (max-width: 980px) {
      .app-shell { width: min(100% - 20px, 760px); padding: var(--space-md) 0; }
      header { grid-template-columns: 1fr; align-items: start; gap: var(--space-md); }
      main { grid-template-columns: 1fr; min-height: auto; }
      main { height: auto; }
      .query-panel { border-width: 0 0 1px 0; }
      .workspace { display: block; }
      .results-scroll { max-height: none; overflow: visible; }
    }
    @media (max-width: 560px) {
      .app-shell { width: min(100% - 14px, 760px); }
      .query-panel, .answer-wrap, .answer-head, .results-head { padding: var(--space-lg); }
      h1 { font-size: 23px; }
      .brand { align-items: flex-start; }
      .mark { width: 38px; height: 38px; }
      button { width: 100%; justify-content: center; }
      .top-actions { flex-wrap: wrap; }
    }
    body {
      height: 100vh;
      overflow: hidden;
      background: #fdfcfa;
      color: #0c0c0d;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    }
    .kb-scene {
      position: relative;
      min-height: 100vh;
      overflow: hidden;
      background: #f8f3eb;
    }
    .kb-painting {
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(180deg, rgba(43, 40, 24, .12), rgba(43, 40, 24, .02) 38%, rgba(43, 40, 24, .26)),
        url("/assets/eu4-bg.png");
      background-size: cover;
      background-position: center;
      transform: scale(1.012);
    }
    .kb-topbar {
      position: absolute;
      z-index: 5;
      left: clamp(24px, 6vw, 120px);
      right: clamp(24px, 6vw, 120px);
      top: clamp(22px, 5vw, 70px);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      color: #4d4830;
    }
    .kb-brand {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      color: #4d4830;
      text-shadow: 0 1px 8px rgba(248, 243, 235, .42);
    }
    .kb-brand-mark {
      display: grid;
      place-items: center;
      width: 38px;
      height: 38px;
      border: 1px solid rgba(98, 92, 59, .18);
      border-radius: 8px;
      background: rgba(248, 243, 235, .52);
      color: #4d4830;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 20px;
      font-weight: 700;
      backdrop-filter: blur(7px);
    }
    .kb-brand h1 {
      margin: 0;
      color: #4d4830;
      font-size: 22px;
      font-weight: 650;
      line-height: 1.05;
      letter-spacing: -.01em;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", sans-serif;
    }
    .kb-brand p {
      margin: 4px 0 0;
      color: rgba(77, 72, 48, .70);
      font-size: 12px;
      font-weight: 600;
    }
    .kb-status-row {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }
    .kb-status-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 30px;
      padding: 5px 10px;
      border: 1px solid rgba(98, 92, 59, .14);
      border-radius: 999px;
      background: rgba(248, 243, 235, .56);
      color: rgba(77, 72, 48, .74);
      font-size: 12px;
      font-weight: 600;
      backdrop-filter: blur(9px);
      white-space: nowrap;
    }
    .kb-status-pill .dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #625c3b;
      box-shadow: 0 0 0 3px rgba(98, 92, 59, .14);
    }
    .kb-stage {
      position: relative;
      z-index: 2;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr) auto;
      width: min(100% - 32px, 1180px);
      height: 100vh;
      min-height: 100vh;
      margin: 0 auto;
      padding: clamp(108px, 14vh, 150px) 0 clamp(18px, 2.6vh, 30px);
      border: 0;
      border-radius: 0;
      overflow: visible;
      background: transparent;
      box-shadow: none;
    }
    .kb-dialog-scroll {
      min-height: 0;
      overflow: hidden;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
      gap: 18px;
      align-items: start;
      padding: 0 4px 22px;
      scrollbar-color: rgba(98, 92, 59, .42) transparent;
    }
    .kb-floating-card {
      width: min(100%, 720px);
      margin: 0 auto;
      border: 1px solid rgba(98, 92, 59, .10);
      border-radius: 18px;
      background: rgba(248, 243, 235, .30);
      color: #252218;
      box-shadow: 0 22px 80px rgba(43, 40, 24, .10);
      backdrop-filter: blur(16px) saturate(.86);
    }
    .kb-answer-card {
      padding: clamp(20px, 3vw, 32px);
      max-height: calc(100vh - 285px);
      overflow-y: auto;
    }
    .kb-section-label {
      margin: 0 0 14px;
      color: rgba(77, 72, 48, .62);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .14em;
      text-transform: uppercase;
    }
    .answer {
      max-width: 76ch;
      color: #17140e;
      font-size: 15px;
      line-height: 1.72;
      text-shadow: 0 1px 12px rgba(248, 243, 235, .20);
    }
    .answer h3, .answer h4, .answer h5 { color: #2f2b1b; }
    .answer blockquote,
    .answer .math-block,
    .answer .table-wrap {
      background: rgba(253, 252, 250, .58);
      backdrop-filter: blur(6px);
      border-color: rgba(98, 92, 59, .16);
    }
    .answer table { background: rgba(253, 252, 250, .68); }
    .answer th { background: rgba(98, 92, 59, .10); color: #4d4830; }
    .answer code, .answer .math-inline { background: rgba(98, 92, 59, .12); color: #17140e; }
    .answer pre {
      background: rgba(43, 40, 24, .92);
      color: #f8f3eb;
    }
    .citations {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }
    .citation {
      border-color: rgba(98, 92, 59, .15);
      background: rgba(248, 243, 235, .48);
      color: #4d4830;
      backdrop-filter: blur(7px);
    }
    .kb-sources {
      width: 100%;
      margin: 0;
      border: 1px solid rgba(98, 92, 59, .12);
      border-radius: 28px;
      background: rgba(248, 243, 235, .38);
      color: #252218;
      backdrop-filter: blur(16px) saturate(.88);
      box-shadow: 0 22px 80px rgba(43, 40, 24, .10);
      overflow: hidden;
    }
    .kb-sources summary {
      cursor: pointer;
      padding: 24px 24px 10px;
      color: rgba(77, 72, 48, .62);
      font-size: 19px;
      font-weight: 500;
      letter-spacing: 0;
      text-transform: none;
      list-style: none;
    }
    .kb-sources summary::-webkit-details-marker { display: none; }
    .results-scroll {
      max-height: calc(100vh - 238px);
      overflow-y: auto;
      padding: 4px 24px 24px;
      scrollbar-color: rgba(98, 92, 59, .38) transparent;
    }
    .result {
      border-bottom-color: rgba(98, 92, 59, .12);
      padding: 12px 0;
    }
    .result h3 { color: #3a351f; font-size: 14px; font-weight: 580; }
    .result h3 span, .result .meta { color: rgba(77, 72, 48, .62); }
    .result .meta a { color: #4d4830; text-decoration-color: rgba(77, 72, 48, .28); }
    .result p {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      color: rgba(23, 20, 14, .78);
    }
    .kb-input-dock {
      width: min(100%, 920px);
      margin: 0 auto;
      border: 1px solid rgba(98, 92, 59, .16);
      border-radius: 26px;
      background: rgba(248, 243, 235, .58);
      box-shadow: 0 18px 70px rgba(43, 40, 24, .16);
      backdrop-filter: blur(16px) saturate(.88);
      padding: 12px 14px;
    }
    .kb-input-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: end;
    }
    .kb-input-actions {
      display: grid;
      grid-template-columns: 42px;
      gap: 8px;
      justify-items: end;
    }
    label.kb-input-label {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
    }
    textarea#question {
      min-height: 64px;
      max-height: 150px;
      resize: vertical;
      border: 0;
      border-radius: 14px;
      background: transparent;
      color: #17140e;
      padding: 12px 4px 10px;
      line-height: 1.55;
      box-shadow: none;
      backdrop-filter: none;
      font-size: 16px;
    }
    textarea#question::placeholder { color: rgba(77, 72, 48, .52); }
    textarea#question:focus {
      background: transparent;
      box-shadow: none;
    }
    button {
      min-height: 34px;
      border: 1px solid rgba(98, 92, 59, .12);
      border-radius: 999px;
      background: rgba(248, 243, 235, .34);
      color: #4d4830;
      padding: 7px 12px;
      font-size: 14px;
      font-weight: 700;
      transition: background .18s ease, transform .18s ease, border-color .18s ease;
    }
    button:hover { background: rgba(248, 243, 235, .56); transform: translateY(-1px); }
    #askBtn {
      width: 42px;
      height: 42px;
      min-height: 42px;
      display: inline-grid;
      place-items: center;
      border: 0;
      background: rgba(98, 92, 59, .88);
      color: #f8f3eb;
      padding: 0;
      font-size: 0;
    }
    #askBtn::before {
      content: "↑";
      font-size: 26px;
      line-height: 1;
      font-weight: 500;
      transform: translateY(-1px);
    }
    #askBtn:hover { background: #4d4830; }
    button.secondary {
      color: rgba(77, 72, 48, .62);
      background: transparent;
      border-color: transparent;
    }
    button.secondary:hover {
      background: rgba(248, 243, 235, .48);
      border-color: rgba(98, 92, 59, .14);
    }
    #searchBtn,
    #clearBtn {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }
    .kb-meta-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-top: 4px;
    }
    .status {
      display: none;
    }
    .quick-prompts {
      display: none;
      flex-wrap: wrap;
      gap: 6px;
      justify-content: flex-end;
    }
    .kb-tag-row.open .quick-prompts { display: flex; }
    .quick-prompts button {
      width: auto;
      min-height: 30px;
      padding: 4px 10px;
      border-radius: 999px;
      color: rgba(77, 72, 48, .74);
      background: rgba(248, 243, 235, .32);
      border-color: rgba(98, 92, 59, .12);
      font-size: 12px;
      font-weight: 650;
    }
    .quick-prompts button:hover {
      color: #4d4830;
      background: rgba(248, 243, 235, .54);
    }
    .kb-plus {
      display: inline-grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border: 0;
      border-radius: 999px;
      background: transparent;
      color: rgba(77, 72, 48, .56);
      font-size: 30px;
      font-weight: 300;
      line-height: 1;
      padding: 0;
    }
    .kb-plus:hover {
      background: rgba(248, 243, 235, .48);
      color: #4d4830;
    }
    .kb-input-tools {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-top: 4px;
    }
    .kb-tag-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
    }
    @media (max-width: 760px) {
      body { overflow: auto; }
      .kb-stage {
        width: min(100% - 20px, 720px);
        height: auto;
        min-height: 100vh;
        padding-top: 118px;
      }
      .kb-dialog-scroll {
        overflow: visible;
        display: block;
      }
      .kb-answer-card {
        max-height: none;
      }
      .kb-sources {
        margin-top: 12px;
        border-radius: 18px;
      }
      .results-scroll {
        max-height: 220px;
      }
      .kb-topbar {
        left: 14px;
        right: 14px;
        top: 16px;
        align-items: flex-start;
        flex-direction: column;
      }
      .kb-input-row { grid-template-columns: minmax(0, 1fr) 42px; }
      .kb-input-actions { justify-content: stretch; }
      .kb-meta-row { align-items: stretch; flex-direction: column; }
      .quick-prompts { justify-content: flex-start; }
      .kb-status-row { justify-content: flex-start; }
      .kb-brand h1 { font-size: 20px; }
    }
    .kb-scene {
      --dock-w: min(100%, 820px);
    }
    .kb-stage {
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 18px;
      width: min(100% - 32px, 1180px);
      padding-top: 108px;
      padding-bottom: 30px;
    }
    .kb-dialog-scroll {
      display: none;
      min-height: 0;
    }
    .kb-scene.has-output .kb-stage {
      justify-content: space-between;
    }
    .kb-scene.has-output .kb-dialog-scroll {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(286px, 370px);
      gap: 18px;
      align-items: start;
      overflow: hidden;
      padding: 0 4px;
    }
    .kb-floating-card {
      width: min(100%, 720px);
      border-radius: 22px;
      background: rgba(248, 243, 235, .24);
      border-color: rgba(98, 92, 59, .10);
      box-shadow: 0 22px 80px rgba(43, 40, 24, .08);
      backdrop-filter: blur(18px) saturate(.86);
    }
    .kb-answer-card {
      max-height: calc(100vh - 270px);
      overflow-y: auto;
    }
    .kb-sources {
      border-radius: 24px;
      background: rgba(248, 243, 235, .36);
      border-color: rgba(98, 92, 59, .12);
      box-shadow: 0 22px 70px rgba(43, 40, 24, .10);
      backdrop-filter: blur(18px) saturate(.86);
    }
    .kb-sources summary {
      padding: 22px 24px 10px;
      color: rgba(77, 72, 48, .60);
      font-size: 18px;
    }
    .results-scroll {
      max-height: calc(100vh - 252px);
      padding: 4px 24px 24px;
    }
    .kb-input-dock {
      width: var(--dock-w);
      min-height: 138px;
      border-radius: 26px;
      padding: 14px 18px 12px;
      background: rgba(248, 243, 235, .56);
      border: 1px solid rgba(98, 92, 59, .16);
      box-shadow: 0 18px 70px rgba(43, 40, 24, .16);
      backdrop-filter: blur(18px) saturate(.88);
    }
    .kb-scene:not(.has-output) .kb-input-dock {
      transform: translateY(12px);
    }
    textarea#question {
      width: 100%;
      min-height: 76px;
      max-height: 180px;
      padding: 4px 0 8px;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
      color: #17140e;
      font-size: 16px;
      line-height: 1.55;
    }
    textarea#question::placeholder {
      color: rgba(77, 72, 48, .48);
    }
    .kb-input-tools {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 4px;
    }
    .kb-tag-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .kb-plus {
      width: auto;
      height: 32px;
      min-height: 32px;
      padding: 0 6px 2px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: rgba(77, 72, 48, .56);
      font-size: 32px;
      font-weight: 250;
      line-height: 1;
    }
    .kb-plus:hover {
      background: transparent;
      color: #4d4830;
      transform: none;
    }
    .kb-plus:focus,
    .kb-plus:focus-visible,
    #askBtn:focus,
    #askBtn:focus-visible,
    .quick-prompts button:focus,
    .quick-prompts button:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(98, 92, 59, .16);
    }
    .quick-prompts {
      display: none;
      flex-wrap: wrap;
      gap: 6px;
    }
    .kb-tag-row.open .quick-prompts {
      display: flex;
    }
    .quick-prompts button {
      min-height: 28px;
      padding: 4px 9px;
      border-radius: 999px;
      color: rgba(77, 72, 48, .72);
      background: rgba(248, 243, 235, .34);
      border-color: rgba(98, 92, 59, .12);
      font-size: 12px;
      font-weight: 650;
    }
    .kb-input-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-left: auto;
    }
    #searchBtn,
    #clearBtn,
    .status {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }
    #askBtn {
      width: 42px;
      height: 42px;
      min-height: 42px;
      display: inline-grid;
      place-items: center;
      border: 0;
      border-radius: 50%;
      padding: 0;
      background: rgba(98, 92, 59, .82);
      color: #f8f3eb;
      font-size: 0;
      box-shadow: none;
    }
    #askBtn::before {
      content: "";
      width: 14px;
      height: 14px;
      border-top: 2px solid currentColor;
      border-left: 2px solid currentColor;
      transform: translateY(3px) rotate(45deg);
    }
    #askBtn::after {
      content: "";
      position: absolute;
      width: 2px;
      height: 18px;
      background: currentColor;
      transform: translateY(2px);
      border-radius: 2px;
    }
    #askBtn:hover {
      background: #4d4830;
      transform: translateY(-1px);
    }
    @media (max-width: 860px) {
      body { overflow: auto; }
      .kb-stage {
        min-height: 100vh;
        height: auto;
        width: min(100% - 20px, 760px);
        padding-top: 126px;
      }
      .kb-scene.has-output .kb-dialog-scroll {
        display: block;
        overflow: visible;
      }
      .kb-sources {
        margin-top: 12px;
      }
      .kb-answer-card {
        max-height: none;
      }
      .results-scroll {
        max-height: 230px;
      }
      .kb-input-dock {
        min-height: 132px;
      }
    }
    body {
      height: auto;
      min-height: 100vh;
      overflow-y: auto;
      overflow-x: hidden;
    }
    .kb-scene {
      min-height: 100vh;
      overflow: visible;
      padding-bottom: 190px;
    }
    .kb-scene:not(.has-output) {
      height: 100vh;
      min-height: 100vh;
      overflow: hidden;
      padding-bottom: 0;
    }
    .kb-painting {
      position: fixed;
      transform: none;
      overflow: hidden;
      background: #d7d0bd;
      background-image:
        linear-gradient(180deg, rgba(12, 18, 16, .16), rgba(248, 243, 235, .10) 42%, rgba(22, 24, 13, .30)),
        url("/assets/eu4-bg.png");
      background-size: cover;
      background-position: center;
    }
    .kb-bg-video {
      position: absolute;
      inset: 0;
      z-index: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      object-position: center 42%;
      opacity: 1;
      filter: saturate(.92) contrast(.94) brightness(.82);
      transform: scale(1.055);
    }
    .kb-painting::before {
      content: "";
      position: absolute;
      inset: -3%;
      z-index: 0;
      background-image:
        linear-gradient(180deg, rgba(12, 18, 16, .16), rgba(248, 243, 235, .10) 42%, rgba(22, 24, 13, .30)),
        url("/assets/eu4-bg.png");
      background-size: cover;
      background-position: center center;
      transform: scale(1.025) translate3d(0, 0, 0);
      animation: campaign-drift 42s ease-in-out infinite alternate;
      will-change: transform;
      opacity: 0;
    }
    .kb-painting::after {
      content: "";
      position: absolute;
      inset: 0;
      z-index: 1;
      background:
        radial-gradient(circle at 28% 18%, rgba(248, 243, 235, .12), transparent 34%),
        linear-gradient(90deg, rgba(248, 243, 235, .18), rgba(248, 243, 235, .03) 48%, rgba(77, 72, 48, .16));
      pointer-events: none;
    }
    @keyframes campaign-drift {
      0% { transform: scale(1.025) translate3d(-.6%, -.4%, 0); }
      50% { transform: scale(1.045) translate3d(.45%, .25%, 0); }
      100% { transform: scale(1.035) translate3d(-.25%, .55%, 0); }
    }
    @media (prefers-reduced-motion: reduce) {
      .kb-bg-video { display: none; }
      .kb-painting::before { animation: none; transform: scale(1.025); opacity: 1; }
    }
    .kb-topbar {
      position: fixed;
      right: auto;
      width: min(360px, calc(100% - 48px));
    }
    .kb-status-row {
      display: none;
    }
    .kb-stage {
      display: block;
      width: min(100% - 32px, 1180px);
      height: auto;
      min-height: 100vh;
      padding-top: 128px;
      padding-bottom: 0;
    }
    .kb-dialog-scroll {
      display: none;
      width: min(760px, calc(100% - 410px));
      min-height: 260px;
      padding: 0 0 220px;
      overflow: visible;
    }
    .kb-scene.has-output .kb-dialog-scroll {
      display: block;
      overflow: visible;
    }
    .kb-conversation {
      display: flex;
      flex-direction: column;
      gap: 18px;
      padding-bottom: 24px;
    }
    .kb-message {
      width: fit-content;
      max-width: min(720px, 100%);
      scroll-margin-top: 112px;
      border: 1px solid rgba(98, 92, 59, .10);
      border-radius: 20px;
      background: rgba(248, 243, 235, .30);
      color: #17140e;
      box-shadow: 0 18px 60px rgba(43, 40, 24, .08);
      backdrop-filter: blur(18px) saturate(.86);
      padding: 18px 22px;
    }
    .kb-user-message {
      align-self: flex-end;
      max-width: min(700px, 86%);
      background: rgba(248, 243, 235, .42);
      font-size: 16px;
      line-height: 1.62;
    }
    .kb-assistant-message {
      align-self: flex-start;
      width: min(720px, 100%);
    }
    .kb-floating-card,
    .kb-answer-card {
      width: auto;
      max-height: none;
      overflow: visible;
      margin: 0;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
      backdrop-filter: none;
    }
    .kb-section-label {
      margin-bottom: 12px;
    }
    .kb-sources {
      position: fixed;
      z-index: 4;
      top: 32px;
      right: 32px;
      width: min(360px, calc(100vw - 64px));
      max-height: calc(100vh - 64px);
      margin: 0;
      overflow: hidden;
    }
    .kb-scene:not(.has-output) .kb-sources {
      display: none;
    }
    .kb-sources summary {
      padding: 22px 24px 10px;
    }
    .results-scroll {
      max-height: calc(100vh - 144px);
      overflow-y: auto;
    }
    .kb-input-dock {
      position: fixed;
      z-index: 6;
      left: 50%;
      bottom: 24px;
      width: min(820px, calc(100% - 32px));
      min-height: 128px;
      transform: translateX(-50%);
      transition: bottom .22s ease, transform .22s ease, width .22s ease;
    }
    .kb-scene:not(.has-output) .kb-input-dock {
      top: 50%;
      bottom: auto;
      transform: translate(-50%, -50%);
    }
    textarea#question {
      min-height: 70px;
    }
    #askBtn {
      width: 34px;
      height: 34px;
      min-height: 34px;
      background: rgba(98, 92, 59, .78);
    }
    .kb-icon-button {
      display: inline-grid;
      place-items: center;
      width: 34px;
      height: 34px;
      min-width: 34px;
      min-height: 34px;
      aspect-ratio: 1;
      padding: 0;
      line-height: 0;
      flex: 0 0 auto;
      vertical-align: middle;
    }
    .kb-icon-button .kb-icon {
      display: block;
      width: 18px;
      height: 18px;
      overflow: visible;
      fill: none;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      vector-effect: non-scaling-stroke;
    }
    #askBtn::before,
    #askBtn::after {
      content: none !important;
      display: none !important;
    }
    #askBtn {
      border: 0;
      border-radius: 999px;
      background: rgba(98, 92, 59, .84);
      color: #f8f3eb;
      box-shadow: 0 6px 16px rgba(43, 40, 24, .16);
    }
    #askBtn:hover {
      background: #4d4830;
    }
    .kb-plus {
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: rgba(77, 72, 48, .70);
      box-shadow: none;
      font-size: 0;
    }
    .kb-plus:hover {
      background: rgba(248, 243, 235, .34);
      color: #4d4830;
      transform: translateY(-1px);
    }
    .kb-plus .kb-icon {
      width: 19px;
      height: 19px;
      stroke-width: 1.9;
    }
    .kb-icon-button:focus,
    .kb-icon-button:focus-visible {
      outline: none;
      box-shadow: 0 0 0 3px rgba(248, 243, 235, .50), 0 0 0 4px rgba(98, 92, 59, .22);
    }
    @media (max-width: 980px) {
      .kb-dialog-scroll {
        width: 100%;
        padding-top: 70px;
      }
      .kb-sources {
        position: static;
        width: 100%;
        max-height: none;
        margin: 14px 0 24px;
      }
      .results-scroll {
        max-height: 260px;
      }
      .kb-topbar {
        position: fixed;
        left: 16px;
        top: 16px;
      }
    }

    @keyframes fade-rise {
      from { opacity: 0; transform: translate3d(0, 18px, 0); }
      to { opacity: 1; transform: translate3d(0, 0, 0); }
    }
    @keyframes enter-breathe {
      0% { transform: translate3d(-50%, -48%, 0) scale(.985); opacity: 0; }
      100% { transform: translate3d(-50%, -50%, 0) scale(1); opacity: 1; }
    }
    @keyframes panel-glint {
      0% { transform: translateX(-120%) skewX(-12deg); opacity: 0; }
      42% { opacity: .22; }
      100% { transform: translateX(160%) skewX(-12deg); opacity: 0; }
    }

    .kb-topbar,
    .kb-input-dock,
    .kb-message,
    .kb-sources,
    .result,
    .status,
    .quick-prompts button {
      border: 1px solid rgba(110, 101, 68, .18);
      background:
        linear-gradient(180deg, rgba(255, 252, 242, .18), rgba(235, 226, 205, .07)),
        rgba(248, 243, 235, .07);
      box-shadow:
        inset 0 1px 1px rgba(255, 252, 242, .24),
        0 18px 54px rgba(43, 40, 24, .065);
      backdrop-filter: blur(22px) saturate(.98);
      -webkit-backdrop-filter: blur(22px) saturate(.98);
    }
    .kb-topbar,
    .kb-input-dock,
    .kb-sources,
    .kb-message {
      position: relative;
      overflow: hidden;
    }
    .kb-topbar::before,
    .kb-input-dock::before,
    .kb-sources::before,
    .kb-message::before {
      content: "";
      position: absolute;
      inset: 0;
      border-radius: inherit;
      padding: 1px;
      background: linear-gradient(180deg, rgba(255, 252, 242, .58), rgba(255, 252, 242, 0) 42%, rgba(89, 82, 54, .16));
      -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
      -webkit-mask-composite: xor;
      mask-composite: exclude;
      pointer-events: none;
    }
    .kb-topbar {
      position: fixed !important;
      top: 22px;
      left: 22px;
      width: min(520px, calc(100% - 44px));
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 12px 14px;
      border-radius: 18px;
      z-index: 7;
      animation: fade-rise .7s cubic-bezier(.16, 1, .3, 1) both;
    }
    .kb-brand {
      min-width: 0;
    }
    .kb-brand-mark {
      width: 36px;
      height: 36px;
      border-radius: 10px;
      background: rgba(248, 243, 235, .16);
      border: 1px solid rgba(110, 101, 68, .18);
      color: rgba(64, 58, 38, .82);
    }
    .kb-brand h1 {
      color: rgba(35, 30, 20, .88);
      font-size: 17px;
      letter-spacing: 0;
    }
    .kb-brand p {
      color: rgba(65, 59, 42, .58);
      font-size: 12px;
    }
    .kb-status-row {
      display: flex;
      align-items: center;
      gap: 6px;
      flex: 0 0 auto;
    }
    .kb-status-pill {
      min-height: 26px;
      padding: 5px 9px;
      border: 1px solid rgba(110, 101, 68, .16);
      border-radius: 999px;
      background: rgba(248, 243, 235, .11);
      color: rgba(64, 58, 38, .68);
      font-size: 12px;
      font-weight: 650;
    }
    .kb-input-dock,
    .kb-sources,
    .kb-message {
      animation: fade-rise .62s cubic-bezier(.16, 1, .3, 1) both;
    }
    .kb-stage {
      transform: none !important;
    }
    .kb-input-dock {
      position: fixed !important;
      border-radius: 24px;
      background:
        linear-gradient(180deg, rgba(255, 252, 242, .18), rgba(233, 224, 202, .08)),
        rgba(248, 243, 235, .08);
      box-shadow:
        inset 0 1px 1px rgba(255, 252, 242, .26),
        0 24px 74px rgba(43, 40, 24, .09);
    }
    .kb-message {
      border-radius: 22px;
      background:
        linear-gradient(180deg, rgba(255, 252, 242, .16), rgba(233, 224, 202, .06)),
        rgba(248, 243, 235, .075);
    }
    .kb-user-message {
      background:
        linear-gradient(180deg, rgba(255, 252, 242, .22), rgba(233, 224, 202, .08)),
        rgba(248, 243, 235, .10);
    }
    .kb-assistant-message {
      background:
        linear-gradient(180deg, rgba(255, 252, 242, .15), rgba(233, 224, 202, .07)),
        rgba(248, 243, 235, .08);
    }
    .kb-sources {
      position: fixed !important;
      border-radius: 24px;
      background:
        linear-gradient(180deg, rgba(255, 252, 242, .18), rgba(233, 224, 202, .08)),
        rgba(248, 243, 235, .09);
    }
    .result {
      border-radius: 14px;
      margin: 0 12px 10px;
      padding: 13px 14px;
      box-shadow: inset 0 1px 1px rgba(255, 252, 242, .36);
    }
    .kb-scene.awaiting-entry .kb-topbar,
    .kb-scene.awaiting-entry .kb-stage {
      opacity: 0 !important;
      pointer-events: none !important;
      transform: translate3d(0, 16px, 0);
    }
    .kb-scene.awaiting-entry .kb-input-dock,
    .kb-scene.awaiting-entry .kb-dialog-scroll,
    .kb-scene.awaiting-entry .kb-sources {
      opacity: 0 !important;
      pointer-events: none !important;
      visibility: hidden !important;
    }
    .kb-scene.awaiting-entry .kb-bg-video {
      filter: saturate(1.03) contrast(.98) brightness(1.04) blur(.7px);
      transform: scale(1.04);
      transition: filter 1.15s cubic-bezier(.16, 1, .3, 1), transform 1.15s cubic-bezier(.16, 1, .3, 1);
    }
    .kb-scene.entered .kb-bg-video {
      filter: saturate(.92) contrast(.94) brightness(.82);
      transform: scale(1.16);
      transition: filter 1.15s cubic-bezier(.16, 1, .3, 1), transform 1.15s cubic-bezier(.16, 1, .3, 1);
    }
    .kb-scene.awaiting-entry .kb-painting::after {
      background:
        radial-gradient(circle at 50% 48%, rgba(248, 243, 235, .06), transparent 30%),
        radial-gradient(circle at 50% 50%, transparent 34%, rgba(43, 40, 24, .22) 78%),
        linear-gradient(180deg, rgba(248, 243, 235, .10), rgba(248, 243, 235, .02));
      transition: opacity .9s cubic-bezier(.16, 1, .3, 1);
    }
    .kb-enter-screen {
      position: fixed;
      z-index: 20;
      left: 50%;
      top: 50%;
      width: min(520px, calc(100% - 48px));
      min-height: 188px;
      display: grid;
      place-items: center;
      gap: 8px;
      padding: 22px 24px;
      border: 0;
      border-radius: 0;
      color: rgba(34, 30, 20, .92);
      background: transparent;
      box-shadow: none;
      backdrop-filter: none;
      -webkit-backdrop-filter: none;
      cursor: pointer;
      transform: translate(-50%, -50%);
      animation: enter-breathe .9s cubic-bezier(.16, 1, .3, 1) both;
    }
    .kb-enter-screen:hover,
    .kb-enter-screen:focus,
    .kb-enter-screen:focus-visible,
    .kb-enter-screen:active {
      background: transparent !important;
      border: 0 !important;
      box-shadow: none !important;
      outline: none !important;
      transform: translate(-50%, -50%) !important;
    }
    .kb-enter-screen::after {
      content: none;
    }
    .kb-enter-kicker,
    .kb-enter-subtitle {
      font-size: 12px;
      color: rgba(64, 58, 38, .66);
      font-weight: 650;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .kb-enter-kicker:empty,
    .kb-enter-subtitle:empty {
      display: none;
    }
    .kb-enter-title {
      font-family: Georgia, "Noto Serif SC", "SimSun", serif;
      font-size: clamp(34px, 5.6vw, 64px);
      line-height: 1.05;
      color: rgba(248, 243, 235, .92);
      text-shadow: 0 3px 22px rgba(34, 30, 20, .48), 0 1px 1px rgba(34, 30, 20, .38);
      letter-spacing: 0;
    }
    .kb-scene.entered .kb-enter-screen {
      animation: none !important;
      opacity: 0 !important;
      visibility: hidden !important;
      transform: translate(-50%, -50%) scale(.985);
      pointer-events: none !important;
      transition: opacity .42s ease, transform .42s ease, visibility 0s linear .42s;
    }
    .kb-scene.entered:not(.has-output) .kb-input-dock {
      left: 50% !important;
      right: auto !important;
      top: 50% !important;
      bottom: auto !important;
      transform: translate(-50%, -50%) !important;
      opacity: 1 !important;
      visibility: visible !important;
      pointer-events: auto !important;
    }
    .kb-scene.has-output .kb-input-dock {
      width: min(760px, calc(100% - 440px)) !important;
      left: calc((100vw - 400px) / 2) !important;
      right: auto !important;
      top: auto !important;
      bottom: 24px !important;
      transform: translateX(-50%) !important;
    }
    @media (prefers-reduced-motion: reduce) {
      .kb-stage,
      .kb-input-dock,
      .kb-sources,
      .kb-message,
      .kb-topbar,
      .kb-enter-screen {
        animation: none !important;
        transition: none !important;
      }
      .kb-enter-screen::after { display: none; }
      .kb-scene.awaiting-entry .kb-bg-video,
      .kb-scene.entered .kb-bg-video {
        filter: saturate(.92) contrast(.94) brightness(.82);
        transform: scale(1.055);
      }
    }
  </style>
</head>
<body>
  <div class="kb-scene awaiting-entry">
    <div class="kb-painting" aria-hidden="true">
      <video class="kb-bg-video" autoplay loop muted playsinline poster="/assets/eu4-bg.png">
        <source src="/assets/eu4-bg-loop-pingpong.mp4?v=4030-1" type="video/mp4">
      </video>
    </div>
    <button id="enterScreen" class="kb-enter-screen" type="button" aria-label="进入 EU4 Wiki 知识库">
      <span class="kb-enter-kicker"></span>
      <span class="kb-enter-title">出发，开始你的征途</span>
      <span class="kb-enter-subtitle"></span>
    </button>
    <header class="kb-topbar">
      <div class="kb-brand">
        <div class="kb-brand-mark" aria-hidden="true">IV</div>
        <div>
          <h1>EU4 Wiki 知识库</h1>
          <p>Local archive · DeepSeek</p>
        </div>
      </div>
      <div class="kb-status-row" aria-label="运行状态">
        <span class="kb-status-pill"><span class="dot"></span>SQLite FTS</span>
        <span class="kb-status-pill">Effects</span>
        <span class="kb-status-pill">Achievements</span>
      </div>
    </header>
    <main class="kb-stage">
      <div class="kb-dialog-scroll">
        <section id="conversation" class="kb-conversation" aria-label="对话历史"></section>
        <details class="kb-sources" open>
          <summary>Retrieved Sources</summary>
          <div id="results" class="results-scroll"></div>
        </details>
      </div>
      <section class="kb-input-dock">
        <label class="kb-input-label" for="question">问题</label>
        <textarea id="question" placeholder="问一个 EU4 问题"></textarea>
        <div class="kb-input-tools">
          <div class="kb-tag-row">
            <button id="tagToggle" class="kb-plus kb-icon-button" type="button" aria-label="添加标签" aria-expanded="false">
              <svg class="kb-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 5v14M5 12h14" />
              </svg>
            </button>
            <div class="quick-prompts" aria-label="示例问题">
              <button type="button" data-prompt="如何让领袖有比较高的概率获得谨慎特质？">特质</button>
              <button type="button" data-prompt="给我一个可以获得减少 AE 效果的控制台事件指令。">事件</button>
              <button type="button" data-prompt="萨卢佐有什么减少 AE 的任务能做？">任务</button>
              <button type="button" data-prompt="帮我查一个成就的完成条件。">成就</button>
            </div>
          </div>
          <div class="kb-input-actions">
            <button id="searchBtn" class="secondary">只搜索</button>
            <button id="clearBtn" class="secondary">清空上下文</button>
            <button id="askBtn" class="kb-icon-button" aria-label="提问">
              <svg class="kb-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" />
              </svg>
            </button>
          </div>
          <div id="stats" class="status">正在读取索引状态...</div>
        </div>
      </section>
    </main>
  </div>
  <script>
    const q = document.getElementById('question');
    const conversation = document.getElementById('conversation');
    const results = document.getElementById('results');
    const askBtn = document.getElementById('askBtn');
    const searchBtn = document.getElementById('searchBtn');
    const clearBtn = document.getElementById('clearBtn');
    const bgVideo = document.querySelector('.kb-bg-video');
    const scene = document.querySelector('.kb-scene');
    const enterScreen = document.getElementById('enterScreen');
    const tagToggle = document.getElementById('tagToggle');
    const tagRow = document.querySelector('.kb-tag-row');
    const promptBtns = document.querySelectorAll('[data-prompt]');
    const MAX_HISTORY_TURNS = 8;
    let chatHistory = [];
    let activeAssistant = null;
    let activeCitations = null;
    if (bgVideo) {
      bgVideo.defaultPlaybackRate = 1;
      bgVideo.playbackRate = 1;
    }

    function enterExperience() {
      if (!scene.classList.contains('awaiting-entry')) return;
      scene.classList.remove('awaiting-entry');
      scene.classList.add('entered');
      window.setTimeout(() => q.focus(), 260);
    }

    function escapeHtml(s) {
      return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function renderMarkdown(md) {
      const lines = String(md ?? '').replace(/\r\n?/g, '\n').split('\n');
      let html = '';
      let listType = null;
      let inCode = false;
      let codeLang = '';
      let codeBuf = [];
      let inMath = false;
      let mathBuf = [];

      const closeList = () => {
        if (listType) {
          html += `</${listType}>`;
          listType = null;
        }
      };
      const openList = (type) => {
        if (listType !== type) {
          closeList();
          html += `<${type}>`;
          listType = type;
        }
      };
      const flushCode = () => {
        closeList();
        const label = codeLang ? ` data-lang="${escapeHtml(codeLang)}"` : '';
        html += `<pre${label}><code>${escapeHtml(codeBuf.join('\n'))}</code></pre>`;
        codeBuf = [];
        codeLang = '';
      };
      const flushMath = () => {
        closeList();
        html += `<div class="math-block">${escapeHtml(mathBuf.join('\n'))}</div>`;
        mathBuf = [];
      };
      const inline = (s) => {
        const placeholders = [];
        let escaped = escapeHtml(s);
        escaped = escaped.replace(/`([^`]+)`/g, (_, code) => {
          const token = `\u0000${placeholders.length}\u0000`;
          placeholders.push(`<code>${code}</code>`);
          return token;
        });
        escaped = escaped.replace(/\$([^$\n]+)\$/g, (_, expr) => {
          const token = `\u0000${placeholders.length}\u0000`;
          placeholders.push(`<span class="math-inline">${expr}</span>`);
          return token;
        });
        escaped = escaped
          .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
          .replace(/__([^_]+)__/g, '<strong>$1</strong>')
          .replace(/\*([^*\s][^*]*?)\*/g, '<em>$1</em>')
          .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
        placeholders.forEach((value, index) => {
          escaped = escaped.replaceAll(`\u0000${index}\u0000`, value);
        });
        return escaped;
      };
      const splitTableRow = (line) => {
        let trimmed = line.trim();
        if (trimmed.startsWith('|')) trimmed = trimmed.slice(1);
        if (trimmed.endsWith('|')) trimmed = trimmed.slice(0, -1);
        return trimmed.split('|').map(cell => cell.trim());
      };
      const isTableSep = (line) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
      const renderTable = (start) => {
        const header = splitTableRow(lines[start]);
        const sep = splitTableRow(lines[start + 1]);
        const aligns = sep.map(cell => {
          const left = cell.startsWith(':');
          const right = cell.endsWith(':');
          return left && right ? 'center' : right ? 'right' : left ? 'left' : '';
        });
        let i = start + 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          rows.push(splitTableRow(lines[i]));
          i++;
        }
        closeList();
        html += '<div class="table-wrap"><table><thead><tr>';
        header.forEach((cell, col) => {
          const style = aligns[col] ? ` style="text-align:${aligns[col]}"` : '';
          html += `<th${style}>${inline(cell)}</th>`;
        });
        html += '</tr></thead><tbody>';
        rows.forEach(row => {
          html += '<tr>';
          header.forEach((_, col) => {
            const style = aligns[col] ? ` style="text-align:${aligns[col]}"` : '';
            html += `<td${style}>${inline(row[col] ?? '')}</td>`;
          });
          html += '</tr>';
        });
        html += '</tbody></table></div>';
        return i;
      };

      for (let i = 0; i < lines.length; i++) {
        const raw = lines[i];
        const line = raw.trimEnd();

        const fence = line.match(/^\s*```([A-Za-z0-9_-]*)\s*$/);
        if (fence) {
          if (inCode) {
            inCode = false;
            flushCode();
          } else {
            closeList();
            inCode = true;
            codeLang = fence[1] || '';
            codeBuf = [];
          }
          continue;
        }
        if (inCode) {
          codeBuf.push(raw);
          continue;
        }

        if (line.trim() === '$$') {
          if (inMath) {
            inMath = false;
            flushMath();
          } else {
            closeList();
            inMath = true;
            mathBuf = [];
          }
          continue;
        }
        if (inMath) {
          mathBuf.push(raw);
          continue;
        }

        if (i + 1 < lines.length && line.includes('|') && isTableSep(lines[i + 1])) {
          i = renderTable(i) - 1;
          continue;
        }

        if (!line.trim()) {
          closeList();
          continue;
        }
        if (/^\s*---+\s*$/.test(line)) {
          closeList();
          html += '<hr>';
          continue;
        }
        const heading = line.match(/^(#{1,4})\s+(.+)$/);
        if (heading) {
          closeList();
          const level = Math.min(6, heading[1].length + 2);
          html += `<h${level}>${inline(heading[2])}</h${level}>`;
          continue;
        }
        const bullet = line.match(/^\s*[-*]\s+(.+)$/);
        if (bullet) {
          openList('ul');
          html += `<li>${inline(bullet[1])}</li>`;
          continue;
        }
        const numbered = line.match(/^\s*\d+\.\s+(.+)$/);
        if (numbered) {
          openList('ol');
          html += `<li>${inline(numbered[1])}</li>`;
          continue;
        }
        const quote = line.match(/^>\s*(.+)$/);
        if (quote) {
          closeList();
          html += `<blockquote>${inline(quote[1])}</blockquote>`;
          continue;
        }
        closeList();
        html += `<p>${inline(line)}</p>`;
      }
      if (inCode) flushCode();
      if (inMath) flushMath();
      closeList();
      return html;
    }
    function renderResults(items) {
      results.innerHTML = items.map(item => `
        <article class="result">
          <h3>${escapeHtml(item.title)} <span>/ ${escapeHtml(item.section)}</span></h3>
          <div class="meta"><a href="${escapeHtml(item.url)}" target="_blank">${escapeHtml(item.path)}</a> · score ${escapeHtml(item.score)}</div>
          <p>${escapeHtml(item.snippet)}</p>
        </article>`).join('') || '<p class="empty">没有召回片段。</p>';
    }
    function renderCitations(items) {
      if (!activeCitations) return;
      activeCitations.innerHTML = items.map(item => `<a class="citation" href="${escapeHtml(item.url)}" target="_blank">${escapeHtml(item.title)} / ${escapeHtml(item.section)}</a>`).join('');
    }
    function appendUserMessage(text) {
      const node = document.createElement('article');
      node.className = 'kb-message kb-user-message';
      node.textContent = text;
      conversation.appendChild(node);
      return node;
    }
    function appendAssistantMessage(text) {
      const node = document.createElement('article');
      node.className = 'kb-message kb-assistant-message';
      node.innerHTML = `
        <h2 class="kb-section-label">Answer</h2>
        <div class="answer">${escapeHtml(text)}</div>
        <div class="citations"></div>`;
      conversation.appendChild(node);
      activeAssistant = node.querySelector('.answer');
      activeCitations = node.querySelector('.citations');
      return node;
    }
    function setActiveAssistantMarkdown(markdown) {
      if (!activeAssistant) appendAssistantMessage('');
      activeAssistant.innerHTML = renderMarkdown(markdown);
    }
    function beginTurn(text, loadingText) {
      enterExperience();
      scene.classList.add('has-output');
      const userNode = appendUserMessage(text);
      appendAssistantMessage(loadingText);
      q.value = '';
      tagRow.classList.remove('open');
      tagToggle.setAttribute('aria-expanded', 'false');
      requestAnimationFrame(() => userNode.scrollIntoView({ block: 'start', behavior: 'smooth' }));
    }
    async function loadStats() {
      const res = await fetch('/api/stats');
      const data = await res.json();
      const effectText = data.effect_sources && data.effect_sources.ready
        ? `效果来源 ${data.effect_sources.indexed_sources} 条。`
        : '效果来源索引未就绪。';
      const missionText = data.mission_sources && data.mission_sources.ready
        ? `任务 ${data.mission_sources.indexed_missions} 条。`
        : '任务索引未就绪。';
      const achievementText = data.achievements && data.achievements.ready
        ? `成就 ${data.achievements.indexed_achievements} 条。`
        : '成就索引未就绪。';
      document.getElementById('stats').textContent = data.ready
        ? `索引页 ${data.indexed_pages}/${data.candidate_pages}，覆盖率 ${(data.coverage * 100).toFixed(1)}%，片段 ${data.indexed_chunks}。${effectText}${missionText}${achievementText}上下文 ${chatHistory.length}/${MAX_HISTORY_TURNS} 轮。`
        : `索引未就绪：${data.error || '请先运行构建命令'}`;
    }
    async function doSearch() {
      const query = q.value.trim();
      if (!query) return;
      beginTurn(query, '检索中...');
      const res = await fetch('/api/search?q=' + encodeURIComponent(query) + '&limit=10');
      const data = await res.json();
      setActiveAssistantMarkdown(data.error || `找到 ${data.results.length} 条相关片段。`);
      renderResults(data.results || []);
    }
    async function doAsk() {
      const question = q.value.trim();
      if (!question) return;
      beginTurn(question, '检索并生成回答中...');
      askBtn.disabled = true;
      try {
        const res = await fetch('/api/ask', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({question, limit: 8, history: chatHistory}) });
        const data = await res.json();
        const planInfo = data.planned_searches ? `规划检索 ${data.planned_searches} 次` : '';
        const toolInfo = data.tool_calls ? `追加检索 ${data.tool_calls} 次` : '';
        const searchInfo = [planInfo, toolInfo].filter(Boolean).join('，');
        const searchInfoText = searchInfo ? `\n\n（本轮${searchInfo}）` : '';
        setActiveAssistantMarkdown((data.answer || data.error || '没有生成回答。') + searchInfoText);
        renderCitations(data.citations || []);
        renderResults(data.retrieved || []);
        if (!data.error && data.answer) {
          chatHistory.push({user: question, assistant: data.answer});
          chatHistory = chatHistory.slice(-MAX_HISTORY_TURNS);
          loadStats();
        }
      } finally {
        askBtn.disabled = false;
      }
    }
    function clearHistory() {
      chatHistory = [];
      scene.classList.remove('has-output');
      conversation.innerHTML = '';
      activeAssistant = null;
      activeCitations = null;
      results.innerHTML = '';
      q.value = '';
      loadStats();
    }
    searchBtn.addEventListener('click', doSearch);
    askBtn.addEventListener('click', doAsk);
    clearBtn.addEventListener('click', clearHistory);
    tagToggle.addEventListener('click', () => {
      const open = !tagRow.classList.contains('open');
      tagRow.classList.toggle('open', open);
      tagToggle.setAttribute('aria-expanded', String(open));
    });
    promptBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        q.value = btn.dataset.prompt || '';
        tagRow.classList.remove('open');
        tagToggle.setAttribute('aria-expanded', 'false');
        q.focus();
      });
    });
    enterScreen.addEventListener('click', enterExperience);
    window.addEventListener('keydown', e => {
      if (!scene.classList.contains('awaiting-entry')) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        enterExperience();
      }
    });
    q.addEventListener('keydown', e => { if (e.ctrlKey && e.key === 'Enter') doAsk(); });
    loadStats();
  </script>
</body>
</html>
"""


def db_path() -> Path:
    return Path(os.environ.get("WIKI_INDEX_DB", str(DEFAULT_DB))).resolve()


def effect_db_path() -> Path:
    return Path(os.environ.get("EFFECT_INDEX_DB", str(DEFAULT_EFFECT_DB))).resolve()


def achievement_db_path() -> Path:
    return Path(os.environ.get("ACHIEVEMENT_INDEX_DB", str(DEFAULT_ACHIEVEMENT_DB))).resolve()


def mission_db_path() -> Path:
    return Path(os.environ.get("MISSION_INDEX_DB", str(DEFAULT_MISSION_DB))).resolve()


def entity_db_path() -> Path:
    return Path(os.environ.get("ENTITY_INDEX_DB", str(DEFAULT_ENTITY_DB))).resolve()


def text_ngrams(text: str, n: int = 2) -> set[str]:
    text = re.sub(r"\s+", "", text.lower())
    if len(text) <= n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def dice_similarity(left: str, right: str) -> float:
    a = text_ngrams(left)
    b = text_ngrams(right)
    if not a or not b:
        return 0.0
    return (2.0 * len(a & b)) / (len(a) + len(b))


def bounded_levenshtein(left: str, right: str, max_distance: int = 3) -> int:
    left = left.lower()
    right = right.lower()
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    previous = list(range(len(right) + 1))
    for i, ca in enumerate(left, 1):
        current = [i]
        row_min = current[0]
        for j, cb in enumerate(right, 1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def edit_similarity(left: str, right: str) -> float:
    max_len = max(len(left), len(right))
    if max_len == 0:
        return 0.0
    distance = bounded_levenshtein(left, right, max(2, min(4, max_len // 2)))
    if distance > max(2, min(4, max_len // 2)):
        return 0.0
    return max(0.0, 1.0 - distance / max_len)


def fuzzy_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left.lower() == right.lower():
        return 1.0
    return max(dice_similarity(left, right), edit_similarity(left, right))


def fuzzy_query_spans(text: str) -> list[str]:
    spans: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]{2,12}", text):
        spans.append(run)
        for suffix in ("\u4efb\u52a1", "\u6210\u5c31", "\u7279\u8d28", "\u7279\u6027", "\u4e8b\u4ef6", "\u51b3\u8bae"):
            if run.endswith(suffix) and len(run) > len(suffix) + 1:
                spans.append(run[: -len(suffix)])
        if len(run) >= 4:
            for size in range(3, min(8, len(run)) + 1):
                for i in range(0, len(run) - size + 1):
                    spans.append(run[i : i + size])
    for token in re.split(r"[\s,/;:(){}\[\]<>!?，。；：！？（）【】]+", text):
        token = token.strip()
        if len(token) >= 4 and re.search(r"[A-Za-z]", token):
            spans.append(token)
    return list(dict.fromkeys(spans))


def fuzzy_entity_rows(
    con: sqlite3.Connection,
    text: str,
    entity_types: tuple[str, ...],
    limit: int,
) -> list[sqlite3.Row]:
    spans = fuzzy_query_spans(text)
    if not spans:
        return []
    where = ["length(alias) between 3 and 24"]
    params: list[object] = []
    if entity_types:
        where.append("entity_type in (" + ",".join("?" for _ in entity_types) + ")")
        params.extend(entity_types)
    candidates = con.execute(
        f"""
        select *
        from entity_aliases
        where {' and '.join(where)}
        """,
        params,
    ).fetchall()
    scored: list[tuple[float, sqlite3.Row, str]] = []
    for row in candidates:
        alias = str(row["alias"])
        best = max(fuzzy_similarity(span, alias) for span in spans)
        threshold = 0.66 if len(alias) <= 3 else (0.74 if re.search(r"[\u4e00-\u9fff]", alias) and len(alias) <= 4 else 0.78)
        if best >= threshold:
            # Prefer longer, named entities over generic page aliases.
            score = best * 1000 + float(row["confidence"]) * 100 + min(len(alias), 12)
            scored.append((score, row, alias))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row, _ in scored[: max(limit, 1)]]


def llm_config_path() -> Path:
    return Path(os.environ.get("LLM_CONFIG", str(DEFAULT_LLM_CONFIG))).resolve()


def answering_skill_path() -> Path:
    return Path(os.environ.get("ANSWERING_SKILL", str(DEFAULT_ANSWERING_SKILL))).resolve()


def read_answering_skill() -> str:
    path = answering_skill_path()
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        except OSError:
            pass
    return "你是 EU4 wiki 知识库助手。只能基于给定片段回答；若片段不足，明确说不确定。回答末尾列出引用编号。"


def read_llm_config() -> dict[str, str]:
    config: dict[str, str] = {}
    path = llm_config_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config.update({str(k): str(v) for k, v in raw.items() if v is not None})
        except (OSError, json.JSONDecodeError):
            config["_config_error"] = f"无法读取 LLM 配置文件：{path}"

    env_map = {
        "api_key": "LLM_API_KEY",
        "base_url": "LLM_BASE_URL",
        "model": "LLM_MODEL",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[key] = value
    return config


def llm_config_status() -> dict:
    config = read_llm_config()
    api_key = config.get("api_key", "")
    return {
        "config_path": str(llm_config_path()),
        "configured": bool(api_key),
        "base_url": config.get("base_url", "https://api.openai.com/v1"),
        "model": config.get("model", "gpt-4.1-mini"),
        "config_error": config.get("_config_error"),
    }


def wiki_dir_from_db(con: sqlite3.Connection) -> Path:
    try:
        raw = get_stat(con, "wiki_dir")
        if raw:
            return Path(raw)
    except Exception:
        pass
    return DEFAULT_WIKI_DIR


def get_stat(con: sqlite3.Connection, key: str):
    row = con.execute("select value from build_stats where key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def resolve_entities(text: str, entity_types: tuple[str, ...] = (), limit: int = 12) -> list[dict]:
    text = text.strip()
    path = entity_db_path()
    if not text or not path.exists():
        return []
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows: list[sqlite3.Row] = []
        params: list[object] = []
        type_clause = ""
        if entity_types:
            type_clause = " and entity_type in (" + ",".join("?" for _ in entity_types) + ")"
            params.extend(entity_types)
        # Exact substring match is more reliable for short Chinese aliases than FTS.
        exact_rows = con.execute(
            f"""
            select *, 1000.0 + confidence * 100 + length(alias) as rank_score
            from entity_aliases
            where ? like '%' || alias || '%'{type_clause}
            order by rank_score desc
            limit ?
            """,
            [text] + params + [limit * 3],
        ).fetchall()
        rows.extend(exact_rows)
        if len(rows) < limit:
            terms = [t for t in split_query_terms(text) if len(t) >= 2]
            fts = " OR ".join(quote_fts_term(t) for t in list(dict.fromkeys(terms))[:8])
            if fts:
                try:
                    fts_rows = con.execute(
                        f"""
                        select e.*, 500.0 - bm25(entity_aliases_fts) + e.confidence * 100 as rank_score
                        from entity_aliases_fts
                        join entity_aliases e on e.id = entity_aliases_fts.rowid
                        where entity_aliases_fts match ?{type_clause.replace('entity_type', 'e.entity_type')}
                        order by rank_score desc
                        limit ?
                        """,
                        [fts] + params + [limit * 3],
                    ).fetchall()
                    rows.extend(fts_rows)
                except sqlite3.Error:
                    pass
        fuzzy_rows = fuzzy_entity_rows(con, text, entity_types, limit * 2)
        if exact_rows:
            rows.extend(fuzzy_rows)
        else:
            rows = fuzzy_rows + rows
    finally:
        con.close()

    seen = set()
    results: list[dict] = []
    for row in rows:
        key = (row["entity_type"], row["canonical"], row["page_path"], row["source_id"], row["target_kind"])
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "entity_type": row["entity_type"],
                "canonical": row["canonical"],
                "display_name": row["display_name"],
                "alias": row["alias"],
                "page_path": row["page_path"],
                "source_id": row["source_id"],
                "target_kind": row["target_kind"],
                "confidence": float(row["confidence"]),
                "metadata": parse_json_object(row["metadata"] or "{}"),
            }
        )
        if len(results) >= limit:
            break
    return results


def entity_page_hints(text: str, entity_types: tuple[str, ...], limit: int = 8) -> list[str]:
    paths: list[str] = []
    for entity in resolve_entities(text, entity_types, limit * 2):
        path = str(entity.get("page_path", ""))
        if path.endswith(".html") and path not in paths:
            paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def entity_source_terms(text: str, entity_types: tuple[str, ...], limit: int = 6) -> list[str]:
    terms: list[str] = []
    for entity in resolve_entities(text, entity_types, limit):
        for value in (entity.get("display_name", ""), entity.get("source_id", ""), entity.get("canonical", "")):
            value = str(value).strip()
            if value and value not in terms:
                terms.append(value)
    return terms[:limit]


def expand_query(query: str) -> str:
    terms = [query]
    for key, value in QUERY_EXPANSIONS.items():
        if key.lower() in query.lower():
            terms.append(value)
    tokens = []
    for term in terms:
        tokens.extend(split_query_terms(term))
    unique = []
    seen = set()
    for token in tokens:
        low = token.lower()
        if low not in seen:
            seen.add(low)
            unique.append(token)
    return " OR ".join(quote_fts_term(t) for t in unique if t.strip())


def split_query_terms(query: str) -> list[str]:
    parts = [query.strip()]
    parts.extend(p for p in query.replace("-", " ").replace("/", " ").split() if len(p) >= 2)
    cjk_runs = []
    current = []
    for ch in query:
        if "\u4e00" <= ch <= "\u9fff":
            current.append(ch)
        elif current:
            cjk_runs.append("".join(current))
            current = []
    if current:
        cjk_runs.append("".join(current))
    for run in cjk_runs:
        parts.append(run)
        if len(run) >= 4:
            parts.extend(run[i : i + 3] for i in range(0, len(run) - 2))
    return [p for p in parts if p]


def quote_fts_term(term: str) -> str:
    cleaned = term.replace('"', " ").strip()
    return '"' + cleaned + '"'


def search_index(query: str, limit: int = 10) -> tuple[list[dict], str | None]:
    query = query.strip()
    if not query:
        return [], "query is required"
    path = db_path()
    if not path.exists():
        return [], f"index database not found: {path}"
    limit = max(1, min(int(limit or 10), 30))
    fetch_limit = min(max(limit * 10, 50), 240)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    wiki_dir = wiki_dir_from_db(con)
    fts_query = expand_query(query)
    sql = """
        select title, section, body, path, bm25(chunks) as score
        from chunks
        where chunks match ?
        order by bm25(chunks)
        limit ?
    """
    try:
        try:
            rows = con.execute(sql, (fts_query, fetch_limit)).fetchall()
        except sqlite3.Error:
            rows = []
        rows = merge_rows(rows, like_search(con, query, fetch_limit))
    finally:
        con.close()
    results = []
    ranked_rows = rerank_rows(filter_noise_rows(rows), query)[:limit]
    for row in ranked_rows:
        item_path = row["path"]
        results.append(
            {
                "title": row["title"],
                "section": row["section"],
                "snippet": compact(row["body"], 520),
                "path": item_path,
                "score": round(float(row["score"]), 4),
                "url": (wiki_dir / item_path).resolve().as_uri(),
            }
        )
    return results, None


def search_index_in_paths(query: str, paths: list[str], limit: int = 5) -> tuple[list[dict], str | None]:
    query = query.strip()
    paths = [p for p in dict.fromkeys(paths) if p]
    if not query or not paths:
        return [], None
    path = db_path()
    if not path.exists():
        return [], f"index database not found: {path}"
    limit = max(1, min(int(limit or 5), 20))
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    wiki_dir = wiki_dir_from_db(con)
    try:
        fts_query = expand_query(query)
        placeholders = ",".join("?" for _ in paths)
        try:
            rows = con.execute(
                f"""
                select title, section, body, path, page_id, bm25(chunks) as score
                from chunks
                where chunks match ? and path in ({placeholders})
                order by bm25(chunks)
                limit ?
                """,
                [fts_query] + paths + [limit],
            ).fetchall()
        except sqlite3.Error:
            rows = []
        if not rows:
            like_terms = [t for t in split_query_terms(query) if len(t) >= 2][:10] or [query]
            clauses = []
            params: list[object] = []
            for term in like_terms:
                clauses.append("(title like ? or section like ? or body like ?)")
                params.extend([f"%{term}%"] * 3)
            rows = con.execute(
                f"""
                select title, section, body, path, page_id, -1.0 as score
                from chunks
                where path in ({placeholders}) and ({' or '.join(clauses)})
                limit ?
                """,
                paths + params + [limit],
            ).fetchall()
    finally:
        con.close()
    results = []
    for row in filter_noise_rows(rows):
        item_path = row["path"]
        results.append(
            {
                "title": row["title"],
                "section": row["section"],
                "snippet": compact(row["body"], 800),
                "path": item_path,
                "score": round(float(row["score"]), 4),
                "url": (wiki_dir / item_path).resolve().as_uri(),
                "source_type": "page",
            }
        )
    return results, None


def search_effect_sources(
    effect_query: str,
    source_type: str = "",
    scope: str = "",
    limit: int = 10,
) -> tuple[list[dict], str | None]:
    effect_query = effect_query.strip()
    source_type = source_type.strip()
    scope = scope.strip()
    if not effect_query:
        return [], "effect_query is required"
    if source_type == "mission":
        mission_results, mission_error = search_mission_sources(effect_query, scope, limit)
        if mission_results:
            return mission_results, None
        if mission_error:
            return [], mission_error
    path = effect_db_path()
    if not path.exists():
        return [], f"effect source database not found: {path}"
    limit = max(1, min(int(limit or 10), 30))
    fetch_limit = min(max(limit * 12, 60), 240)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    wiki_dir = DEFAULT_WIKI_DIR
    country_terms: list[str] = []
    try:
        try:
            raw_wiki_dir = get_stat(con, "wiki_dir")
            if raw_wiki_dir:
                wiki_dir = Path(raw_wiki_dir)
        except Exception:
            wiki_dir = DEFAULT_WIKI_DIR
        fts_query = expand_effect_query(effect_query, scope)
        rows = effect_fts_search(con, fts_query, source_type, fetch_limit)
        rows = merge_effect_rows(rows, effect_like_search(con, effect_query, source_type, scope, fetch_limit))
    finally:
        con.close()
    results = []
    cleaned_rows = filter_effect_topic_rows(filter_noise_effect_rows(rows), effect_query)
    for row in rerank_effect_rows(cleaned_rows, effect_query, source_type, scope)[:limit]:
        path_value = row["page_path"]
        title = row["source_title"] or Path(path_value).stem.replace("_", " ")
        section_bits = [row["source_type"]]
        if row["source_id"]:
            section_bits.append(row["source_id"])
        if row["option_or_section"]:
            section_bits.append(row["option_or_section"])
        effects = row["effects"] or row["raw_text"]
        details = []
        if row["conditions"]:
            details.append("条件：" + row["conditions"])
        if effects:
            details.append("效果：" + effects)
        if row["duration"]:
            details.append("持续：" + row["duration"])
        if row["scope"]:
            details.append("范围：" + row["scope"])
        if row["source_type"] == "event" and row["raw_text"]:
            raw_text = str(row["raw_text"] or "")
            if "触发条件" in raw_text or "trigger" in raw_text.lower():
                details.insert(0, raw_text)
        results.append(
            {
                "title": title,
                "section": " / ".join(section_bits),
                "snippet": compact("\n".join(details) or row["raw_text"], 700),
                "path": path_value,
                "score": round(float(row["score"]), 4),
                "url": (wiki_dir / path_value).resolve().as_uri(),
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "option_or_section": row["option_or_section"],
                "conditions": row["conditions"],
                "effects": row["effects"],
                "duration": row["duration"],
                "scope": row["scope"],
            }
        )
    return results, None


def search_achievements(query: str, limit: int = 10) -> tuple[list[dict], str | None]:
    query = query.strip()
    if not query:
        return [], "query is required"
    path = achievement_db_path()
    if not path.exists():
        return [], f"achievement database not found: {path}"
    limit = max(1, min(int(limit or 10), 30))
    fetch_limit = min(max(limit * 10, 50), 160)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    wiki_dir = DEFAULT_WIKI_DIR
    try:
        try:
            raw_wiki_dir = get_stat(con, "wiki_dir")
            if raw_wiki_dir:
                wiki_dir = Path(raw_wiki_dir)
        except Exception:
            pass
        fts_query = expand_query(query)
        try:
            rows = con.execute(
                """
                select *, bm25(achievements_fts) as score
                from achievements_fts
                where achievements_fts match ?
                order by bm25(achievements_fts)
                limit ?
                """,
                (fts_query, fetch_limit),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        country_terms = achievement_country_terms(con, query)
        country_rows = achievement_country_start_search(con, query, fetch_limit)
        rows = merge_achievement_rows(country_rows, merge_achievement_rows(rows, achievement_like_search(con, query, fetch_limit)))
    finally:
        con.close()

    results = []
    ranked_rows = rerank_achievement_rows(rows, query)
    if country_terms and ("\u6210\u5c31" in query or "achievement" in query.lower()):
        country_rows_only = [
            row
            for row in ranked_rows
            if any(is_country_start_achievement(row, term) for term in country_terms)
        ]
        if country_rows_only:
            ranked_rows = country_rows_only
    for row in ranked_rows[:limit]:
        title = row["chinese_name"] or row["english_name"]
        section = "成就"
        if row["english_name"] and row["chinese_name"]:
            section = f"{row['english_name']} / {row['difficulty']} / {row['version']}"
        snippet = "\n".join(
            bit
            for bit in (
                f"描述：{row['description']}" if row["description"] else "",
                f"开始条件：{row['starting_conditions']}" if row["starting_conditions"] else "",
                f"完成需求：{row['completion_requirements']}" if row["completion_requirements"] else "",
                f"备注/路线：{row['notes']}" if row["notes"] else "",
                f"DLC：{row['dlc']}" if row["dlc"] else "",
            )
            if bit
        )
        item_path = row["page_path"]
        results.append(
            {
                "title": title,
                "section": section,
                "snippet": compact(snippet or row["raw_text"], 900),
                "path": item_path,
                "score": round(float(row["score"]), 4),
                "url": (wiki_dir / item_path).resolve().as_uri(),
                "source_type": "achievement",
                "english_name": row["english_name"],
                "chinese_name": row["chinese_name"],
                "description": row["description"],
                "starting_conditions": row["starting_conditions"],
                "completion_requirements": row["completion_requirements"],
                "notes": row["notes"],
                "dlc": row["dlc"],
                "version": row["version"],
                "difficulty": row["difficulty"],
            }
        )
    return results, None


def search_mission_sources(query: str, scope: str = "", limit: int = 10) -> tuple[list[dict], str | None]:
    query = query.strip()
    scope = scope.strip()
    if not query:
        return [], "query is required"
    path = mission_db_path()
    if not path.exists():
        return [], f"mission source database not found: {path}"
    limit = max(1, min(int(limit or 10), 30))
    fetch_limit = min(max(limit * 12, 80), 260)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    wiki_dir = DEFAULT_WIKI_DIR
    try:
        try:
            raw_wiki_dir = get_stat(con, "wiki_dir")
            if raw_wiki_dir:
                wiki_dir = Path(raw_wiki_dir)
        except Exception:
            pass
        page_hints = locate_mission_pages(con, query, scope, 12)
        direct_paths = [row["page_path"] for row in page_hints]
        alias_paths = mission_scope_alias_pages(query, scope)
        page_hint_paths = direct_paths + alias_paths
        if not page_hint_paths:
            page_hint_paths.extend(locate_mission_pages_from_wiki(query, scope))
        page_hint_paths = list(dict.fromkeys(page_hint_paths))[:16]
        fts_query = expand_mission_query(query, scope)
        rows = mission_fts_search(con, fts_query, fetch_limit)
        rows = merge_mission_rows(rows, mission_like_search(con, query, scope, page_hint_paths, fetch_limit))
        if page_hint_paths and (scope or direct_paths or alias_paths):
            allowed_paths = set(page_hint_paths)
            rows = [row for row in rows if row["page_path"] in allowed_paths]
    finally:
        con.close()

    page_hint_set = set(page_hint_paths)
    ranked_rows = rerank_mission_rows(rows, query, scope, page_hint_set)[:limit]
    if not ranked_rows and page_hint_paths:
        return search_index_in_paths(" ".join(t for t in (query, scope, "mission 任务") if t), page_hint_paths, limit)
    results = []
    for row in ranked_rows:
        path_value = row["page_path"]
        title = row["mission_title"] or row["page_title"]
        details = []
        if row["conditions"]:
            details.append("完成条件：" + row["conditions"])
        if row["effects"]:
            details.append("效果：" + row["effects"])
        if row["prerequisites"]:
            details.append("前置任务：" + row["prerequisites"])
        if row["version_note"]:
            details.append("版本提示：" + row["version_note"])
        snippet = compact("\n".join(details) or row["raw_text"], 950)
        section_bits = ["mission"]
        if row["country_or_tree"]:
            section_bits.append(row["country_or_tree"])
        if row["slot_or_section"]:
            section_bits.append(row["slot_or_section"])
        if row["mission_id"] and row["mission_id"] != title:
            section_bits.append(row["mission_id"])
        results.append(
            {
                "title": title,
                "section": " / ".join(section_bits),
                "snippet": snippet,
                "path": path_value,
                "score": round(float(row["score"]), 4),
                "url": (wiki_dir / path_value).resolve().as_uri(),
                "source_type": "mission",
                "mission_title": row["mission_title"],
                "mission_id": row["mission_id"],
                "country_or_tree": row["country_or_tree"],
                "slot_or_section": row["slot_or_section"],
                "description": row["description"],
                "conditions": row["conditions"],
                "effects": row["effects"],
                "prerequisites": row["prerequisites"],
                "version_note": row["version_note"],
            }
        )
    return results, None


def mission_entity_terms(query: str, scope: str = "") -> list[str]:
    terms: list[str] = []
    for text in (scope, query):
        text = text.strip()
        if not text:
            continue
        terms.append(text)
        for part in re.split(r"[\s,/，。；;:：()（）【】\\-]+", text):
            part = part.strip()
            if len(part) >= 2:
                terms.append(part)
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            terms.append(run)
            for suffix in ("任务", "任務", "成就", "mission", "missions"):
                if run.endswith(suffix) and len(run) > len(suffix):
                    terms.append(run[: -len(suffix)])
    cleaned = []
    stop = {"任务", "任務", "mission", "missions", "奖励", "效果", "条件", "怎么做", "有什么", "哪些"}
    for term in terms:
        term = term.strip()
        if len(term) < 2 or term in stop:
            continue
        if term not in cleaned:
            cleaned.append(term)
    return cleaned[:16]


def expand_mission_query(query: str, scope: str = "") -> str:
    terms = [query]
    if scope:
        terms.append(scope)
    for key, value in {
        "ae": "侵略扩张 aggressive expansion aggressive_expansion_impact",
        "AE": "侵略扩张 aggressive expansion aggressive_expansion_impact",
        "侵略扩张": "aggressive expansion aggressive_expansion_impact AE",
        "外交吞并": "diplomatic annexation annexation cost",
        "传教强度": "missionary strength global_missionary_strength",
        "行政效率": "administrative efficiency administrative_efficiency",
        "造核": "core creation core_creation_cost",
        "外交声誉": "diplomatic reputation diplomatic_reputation",
        "改善关系": "improve relation improve_relation_modifier",
    }.items():
        if key.lower() in query.lower():
            terms.append(value)
    tokens = []
    for term in terms:
        tokens.extend(split_query_terms(term))
    unique = []
    seen = set()
    for token in tokens:
        low = token.lower()
        if low not in seen:
            seen.add(low)
            unique.append(token)
    return " OR ".join(quote_fts_term(t) for t in unique if t.strip())


def locate_mission_pages(con: sqlite3.Connection, query: str, scope: str, limit: int) -> list[sqlite3.Row]:
    terms = mission_entity_terms(query, scope)
    if not terms:
        return []
    fields = "page_title country_or_tree page_path".split()
    clauses = []
    params: list[object] = []
    for term in terms:
        clauses.append("(" + " or ".join(f"{field} like ?" for field in fields) + ")")
        params.extend([f"%{term}%"] * len(fields))
    params.append(limit)
    try:
        return con.execute(
            f"""
            select page_path, page_title, country_or_tree, count(*) as missions
            from mission_sources
            where {' or '.join(clauses)}
            group by page_path, page_title, country_or_tree
            order by
              case
                when country_or_tree in ({','.join('?' for _ in terms)}) then 0
                when page_title in ({','.join('?' for _ in terms)}) then 1
                else 2
              end,
              missions desc
            limit ?
            """,
            params[:-1] + terms + terms + [limit],
        ).fetchall()
    except sqlite3.Error:
        return []


def locate_mission_pages_from_wiki(query: str, scope: str) -> list[str]:
    terms = " ".join(t for t in (scope, query, "任务 mission missions") if t)
    try:
        results, _ = search_index(terms, 10)
    except Exception:
        return []
    paths: list[str] = []
    for item in results:
        path = str(item.get("path", ""))
        title = str(item.get("title", ""))
        section = str(item.get("section", ""))
        combined = f"{path} {title} {section}".lower()
        if "任务" in combined or "mission" in combined:
            if path.endswith(".html"):
                paths.append(path)
    return paths


def mission_scope_alias_pages(query: str, scope: str) -> list[str]:
    text = f"{query} {scope}".lower()
    registry_text = scope or query
    registry_pages: list[str] = []
    if scope:
        scope_norm = re.sub(r"(?:任务|missions?)$", "", scope.strip(), flags=re.IGNORECASE).strip().lower()
        for entity in resolve_entities(scope, ("mission_page",), 12):
            candidates = {
                str(entity.get("alias", "")),
                str(entity.get("canonical", "")),
                str(entity.get("display_name", "")),
            }
            cleaned_candidates = {
                re.sub(r"(?:任务|missions?)$", "", candidate.strip(), flags=re.IGNORECASE).strip().lower()
                for candidate in candidates
                if candidate
            }
            path = str(entity.get("page_path", ""))
            if scope_norm in cleaned_candidates and path.endswith(".html") and path not in registry_pages:
                registry_pages.append(path)
        if not registry_pages:
            registry_pages = entity_page_hints(registry_text, ("mission_page",), 12)
    else:
        registry_pages = entity_page_hints(registry_text, ("mission_page",), 12)
    aliases = {
        "萨卢佐": ["意大利小国任务.html"],
        "saluzzo": ["意大利小国任务.html"],
        "里加": ["里加任务.html"],
        "riga": ["里加任务.html"],
    }
    pages: list[str] = list(registry_pages)
    for key, values in aliases.items():
        if key.lower() in text:
            pages.extend(values)
    return pages


def mission_fts_search(con: sqlite3.Connection, fts_query: str, limit: int) -> list[sqlite3.Row]:
    try:
        return con.execute(
            """
            select m.*, bm25(mission_sources_fts) as score
            from mission_sources_fts
            join mission_sources m on m.id = mission_sources_fts.rowid
            where mission_sources_fts match ?
            order by bm25(mission_sources_fts)
            limit ?
            """,
            (fts_query, limit),
        ).fetchall()
    except sqlite3.Error:
        return []


def mission_like_search(
    con: sqlite3.Connection,
    query: str,
    scope: str,
    page_hints: list[str],
    limit: int,
) -> list[sqlite3.Row]:
    terms = mission_entity_terms(query, scope)
    terms.extend(t for t in split_query_terms(query + " " + scope) if len(t) >= 2)
    terms = list(dict.fromkeys(terms))[:24] or [query]
    fields = (
        "page_title country_or_tree mission_title mission_id slot_or_section description "
        "conditions effects prerequisites version_note page_path raw_text"
    ).split()
    clauses = []
    params: list[object] = []
    for term in terms:
        clauses.append("(" + " or ".join(f"{field} like ?" for field in fields) + ")")
        params.extend([f"%{term}%"] * len(fields))
    page_paths = page_hints[:8] if is_mission_overview_query(query) else []
    page_clause = ""
    if page_paths:
        page_clause = " or page_path in (" + ",".join("?" for _ in page_paths) + ")"
        params.extend(page_paths)
    params.append(limit)
    try:
        return con.execute(
            f"select *, -1.0 as score from mission_sources where ({' or '.join(clauses)}{page_clause}) limit ?",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []


def merge_mission_rows(primary: list[sqlite3.Row], secondary: list[sqlite3.Row]) -> list[sqlite3.Row]:
    merged: list[sqlite3.Row] = []
    seen = set()
    for row in list(primary) + list(secondary):
        key = (row["page_path"], row["mission_title"], row["mission_id"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def rerank_mission_rows(
    rows: list[sqlite3.Row],
    query: str,
    scope: str,
    page_hint_set: set[str],
) -> list[sqlite3.Row]:
    query_terms = [t.lower() for t in split_query_terms(query + " " + scope) if len(t) >= 2]
    query_terms.extend(t.lower() for t in mission_entity_terms(query, scope))
    lowered_query = query.lower()
    if "ae" in lowered_query or "侵略扩张" in query:
        query_terms.extend(["侵略扩张", "侵略扩张影响", "aggressive expansion", "aggressive_expansion_impact"])
    if "外交声誉" in query:
        query_terms.extend(["外交声誉", "diplomatic reputation", "diplomatic_reputation"])
    if "传教强度" in query:
        query_terms.extend(["传教强度", "missionary strength", "global_missionary_strength"])
    if "行政效率" in query:
        query_terms.extend(["行政效率", "administrative efficiency", "administrative_efficiency"])
    query_terms = list(dict.fromkeys(query_terms))
    overview_query = is_mission_overview_query(query)

    def rank(row: sqlite3.Row) -> tuple[float, float]:
        page = str(row["page_path"])
        names = f"{row['page_title']} {row['country_or_tree']} {row['mission_title']} {row['mission_id']}".lower()
        core = f"{row['conditions']} {row['effects']} {row['prerequisites']}".lower()
        haystack = f"{names} {core} {row['raw_text']}".lower()
        hits = sum(1 for term in query_terms if term in haystack)
        name_hits = sum(1 for term in query_terms if term in names)
        effect_hits = sum(1 for term in query_terms if term in core)
        page_bonus = 80 if page in page_hint_set else 0
        exact_scope_bonus = 0
        if scope and scope.lower() in names:
            exact_scope_bonus += 60
        for term in mission_entity_terms(query, scope)[:6]:
            if term.lower() in names:
                exact_scope_bonus += 20
        effect_phrase_bonus = 0
        for phrase in ("侵略扩张", "侵略扩张影响", "外交声誉", "传教强度", "行政效率"):
            if phrase in query and phrase in core:
                effect_phrase_bonus += 80
        if ("ae" in lowered_query or "侵略扩张" in query) and "侵略扩张" in core:
            effect_phrase_bonus += 120
        relevance = page_bonus + exact_scope_bonus + hits * 10 + name_hits * 25 + effect_hits * 14 + effect_phrase_bonus
        try:
            bm25 = float(row["score"])
        except Exception:
            bm25 = 0.0
        if overview_query and page in page_hint_set:
            try:
                row_id = float(row["id"])
            except Exception:
                row_id = 0.0
            return (-page_bonus - exact_scope_bonus, row_id)
        return (-relevance, bm25)

    return sorted(rows, key=rank)


def achievement_country_terms(con: sqlite3.Connection, query: str) -> list[str]:
    query_lower = query.lower()
    terms: list[str] = []
    aliases = {
        "riga": "里加",
        "里加": "里加",
    }
    for key, value in aliases.items():
        if key in query_lower or key in query:
            terms.append(value)

    rows = con.execute(
        "select description, starting_conditions from achievements "
        "where description is not null or starting_conditions is not null"
    ).fetchall()
    candidates: set[str] = set()
    for row in rows:
        text = f"{row['description'] or ''}\n{row['starting_conditions'] or ''}"
        for match in re.findall(r"(?:以|用)([\u4e00-\u9fff]{2,8})开局", text):
            candidates.add(match)
        for match in re.findall(r"是\s+[\u00a0\s]*([\u4e00-\u9fff]{2,8})", text):
            candidates.add(match)
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate in query and candidate not in terms:
            terms.append(candidate)
    return terms[:6]


def achievement_country_start_search(con: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    terms = achievement_country_terms(con, query)
    if not terms:
        return []
    clauses = []
    params: list[object] = []
    for term in terms:
        start_patterns = (
            f"%是%{term}%",
            f"%以{term}开局%",
            f"%用{term}开局%",
            f"%作为{term}%",
        )
        clauses.append(
            "("
            "starting_conditions like ? or "
            "description like ? or description like ? or description like ? or "
            "completion_requirements like ?"
            ")"
        )
        params.extend(
            [
                start_patterns[0],
                start_patterns[1],
                start_patterns[2],
                start_patterns[3],
                f"%是%{term}%",
            ]
        )
    params.append(limit)
    return con.execute(
        f"select *, -5.0 as score from achievements where {' or '.join(clauses)} limit ?",
        params,
    ).fetchall()


def achievement_like_search(con: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    terms = [t for t in split_query_terms(query) if len(t) >= 2]
    terms = list(dict.fromkeys(terms))[:20] or [query]
    fields = (
        "english_name chinese_name description starting_conditions completion_requirements "
        "notes dlc version difficulty raw_text"
    ).split()
    clauses = []
    params: list[object] = []
    for term in terms:
        clauses.append("(" + " or ".join(f"{field} like ?" for field in fields) + ")")
        params.extend([f"%{term}%"] * len(fields))
    params.append(limit)
    return con.execute(
        f"select *, -1.0 as score from achievements where {' or '.join(clauses)} limit ?",
        params,
    ).fetchall()


def achievement_country_terms(con: sqlite3.Connection, query: str) -> list[str]:
    query_lower = query.lower()
    terms: list[str] = []
    for key, value in (
        ("riga", "\u91cc\u52a0"),
        ("\u91cc\u52a0", "\u91cc\u52a0"),
    ):
        if key in query_lower or key in query:
            terms.append(value)
    registry_terms: list[str] = []
    for entity in resolve_entities(query, ("mission_page", "country", "page"), 8):
        for value in (entity.get("display_name", ""), entity.get("canonical", ""), entity.get("alias", "")):
            candidate = str(value or "").strip()
            candidate = re.sub(r"(?:\u4efb\u52a1|missions?)$", "", candidate, flags=re.IGNORECASE).strip()
            candidate = candidate.split("/")[0].split(":")[-1].strip()
            if candidate in {"", "\u4efb\u52a1", "\u4efb\u52a1\u6811", "\u6210\u5c31", "mission", "missions"}:
                continue
            if 2 <= len(candidate) <= 10 and re.search(r"[\u4e00-\u9fff]", candidate) and candidate not in registry_terms:
                registry_terms.append(candidate)
    if terms:
        for candidate in registry_terms:
            if candidate not in terms:
                terms.append(candidate)
    elif registry_terms:
        terms.append(registry_terms[0])

    rows = con.execute(
        "select description, starting_conditions from achievements "
        "where description is not null or starting_conditions is not null"
    ).fetchall()
    candidates: set[str] = set()
    start_pattern = re.compile(f"(?:\u4ee5|\u7528)([\u4e00-\u9fff]{{2,8}})\u5f00\u5c40")
    is_pattern = re.compile(f"\u662f\\s*[\u00a0\\s]*([\u4e00-\u9fff]{{2,8}})")
    for row in rows:
        text = f"{row['description'] or ''}\n{row['starting_conditions'] or ''}"
        candidates.update(start_pattern.findall(text))
        candidates.update(is_pattern.findall(text))
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate in query and candidate not in terms:
            terms.append(candidate)
    return terms[:6]


def achievement_country_start_search(con: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    terms = achievement_country_terms(con, query)
    if not terms:
        return []
    clauses = []
    params: list[object] = []
    for term in terms:
        clauses.append(
            "("
            "starting_conditions like ? or "
            "description like ? or description like ? or description like ? or "
            "completion_requirements like ?"
            ")"
        )
        params.extend(
            [
                f"%\u662f%{term}%",
                f"%\u4ee5{term}\u5f00\u5c40%",
                f"%\u7528{term}\u5f00\u5c40%",
                f"%\u4f5c\u4e3a{term}%",
                f"%\u662f%{term}%",
            ]
        )
    params.append(limit)
    return con.execute(
        f"select *, -5.0 as score from achievements where {' or '.join(clauses)} limit ?",
        params,
    ).fetchall()


def is_country_start_achievement(row: sqlite3.Row, country: str) -> bool:
    description = str(row["description"] or "")
    starting_conditions = str(row["starting_conditions"] or "")
    escaped = re.escape(country)
    if re.search(f"(?:\u4ee5|\u7528|\u4f5c\u4e3a){escaped}", description):
        return True
    if re.search(f"\u662f\\s*[\u00a0\\s]*{escaped}", starting_conditions):
        return True
    return False


def infer_achievement_queries(text: str) -> list[str]:
    lowered = text.lower()
    has_achievement_intent = "\u6210\u5c31" in text or "achievement" in lowered or "achievements" in lowered
    if not has_achievement_intent:
        return []
    queries = [text]
    path = achievement_db_path()
    if path.exists():
        try:
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            try:
                for term in achievement_country_terms(con, text):
                    queries.append(f"{term} \u6210\u5c31")
            finally:
                con.close()
        except Exception:
            pass
    return list(dict.fromkeys(q for q in queries if q.strip()))[:3]


def text_has_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def infer_effect_phrase(text: str) -> str:
    lowered = text.lower()
    wants_reduce = text_has_any(text, ("\u51cf\u5c11", "\u964d\u4f4e", "reduce", "lower", "decrease", "-"))
    wants_add = text_has_any(text, ("\u589e\u52a0", "\u63d0\u9ad8", "increase", "add", "+"))
    prefix = "\u51cf\u5c11 " if wants_reduce else ("\u589e\u52a0 " if wants_add else "")
    mapping = (
        (("ae", "\u4fb5\u7565\u6269\u5f20", "aggressive expansion", "aggressive_expansion"), "\u4fb5\u7565\u6269\u5f20\u5f71\u54cd AE aggressive_expansion_impact"),
        (("\u5916\u4ea4\u58f0\u8a89", "diplomatic reputation", "diplomatic_reputation"), "\u5916\u4ea4\u58f0\u8a89 diplomatic reputation diplomatic_reputation"),
        (("\u5916\u4ea4\u541e\u5e76", "\u5916\u4ea4\u5408\u5e76", "diplomatic annexation", "annexation cost"), "\u5916\u4ea4\u541e\u5e76\u82b1\u8d39 diplomatic annexation cost diplomatic_annexation_cost"),
        (("\u6539\u5584\u5173\u7cfb", "improve relation", "improve_relation"), "\u6539\u5584\u5173\u7cfb improve_relation_modifier"),
        (("\u4f20\u6559\u5f3a\u5ea6", "missionary strength", "missionary_strength"), "\u4f20\u6559\u5f3a\u5ea6 global_missionary_strength"),
        (("\u884c\u653f\u6548\u7387", "administrative efficiency", "admin efficiency"), "\u884c\u653f\u6548\u7387 administrative_efficiency"),
        (("\u9020\u6838", "\u6838\u5fc3\u5316", "core creation", "core_creation"), "\u9020\u6838 core_creation_cost"),
        (("\u7a33\u5b9a", "stability"), "\u7a33\u5b9a stability stability_cost_modifier"),
        (("\u6218\u4e89\u5206\u6570", "war score", "warscore"), "\u6218\u4e89\u5206\u6570 warscore cost"),
        (("\u6700\u5927\u4e13\u5236", "max absolutism", "maximum absolutism"), "\u6700\u5927\u4e13\u5236\u5ea6 max_absolutism"),
        (("\u987e\u95ee\u82b1\u8d39", "advisor cost"), "\u987e\u95ee\u82b1\u8d39 advisor_cost"),
    )
    for keys, value in mapping:
        if text_has_any(text, keys):
            return prefix + value
    return prefix + text[:120]


def infer_source_type_from_text(text: str) -> str:
    type_hints = (
        ("event", ("\u4e8b\u4ef6", "\u63a7\u5236\u53f0", "\u6307\u4ee4", "event", "console command")),
        ("mission", ("\u4efb\u52a1", "mission")),
        ("decision", ("\u51b3\u8bae", "decision")),
        ("idea", ("\u7406\u5ff5", "idea")),
        ("policy", ("\u653f\u7b56", "policy")),
        ("reform", ("\u6539\u9769", "\u653f\u5e9c\u6539\u9769", "reform")),
        ("religion", ("\u5b97\u6559", "\u4fe1\u4ef0", "\u5b66\u6d3e", "\u795d\u798f", "religion")),
        ("estate", ("\u9636\u5c42", "\u7279\u6743", "estate", "privilege")),
        ("great_project", ("\u5947\u89c2", "\u4f1f\u5927\u5de5\u7a0b", "great project")),
        ("modifier", ("\u4fee\u6b63", "modifier")),
        ("trait", ("\u7279\u8d28", "\u7279\u6027", "trait")),
    )
    for source_type, hints in type_hints:
        if text_has_any(text, hints):
            return source_type
    return ""


def infer_mission_scopes(text: str) -> list[str]:
    scopes: list[str] = []
    aliases = (
        ("\u91cc\u52a0", "\u91cc\u52a0"),
        ("riga", "\u91cc\u52a0"),
        ("\u8428\u5362\u4f50", "\u8428\u5362\u4f50"),
        ("saluzzo", "\u8428\u5362\u4f50"),
        ("\u5965\u5730\u5229", "\u5965\u5730\u5229"),
        ("austria", "\u5965\u5730\u5229"),
    )
    lowered = text.lower()
    for key, value in aliases:
        if key.lower() in lowered and value not in scopes:
            scopes.append(value)
    path = mission_db_path()
    if path.exists():
        try:
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "select distinct page_title, country_or_tree from mission_sources "
                    "where page_title is not null or country_or_tree is not null"
                ).fetchall()
            finally:
                con.close()
            candidates: set[str] = set()
            for row in rows:
                for value in (row["page_title"], row["country_or_tree"]):
                    value = str(value or "").strip()
                    if not value:
                        continue
                    value = re.sub(f"(?:\u4efb\u52a1|missions?)$", "", value, flags=re.IGNORECASE).strip()
                    if 2 <= len(value) <= 12:
                        candidates.add(value)
            for candidate in sorted(candidates, key=len, reverse=True):
                if candidate in text and candidate not in scopes:
                    scopes.append(candidate)
        except Exception:
            pass
    registry_scopes: list[str] = []
    for entity in resolve_entities(text, ("mission_page", "country"), 8):
        for value in (entity.get("display_name", ""), entity.get("canonical", ""), entity.get("alias", "")):
            candidate = str(value or "").strip()
            candidate = re.sub(f"(?:\u4efb\u52a1|missions?)$", "", candidate, flags=re.IGNORECASE).strip()
            candidate = candidate.split("/")[0].split(":")[-1].strip()
            if candidate in {"", "\u4efb\u52a1", "\u4efb\u52a1\u6811", "mission", "missions"}:
                continue
            if str(entity.get("display_name", "")) in {"\u4efb\u52a1", "Mission", "Missions"}:
                continue
            if 2 <= len(candidate) <= 12 and re.search(r"[\u4e00-\u9fff]", candidate) and candidate not in registry_scopes:
                registry_scopes.append(candidate)
    registry_scopes = sorted(registry_scopes, key=len, reverse=True)
    if scopes:
        if registry_scopes and len(registry_scopes[0]) > max(len(scope) for scope in scopes):
            scopes = [registry_scopes[0]] + [scope for scope in scopes if scope != registry_scopes[0]]
        for candidate in registry_scopes:
            if candidate not in scopes:
                scopes.append(candidate)
    elif registry_scopes:
        scopes.append(registry_scopes[0])
    return scopes[:4]


def infer_local_search_hints(question: str) -> dict:
    hints: dict[str, list] = {
        "achievement_queries": [],
        "mission_queries": [],
        "effect_source_queries": [],
        "queries": [],
        "page_context_queries": [],
    }
    text = question.strip()
    lowered = text.lower()
    is_achievement = "\u6210\u5c31" in text or "achievement" in lowered
    is_mission = "\u4efb\u52a1" in text or "mission" in lowered
    is_trait = text_has_any(text, ("\u7279\u8d28", "\u7279\u6027", "\u8c28\u614e", "\u6743\u91cd", "\u6982\u7387", "\u9886\u8896", "\u7edf\u6cbb\u8005", "\u7ee7\u627f\u4eba", "trait"))
    is_console_event = text_has_any(text, ("\u63a7\u5236\u53f0", "\u6307\u4ee4", "console command")) or text_has_any(text, ("\u739b\u4e3d", "\u5760\u9a6c", "mary", "horse"))

    if is_achievement:
        hints["achievement_queries"].extend(infer_achievement_queries(text))

    mission_scopes = infer_mission_scopes(text)
    if is_mission:
        effect_phrase = infer_effect_phrase(text)
        if mission_scopes:
            for scope in mission_scopes:
                hints["mission_queries"].append({"query": text, "scope": scope})
                if effect_phrase != text[:120]:
                    hints["effect_source_queries"].append(
                        {"effect_query": effect_phrase, "source_type": "mission", "scope": scope}
                    )
        else:
            hints["mission_queries"].append({"query": text, "scope": ""})

    if is_trait:
        hints["effect_source_queries"].append(
            {"effect_query": infer_trait_query(text), "source_type": "trait", "scope": infer_trait_scope(text)}
        )

    effect_intent = text_has_any(
        text,
        (
            "\u6709\u4ec0\u4e48",
            "\u54ea\u4e9b",
            "\u54ea\u4e2a",
            "\u7ed9",
            "\u83b7\u5f97",
            "\u51cf\u5c11",
            "\u964d\u4f4e",
            "\u589e\u52a0",
            "\u63d0\u9ad8",
            "which",
            "what",
        ),
    )
    effect_phrase = infer_effect_phrase(text)
    source_type = infer_source_type_from_text(text)
    if effect_intent and effect_phrase != text[:120] and not is_trait and not (is_mission and source_type == "mission"):
        hints["effect_source_queries"].append(
            {"effect_query": effect_phrase, "source_type": source_type, "scope": ""}
        )

    if effect_intent and not is_trait and not is_mission:
        entity_types = ("religion", "reform", "decision", "idea", "policy", "estate", "great_project", "modifier")
        for entity in resolve_entities(text, entity_types, 3):
            entity_type = str(entity.get("entity_type") or "")
            entity_query = " ".join(
                part
                for part in (
                    str(entity.get("display_name") or ""),
                    str(entity.get("canonical") or ""),
                    str(entity.get("alias") or ""),
                    text,
                )
                if part
            )
            hints["effect_source_queries"].append(
                {"effect_query": entity_query, "source_type": entity_type, "scope": ""}
            )
            break

    event_detail_intent = text_has_any(
        text,
        (
            "\u4e8b\u4ef6",
            "\u89e6\u53d1",
            "\u6761\u4ef6",
            "\u6548\u679c",
            "\u9009\u9879",
            "event",
            "trigger",
            "condition",
            "effect",
        ),
    )
    if event_detail_intent and not is_mission and not is_trait:
        for entity in resolve_entities(text, ("event",), 3):
            entity_query = " ".join(
                part
                for part in (
                    str(entity.get("display_name") or ""),
                    str(entity.get("canonical") or ""),
                    str(entity.get("source_id") or ""),
                    str(entity.get("alias") or ""),
                    text,
                )
                if part
            )
            hints["effect_source_queries"].append(
                {"effect_query": entity_query, "source_type": "event", "scope": ""}
            )
            path_hint = str(entity.get("page_path") or "")
            if path_hint.endswith(".html"):
                hints["page_context_queries"].append({"path_hint": path_hint, "query": entity_query})
            break

    if is_console_event:
        alias_query = text
        if text_has_any(text, ("\u739b\u4e3d", "\u5760\u9a6c", "mary", "horse")):
            alias_query = "\u52c3\u826e\u7b2c\u5973\u516c\u7235\u53bb\u4e16 incidents_bur_inheritance.5 \u739b\u4e3d \u5760\u9a6c"
            hints["page_context_queries"].append({"path_hint": "\u52c3\u826e\u7b2c\u4e8b\u4ef6.html", "query": "\u739b\u4e3d \u5760\u9a6c incidents_bur_inheritance"})
        if alias_query != text:
            hints["effect_source_queries"].append({"effect_query": alias_query, "source_type": "event", "scope": ""})
        hints["queries"].append("\u63a7\u5236\u53f0\u6307\u4ee4 event command")

    return hints


def merge_achievement_rows(primary: list[sqlite3.Row], secondary: list[sqlite3.Row]) -> list[sqlite3.Row]:
    merged = []
    seen = set()
    for row in list(primary) + list(secondary):
        key = (row["english_name"], row["chinese_name"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def rerank_achievement_rows(rows: list[sqlite3.Row], query: str) -> list[sqlite3.Row]:
    query_terms = [t.lower() for t in split_query_terms(query) if len(t) >= 2]
    query_terms = list(dict.fromkeys(query_terms))
    country_terms: list[str] = []
    if "里加" in query or "riga" in query.lower():
        country_terms.append("里加")

    def rank(row: sqlite3.Row) -> tuple[float, float]:
        names = f"{row['english_name']} {row['chinese_name']}".lower()
        reqs = f"{row['description']} {row['starting_conditions']} {row['completion_requirements']}".lower()
        notes = str(row["notes"]).lower()
        haystack = f"{names} {reqs} {notes} {row['difficulty']} {row['version']}".lower()
        hits = sum(1 for term in query_terms if term in haystack)
        name_hits = sum(1 for term in query_terms if term in names)
        req_hits = sum(1 for term in query_terms if term in reqs)
        phrase_bonus = 1 if query.lower() in haystack else 0
        country_start_bonus = 0
        if country_terms:
            description = str(row["description"] or "")
            starting_conditions = str(row["starting_conditions"] or "")
            completion_requirements = str(row["completion_requirements"] or "")
            for term in country_terms:
                if re.search(rf"(?:以|用){re.escape(term)}开局", description):
                    country_start_bonus += 180
                if term in starting_conditions:
                    country_start_bonus += 160
                if re.search(rf"是\s+[\u00a0\s]*{re.escape(term)}", completion_requirements):
                    country_start_bonus += 90
                if term in notes and not country_start_bonus:
                    country_start_bonus -= 40
        relevance = hits * 16 + name_hits * 35 + req_hits * 18 + phrase_bonus * 50 + country_start_bonus
        try:
            bm25 = float(row["score"])
        except Exception:
            bm25 = 0.0
        return (-relevance, bm25)

    return sorted(rows, key=rank)


def rerank_achievement_rows(rows: list[sqlite3.Row], query: str) -> list[sqlite3.Row]:
    query_terms = [t.lower() for t in split_query_terms(query) if len(t) >= 2]
    query_terms = list(dict.fromkeys(query_terms))
    country_terms: list[str] = []
    if "\u91cc\u52a0" in query or "riga" in query.lower():
        country_terms.append("\u91cc\u52a0")

    def rank(row: sqlite3.Row) -> tuple[float, float]:
        names = f"{row['english_name']} {row['chinese_name']}".lower()
        reqs = f"{row['description']} {row['starting_conditions']} {row['completion_requirements']}".lower()
        notes = str(row["notes"]).lower()
        haystack = f"{names} {reqs} {notes} {row['difficulty']} {row['version']}".lower()
        hits = sum(1 for term in query_terms if term in haystack)
        name_hits = sum(1 for term in query_terms if term in names)
        req_hits = sum(1 for term in query_terms if term in reqs)
        phrase_bonus = 1 if query.lower() in haystack else 0
        country_start_bonus = 0
        for term in country_terms:
            escaped = re.escape(term)
            description = str(row["description"] or "")
            starting_conditions = str(row["starting_conditions"] or "")
            completion_requirements = str(row["completion_requirements"] or "")
            if re.search(f"(?:\u4ee5|\u7528){escaped}\u5f00\u5c40", description):
                country_start_bonus += 180
            if term in starting_conditions:
                country_start_bonus += 160
            if re.search(f"\u662f\\s*[\u00a0\\s]*{escaped}", completion_requirements):
                country_start_bonus += 90
            if term in notes and not country_start_bonus:
                country_start_bonus -= 40
        relevance = hits * 16 + name_hits * 35 + req_hits * 18 + phrase_bonus * 50 + country_start_bonus
        try:
            bm25 = float(row["score"])
        except Exception:
            bm25 = 0.0
        return (-relevance, bm25)

    return sorted(rows, key=rank)


def expand_effect_query(effect_query: str, scope: str = "") -> str:
    terms = [effect_query]
    if scope:
        terms.append(scope)
    effect_expansions = {
        "ae": "侵略扩张 aggressive expansion aggressive_expansion_impact ae impact",
        "AE": "侵略扩张 aggressive expansion aggressive_expansion_impact ae impact",
        "侵略扩张": "aggressive expansion aggressive_expansion_impact AE",
        "外交声誉": "diplomatic reputation diplomatic_reputation",
        "改善关系": "improve relations improve_relation_modifier",
        "传教强度": "missionary strength global_missionary_strength",
        "行政效率": "administrative efficiency administrative_efficiency",
        "造核": "core creation core_creation core_creation_cost",
        "稳定": "stability stability cost stability_cost_modifier",
        "战争分数": "war score cost warscore cost",
        "最大专制度": "max absolutism maximum absolutism max_absolutism",
        "自治度": "autonomy local_autonomy",
    }
    for key, value in effect_expansions.items():
        if key.lower() in effect_query.lower():
            terms.append(value)
    tokens = []
    for term in terms:
        tokens.extend(split_query_terms(term))
    unique = []
    seen = set()
    for token in tokens:
        low = token.lower()
        if low not in seen:
            seen.add(low)
            unique.append(token)
    return " OR ".join(quote_fts_term(t) for t in unique if t.strip())


def effect_fts_search(con: sqlite3.Connection, fts_query: str, source_type: str, limit: int) -> list[sqlite3.Row]:
    where = ["effect_sources_fts match ?"]
    params: list[object] = [fts_query]
    if source_type:
        where.append("e.source_type = ?")
        params.append(source_type)
    params.append(limit)
    sql = f"""
        select e.*, bm25(effect_sources_fts) as score
        from effect_sources_fts
        join effect_sources e on e.id = effect_sources_fts.rowid
        where {' and '.join(where)}
        order by bm25(effect_sources_fts)
        limit ?
    """
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def effect_like_search(
    con: sqlite3.Connection,
    effect_query: str,
    source_type: str,
    scope: str,
    limit: int,
) -> list[sqlite3.Row]:
    terms = [t for t in split_query_terms(effect_query + " " + scope) if len(t) >= 2]
    terms = list(dict.fromkeys(terms))[:24] or [effect_query]
    clauses = []
    params: list[object] = []
    fields = "source_title source_id option_or_section conditions effects scope page_path raw_text".split()
    for term in terms:
        clauses.append("(" + " or ".join(f"{field} like ?" for field in fields) + ")")
        like = f"%{term}%"
        params.extend([like] * len(fields))
    type_clause = ""
    if source_type:
        type_clause = " and source_type = ?"
        params.append(source_type)
    params.append(limit)
    sql = f"""
        select *, -1.0 as score
        from effect_sources
        where ({' or '.join(clauses)}){type_clause}
        limit ?
    """
    return con.execute(sql, params).fetchall()


def merge_effect_rows(primary: list[sqlite3.Row], secondary: list[sqlite3.Row]) -> list[sqlite3.Row]:
    merged: list[sqlite3.Row] = []
    seen = set()
    for row in list(primary) + list(secondary):
        if row["source_id"]:
            key = (row["source_type"], row["source_id"])
        else:
            key = (row["source_type"], row["source_title"], row["option_or_section"], row["page_path"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def is_noise_effect_row(row: sqlite3.Row) -> bool:
    path = str(row["page_path"])
    if is_noise_path(path):
        return True
    title = str(row["source_title"])
    raw_text = str(row["raw_text"])
    combined = f"{title}\n{row['conditions']}\n{row['effects']}\n{raw_text[:700]}"
    if is_clear_low_value_source(path, title, combined):
        return True
    if title in {"成就", "Achievement", "Achievements", "总决议列表", "列表", "索引"}:
        return True
    if title.startswith("欧陆风云4百科:"):
        return True
    if "modding" in path.lower() or "modding" in title.lower():
        return True
    if is_noise_text(combined):
        return True
    head = "\n".join(raw_text.splitlines()[:4])
    if "目录" in head and not any(marker in raw_text for marker in ("效果", "奖励", "触发条件", "获得", "修正")):
        return True
    if row["source_type"] in {"decision", "event", "mission"} and re.search(
        r"\bmod(?:ding)?\b|mod文件夹|创建.*mod", raw_text, flags=re.IGNORECASE
    ):
        return True
    return False


def filter_noise_effect_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in rows if not is_noise_effect_row(row)]


def filter_effect_topic_rows(rows: list[sqlite3.Row], effect_query: str) -> list[sqlite3.Row]:
    lowered_query = effect_query.lower()
    topic_groups = []
    if "侵略扩张" in effect_query or "aggressive_expansion" in lowered_query or "aggressive expansion" in lowered_query or re.search(r"\bae\b", lowered_query):
        topic_groups.append(("侵略扩张", "aggressive expansion", "aggressive_expansion"))
    if "外交声誉" in effect_query or "diplomatic reputation" in lowered_query or "diplomatic_reputation" in lowered_query:
        topic_groups.append(("外交声誉", "diplomatic reputation", "diplomatic_reputation"))
    if "改善关系" in effect_query or "improve_relation" in lowered_query or "improve relations" in lowered_query:
        topic_groups.append(("改善关系", "improve relations", "improve_relation"))
    if "传教强度" in effect_query or "missionary strength" in lowered_query or "missionary_strength" in lowered_query:
        topic_groups.append(("传教强度", "missionary strength", "missionary_strength"))
    if not topic_groups:
        return rows

    filtered = []
    for row in rows:
        haystack = " ".join(
            str(row[key])
            for key in ("source_title", "source_id", "option_or_section", "conditions", "effects", "scope", "page_path", "raw_text")
        ).lower()
        if all(any(token.lower() in haystack for token in group) for group in topic_groups):
            filtered.append(row)
    return filtered


def rerank_effect_rows(
    rows: list[sqlite3.Row],
    effect_query: str,
    source_type: str,
    scope: str,
) -> list[sqlite3.Row]:
    query_terms = [t.lower() for t in split_query_terms(effect_query + " " + scope) if len(t) >= 2]
    query_terms = list(dict.fromkeys(query_terms))

    def rank(row: sqlite3.Row) -> tuple[float, float]:
        fields = [
            str(row["source_type"]),
            str(row["source_title"]),
            str(row["source_id"]),
            str(row["option_or_section"]),
            str(row["conditions"]),
            str(row["effects"]),
            str(row["scope"]),
            str(row["page_path"]),
            str(row["raw_text"]),
        ]
        haystack = " ".join(fields).lower()
        effects = str(row["effects"]).lower()
        title = str(row["source_title"]).lower()
        source_id = str(row["source_id"]).lower()
        hits = sum(1 for term in query_terms if term in haystack)
        effect_hits = sum(1 for term in query_terms if term in effects)
        title_hits = sum(1 for term in query_terms if term in title or term in source_id)
        relevance = hits * 18 + effect_hits * 26 + title_hits * 14
        if source_id and source_id in effect_query.lower():
            relevance += 220
        if title and str(row["source_title"]) in effect_query:
            relevance += 180
        if row["source_type"] == "trait":
            named_trait_terms = [term for term in query_terms if term not in {"特质", "特性", "trait", "权重"}]
            if any(term in title or term in source_id for term in named_trait_terms):
                relevance += 130
            elif named_trait_terms and any(term in haystack for term in named_trait_terms):
                relevance -= 70
        wants_reduction = any(term in effect_query for term in ("减少", "降低", "减", "-")) or any(
            term in effect_query.lower() for term in ("reduce", "reduced", "less", "lower", "decrease", "−")
        )
        if wants_reduction:
            if any(marker in effects for marker in ("−", "-", "减少", "降低")):
                relevance += 80
            if re.search(r"(?:\+|增加)\s*\d", effects):
                relevance -= 90
        if source_type and row["source_type"] == source_type:
            relevance += 45
        if scope and scope.lower() in haystack:
            relevance += 55
        if ("event" in effect_query.lower() or "事件" in effect_query) and row["source_type"] == "event":
            relevance += 30
        if ("任务" in effect_query or "mission" in effect_query.lower()) and row["source_type"] == "mission":
            relevance += 30
        try:
            bm25 = float(row["score"])
        except Exception:
            bm25 = 0.0
        return (-relevance, bm25)

    return sorted(rows, key=rank)


def merge_rows(primary: list[sqlite3.Row], secondary: list[sqlite3.Row]) -> list[sqlite3.Row]:
    merged: list[sqlite3.Row] = []
    seen = set()
    for row in list(primary) + list(secondary):
        key = (row["title"], row["section"], row["path"], row["body"][:160])
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def is_noise_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(pattern.search(normalized) for pattern in NOISE_PATH_PATTERNS)


def is_noise_text(text: str) -> bool:
    return any(marker in text for marker in NOISE_TEXT_MARKERS)


def is_clear_low_value_source(path: str, title: str = "", text: str = "") -> bool:
    combined = f"{path}\n{title}\n{text}".lower().replace("\\", "/")
    if re.search(r"(^|/)(?:1\.[0-2]|1\.3[0-6])(?:\.\d+)?(?:_|%|\.html|$)", combined):
        return True
    low_value_tokens = (
        "modding",
        "\u6a21\u7ec4",
        "\u8ba8\u8bba",
        "talk:",
        "template:",
        "user:",
        "\u7248\u672c\u5386\u53f2",
        "\u65e7\u7248\u672c",
        "\u8fc7\u65f6",
    )
    return any(token in combined for token in low_value_tokens)


def is_noise_row(row: sqlite3.Row) -> bool:
    path = str(row["path"])
    if is_noise_path(path):
        return True
    title = str(row["title"])
    section = str(row["section"])
    body = str(row["body"])
    if is_clear_low_value_source(path, title, f"{section}\n{body[:700]}"):
        return True
    if title in {"成就", "Achievement", "Achievements", "总决议列表", "列表", "索引"}:
        return True
    if title.startswith("欧陆风云4百科:"):
        return True
    if "modding" in path.lower() or "modding" in title.lower():
        return True
    if is_noise_text(f"{title}\n{section}\n{body[:500]}"):
        return True
    return False


def filter_noise_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in rows if not is_noise_row(row)]


def like_search(con: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    expansion_bits = []
    if ("兵种" in query or "unit" in query.lower()) and ("点数" in query or "pips" in query.lower()):
        expansion_bits.append("pips 陆战 骰子计算 火力 冲击 士气 点数")
    if any(term in query for term in ("玛丽", "坠马")):
        expansion_bits.append("勃艮第女公爵去世 勃艮第继承危机 incidents_bur_inheritance.5 incidents_bur_inheritance.501 坠马")
    for key, value in QUERY_EXPANSIONS.items():
        if key.lower() in query.lower():
            expansion_bits.append(value)
    terms = [t for bit in expansion_bits for t in split_query_terms(bit) if len(t) >= 2]
    terms.extend(t for t in split_query_terms(query) if len(t) >= 2)
    terms = list(dict.fromkeys(terms))
    terms = terms[:24] or [query]
    clauses = []
    params = []
    for term in terms:
        clauses.append("(title like ? or section like ? or body like ?)")
        like = f"%{term}%"
        params.extend([like, like, like])
    sql = f"""
        select title, section, body, path, -1.0 as score
        from chunks
        where {' or '.join(clauses)}
        limit ?
    """
    params.append(limit)
    return con.execute(sql, params).fetchall()


def rerank_rows(rows: list[sqlite3.Row], query: str) -> list[sqlite3.Row]:
    query_terms = [t.lower() for t in split_query_terms(query) if len(t) >= 2]
    query_terms = list(dict.fromkeys(query_terms))

    def rank(row: sqlite3.Row) -> tuple[float, float]:
        title = str(row["title"]).lower()
        section = str(row["section"]).lower()
        body = str(row["body"]).lower()
        path = str(row["path"]).lower()
        haystack = f"{title} {section} {body} {path}"
        hits = sum(1 for term in query_terms if term in haystack)
        title_hits = sum(1 for term in query_terms if term in title or term in path)
        section_hits = sum(1 for term in query_terms if term in section)
        phrase_bonus = 1 if query.lower() in haystack else 0
        relevance = hits * 20 + title_hits * 18 + section_hits * 8 + phrase_bonus * 25
        relevance += domain_boost(query.lower(), title, section, body, path)
        try:
            bm25 = float(row["score"])
        except Exception:
            bm25 = 0.0
        return (-relevance, bm25)

    return sorted(rows, key=rank)


def domain_boost(query: str, title: str, section: str, body: str, path: str) -> int:
    boost = 0
    combined = f"{title} {section} {body} {path}"
    if ("任务" in query or "mission" in query) and ("任务" in title or "missions" in path):
        boost += 55
    if ("萨卢佐" in query or "saluzzo" in query) and ("萨卢佐" in combined or "saluzzo" in combined):
        boost += 65
    if ("萨卢佐" in query or "saluzzo" in query) and "意大利小国任务" in combined:
        boost += 85
    if ("点数" in query or "pips" in query) and ("pips" in path or "点数" in title or "骰子计算" in combined):
        boost += 75
    if ("兵种" in query or "unit" in query) and ("点数" in query or "pips" in query):
        if "pips" in path or "陆战" in title or "骰子计算" in combined:
            boost += 120
        if "君主点数" in title:
            boost -= 120
    if ("冲击" in query or "伤害" in query or "damage" in query or "shock" in query) and (
        path in {"battle.html", "pips.html"} or "陆战" in title
    ):
        boost += 60
    if any(term in query for term in ("玛丽", "坠马")):
        if "坠马" in combined or "勃艮第女公爵去世" in combined:
            boost += 180
        if "勃艮第继承危机" in path or "burgundian_inheritance" in path or "burgundian_events" in path:
            boost += 150
        if "控制台指令" in title and not ("坠马" in combined or "勃艮第" in combined):
            boost -= 120
    return boost


def compact(text: str, max_len: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def normalize_history(raw_history: object) -> list[dict[str, str]]:
    if not isinstance(raw_history, list):
        return []
    history: list[dict[str, str]] = []
    for item in raw_history[-MAX_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            continue
        user = str(item.get("user", "")).strip()
        assistant = str(item.get("assistant", "")).strip()
        if not user or not assistant:
            continue
        history.append({"user": compact(user, 800), "assistant": compact(assistant, 1200)})
    return history


def format_retrieved_context(retrieved: list[dict], heading: str = "本轮初始检索片段") -> str:
    context = "\n\n".join(format_cited_item(i, item) for i, item in enumerate(retrieved, start=1))
    return f"{heading}：\n{context}" if context else f"{heading}：无"


def format_cited_item(index: int, item: dict) -> str:
    return f"[{index}] {item['title']} / {item['section']} / {item['path']}\n{item['snippet']}"


def result_key(item: dict) -> tuple[str, str, str, str]:
    return (
        str(item.get("title", "")),
        str(item.get("section", "")),
        str(item.get("path", "")),
        str(item.get("snippet", ""))[:160],
    )


def citation_index(retrieved: list[dict], item: dict) -> int | None:
    key = result_key(item)
    for index, existing in enumerate(retrieved, start=1):
        if result_key(existing) == key:
            return index
    return None


def append_unique_results(retrieved: list[dict], new_results: list[dict]) -> list[tuple[int, dict]]:
    cited: list[tuple[int, dict]] = []
    for item in new_results:
        existing_index = citation_index(retrieved, item)
        if existing_index is None:
            retrieved.append(item)
            existing_index = len(retrieved)
        cited.append((existing_index, item))
    return cited


def build_search_plan_prompt(question: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
    history_text = "\n".join(
        f"用户：{turn['user']}\n助手：{turn['assistant']}" for turn in history[-3:]
    )
    return [
        {
            "role": "system",
            "content": (
                "你是 EU4 wiki 检索规划器。你的任务不是回答问题，而是把用户问题改写成高质量检索计划。"
                "只输出 JSON，不要输出 Markdown。JSON 字段：intent, entities, queries, achievement_queries, effect_source_queries, page_context_queries。"
                "queries 是字符串数组，最多 5 个；page_context_queries 是对象数组，最多 3 个，每个对象含 path_hint 和 query。"
                "achievement_queries 是字符串数组，最多 3 个；当用户问成就名称、条件、路线、难度或 achievement 时，必须使用 achievement_queries。"
                "effect_source_queries 是对象数组，最多 5 个，每个对象含 effect_query、source_type、scope；"
                "当用户问“什么事件/任务/政策/理念/改革/宗教/特权/奇观/修正给某种效果”时，必须使用 effect_source_queries。"
                "source_type 可选：event, mission, decision, idea, policy, reform, religion, estate, great_project, modifier。"
                "如果用户提到黑话或俗称，要给出可能的正式术语。"
                "示例：玛丽小姐坠马 => 坠马, 勃艮第女公爵去世, 勃艮第继承危机, incidents_bur_inheritance。"
                "示例：减少 AE 的事件 => effect_source_queries=[{effect_query:'侵略扩张影响 AE aggressive_expansion_impact', source_type:'event', scope:''}]。"
            ),
        },
        {
            "role": "user",
            "content": f"会话历史：\n{history_text or '无'}\n\n当前问题：{question}",
        },
    ]


def plan_search(question: str, history: list[dict[str, str]]) -> tuple[dict, str | None]:
    config = read_llm_config()
    if config.get("_config_error"):
        return fallback_search_plan(question), config["_config_error"]
    api_key = config.get("api_key", "")
    if not api_key:
        return fallback_search_plan(question), None
    base_url = config.get("base_url", "https://api.openai.com/v1").rstrip("/") or "https://api.openai.com/v1"
    model = config.get("model", "gpt-4.1-mini")
    payload: dict[str, object] = {
        "model": model,
        "messages": build_search_plan_prompt(question, history),
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    try:
        message = request_chat_completion(base_url, api_key, payload)
        plan = parse_json_object(message.get("content") or "")
        return normalize_search_plan(plan, question), None
    except Exception as exc:
        return fallback_search_plan(question), f"检索规划失败，已退回默认检索：{exc}"


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
    return data if isinstance(data, dict) else {}


def normalize_search_plan(plan: dict, question: str) -> dict:
    queries = [question]
    raw_queries = plan.get("queries", [])
    if isinstance(raw_queries, list):
        queries.extend(str(q).strip() for q in raw_queries if str(q).strip())
    page_context_queries = []
    raw_page_queries = plan.get("page_context_queries", [])
    if isinstance(raw_page_queries, list):
        for item in raw_page_queries:
            if not isinstance(item, dict):
                continue
            path_hint = str(item.get("path_hint", "")).strip()
            query = str(item.get("query", "")).strip()
            if path_hint and query:
                page_context_queries.append({"path_hint": path_hint, "query": query})
    effect_source_queries = []
    raw_effect_queries = plan.get("effect_source_queries", [])
    if isinstance(raw_effect_queries, list):
        for item in raw_effect_queries:
            if not isinstance(item, dict):
                continue
            effect_query = str(item.get("effect_query", "")).strip()
            source_type = normalize_source_type(str(item.get("source_type", "")).strip())
            scope = str(item.get("scope", "")).strip()
            if effect_query:
                effect_source_queries.append(
                    {"effect_query": effect_query, "source_type": source_type, "scope": scope}
                )
    achievement_queries = []
    raw_achievement_queries = plan.get("achievement_queries", [])
    if isinstance(raw_achievement_queries, list):
        achievement_queries.extend(str(q).strip() for q in raw_achievement_queries if str(q).strip())
    mission_queries = []
    raw_mission_queries = plan.get("mission_queries", [])
    if isinstance(raw_mission_queries, list):
        for item in raw_mission_queries:
            if isinstance(item, dict):
                query = str(item.get("query", "")).strip()
                scope = str(item.get("scope", "")).strip()
                if query:
                    mission_queries.append({"query": query, "scope": scope})
            else:
                query = str(item).strip()
                if query:
                    mission_queries.append({"query": query, "scope": ""})
    normalized = {
        "intent": str(plan.get("intent", "")).strip(),
        "entities": [str(e).strip() for e in plan.get("entities", []) if str(e).strip()] if isinstance(plan.get("entities"), list) else [],
        "queries": list(dict.fromkeys(queries))[:MAX_PLANNED_SEARCHES],
        "achievement_queries": list(dict.fromkeys(achievement_queries))[:MAX_PLANNED_ACHIEVEMENT_SEARCHES],
        "mission_queries": mission_queries[:MAX_PLANNED_MISSION_SEARCHES],
        "effect_source_queries": effect_source_queries[:MAX_PLANNED_EFFECT_SEARCHES],
        "page_context_queries": page_context_queries[:MAX_PLANNED_PAGE_SEARCHES],
    }
    return enrich_search_plan(normalized, question)


def normalize_source_type(source_type: str) -> str:
    source_type = source_type.lower().strip()
    aliases = {
        "events": "event",
        "事件": "event",
        "missions": "mission",
        "任务": "mission",
        "decisions": "decision",
        "决议": "decision",
        "ideas": "idea",
        "理念": "idea",
        "policies": "policy",
        "政策": "policy",
        "reforms": "reform",
        "改革": "reform",
        "religions": "religion",
        "宗教": "religion",
        "信仰": "religion",
        "estates": "estate",
        "阶层": "estate",
        "特权": "estate",
        "great_projects": "great_project",
        "great project": "great_project",
        "伟大工程": "great_project",
        "奇观": "great_project",
        "modifiers": "modifier",
        "修正": "modifier",
        "traits": "trait",
        "trait": "trait",
        "特质": "trait",
        "特性": "trait",
    }
    source_type = aliases.get(source_type, source_type)
    allowed = {"event", "mission", "decision", "idea", "policy", "reform", "religion", "estate", "great_project", "modifier", "trait"}
    return source_type if source_type in allowed else ""


def enrich_search_plan(plan: dict, question: str) -> dict:
    text = " ".join(
        [question, plan.get("intent", "")]
        + plan.get("entities", [])
        + plan.get("queries", [])
    )
    queries = list(plan.get("queries", []))
    page_queries = list(plan.get("page_context_queries", []))
    effect_queries = list(plan.get("effect_source_queries", []))
    achievement_queries = list(plan.get("achievement_queries", []))
    mission_queries = list(plan.get("mission_queries", []))
    local_hints = infer_local_search_hints(question)
    if local_hints["achievement_queries"]:
        achievement_queries = local_hints["achievement_queries"] + achievement_queries
        queries = [q for q in queries if "\u6210\u5c31" not in q and "achievement" not in q.lower()]
    mission_queries = local_hints["mission_queries"] + mission_queries
    effect_queries = local_hints["effect_source_queries"] + effect_queries
    page_queries = local_hints["page_context_queries"] + page_queries
    queries = local_hints["queries"] + queries
    if local_hints["mission_queries"]:
        queries = [q for q in queries if not is_mission_query(str(q))]

    if any(term in text for term in ("玛丽", "坠马", "勃艮第女公爵去世")):
        queries = [
            "坠马 勃艮第女公爵去世",
            "勃艮第继承危机 incidents_bur_inheritance",
            "玛丽 骑马 勃艮第女公爵去世",
        ] + queries
        page_queries = [
            {"path_hint": "勃艮第继承危机.html", "query": "勃艮第女公爵去世"},
            {"path_hint": "勃艮第继承危机.html", "query": "incidents_bur_inheritance"},
            {"path_hint": "勃艮第继承危机.html", "query": "玛丽"},
        ] + page_queries

    if any(term in text for term in ("萨卢佐", "Saluzzo")) and any(term in text for term in ("AE", "ae", "侵略扩张")):
        queries = ["萨卢佐 意大利小国任务 侵略扩张", "意大利小国任务 AE"] + queries
        effect_queries = [
            {"effect_query": "侵略扩张影响 AE aggressive_expansion_impact", "source_type": "mission", "scope": "萨卢佐 意大利小国任务"},
        ] + effect_queries
        page_queries = [
            {"path_hint": "意大利小国任务.html", "query": "侵略扩张"},
            {"path_hint": "意大利小国任务.html", "query": "篡夺控制权"},
        ] + page_queries

    if (
        any(term in question for term in ("减少", "降低", "减"))
        and any(term in question for term in ("AE", "ae", "侵略扩张"))
        and any(term in question for term in ("事件", "控制台", "event"))
    ):
        effect_queries = [
            {"effect_query": "减少 侵略扩张影响 AE aggressive_expansion_impact", "source_type": "event", "scope": ""}
        ] + effect_queries

    if "成就" in text or "achievement" in text.lower():
        achievement_queries = [question] + achievement_queries
        queries = [q for q in queries if "成就" not in q and "achievement" not in q.lower()]

    trait_requested = any(term in text for term in ("特质", "特性", "谨慎", "权重", "统治者", "领袖", "继承人", "配偶", "将领")) or "trait" in text.lower()
    if trait_requested:
        scope = infer_trait_scope(text)
        effect_queries = [
            {"effect_query": infer_trait_query(text), "source_type": "trait", "scope": scope}
        ] + effect_queries
        queries = [
            q
            for q in queries
            if not any(term in q for term in ("特质", "特性", "谨慎", "权重", "统治者", "领袖", "继承人", "配偶", "将领"))
            and "trait" not in q.lower()
        ]

    effect_lookup_terms = (
        "有什么",
        "哪些",
        "哪个",
        "给",
        "获得",
        "减少",
        "增加",
        "事件",
        "任务",
        "决议",
        "理念",
        "政策",
        "改革",
        "宗教",
        "信仰",
        "特权",
        "奇观",
        "修正",
        "特质",
        "特性",
    )
    effect_keywords = (
        "AE",
        "ae",
        "侵略扩张",
        "外交声誉",
        "改善关系",
        "传教强度",
        "行政效率",
        "造核",
        "稳定",
        "战争分数",
        "最大专制度",
        "自治度",
        "顾问花费",
        "权重",
        "外交技能",
        "行政技能",
        "军事技能",
        "特质",
        "特性",
    )
    if not trait_requested and any(term in text for term in effect_lookup_terms) and any(term in text for term in effect_keywords):
        inferred_type = ""
        type_hints = [
            ("event", ("事件", "event")),
            ("mission", ("任务", "mission")),
            ("decision", ("决议", "decision")),
            ("idea", ("理念", "idea")),
            ("policy", ("政策", "policy")),
            ("reform", ("改革", "reform")),
            ("religion", ("宗教", "信仰", "religion")),
            ("estate", ("阶层", "特权", "estate")),
            ("great_project", ("奇观", "伟大工程", "great project")),
            ("modifier", ("修正", "modifier")),
            ("trait", ("特质", "特性", "trait")),
        ]
        for candidate, hints in type_hints:
            if any(hint.lower() in text.lower() for hint in hints):
                inferred_type = candidate
                break
        effect_queries = [
            {"effect_query": infer_effect_query(text), "source_type": inferred_type, "scope": infer_scope_query(text)}
        ] + effect_queries

    if local_hints["effect_source_queries"] or local_hints["mission_queries"] or local_hints["achievement_queries"]:
        preserved_queries = set(local_hints.get("queries", []))
        queries = [q for q in queries if q in preserved_queries]

    plan["queries"] = list(dict.fromkeys(q for q in queries if q))[:MAX_PLANNED_SEARCHES]
    plan["achievement_queries"] = list(dict.fromkeys(q for q in achievement_queries if q))[:MAX_PLANNED_ACHIEVEMENT_SEARCHES]
    deduped_mission_queries = []
    seen_missions = set()
    for item in mission_queries:
        if isinstance(item, dict):
            query = str(item.get("query", "")).strip()
            scope = str(item.get("scope", "")).strip()
        else:
            query = str(item).strip()
            scope = ""
        if not query:
            continue
        key = (query, scope)
        if key in seen_missions:
            continue
        seen_missions.add(key)
        deduped_mission_queries.append({"query": query, "scope": scope})
    plan["mission_queries"] = deduped_mission_queries[:MAX_PLANNED_MISSION_SEARCHES]
    deduped_effect_queries = []
    seen_effects = set()
    scoped_effect_keys = {
        (str(item.get("effect_query", "")).strip(), normalize_source_type(str(item.get("source_type", "")).strip()))
        for item in effect_queries
        if isinstance(item, dict) and str(item.get("scope", "")).strip()
    }
    for item in effect_queries:
        effect_query = str(item.get("effect_query", "")).strip()
        source_type = normalize_source_type(str(item.get("source_type", "")).strip())
        scope = str(item.get("scope", "")).strip()
        if not effect_query:
            continue
        if not scope and (effect_query, source_type) in scoped_effect_keys:
            continue
        key = (effect_query, source_type, scope)
        if key in seen_effects:
            continue
        seen_effects.add(key)
        deduped_effect_queries.append({"effect_query": effect_query, "source_type": source_type, "scope": scope})
    plan["effect_source_queries"] = deduped_effect_queries[:MAX_PLANNED_EFFECT_SEARCHES]
    deduped_page_queries = []
    seen = set()
    for item in page_queries:
        key = (item.get("path_hint", ""), item.get("query", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped_page_queries.append(item)
    plan["page_context_queries"] = deduped_page_queries[:MAX_PLANNED_PAGE_SEARCHES]
    return plan


def infer_effect_query(text: str) -> str:
    prefix = ""
    if any(term in text for term in ("减少", "降低", "减")) or any(
        term in text.lower() for term in ("reduce", "lower", "decrease")
    ):
        prefix = "减少 "
    elif any(term in text for term in ("增加", "提高", "加")) or any(
        term in text.lower() for term in ("increase", "higher", "add")
    ):
        prefix = "增加 "
    mapping = [
        (("AE", "ae", "侵略扩张"), "侵略扩张影响 AE aggressive_expansion_impact"),
        (("外交声誉",), "外交声誉 diplomatic reputation diplomatic_reputation"),
        (("改善关系",), "改善关系 improve_relation_modifier"),
        (("传教强度",), "传教强度 missionary strength global_missionary_strength"),
        (("行政效率",), "行政效率 administrative_efficiency"),
        (("造核",), "造核 core creation core_creation_cost"),
        (("稳定",), "稳定 stability stability_cost_modifier"),
        (("战争分数",), "战争分数花费 warscore cost"),
        (("最大专制度",), "最大专制度 max absolutism"),
        (("自治度",), "自治度 local autonomy"),
        (("顾问花费",), "顾问花费 advisor cost"),
    ]
    for keys, value in mapping:
        if any(key in text for key in keys):
            return prefix + value
    return (prefix + text)[:120]


def infer_trait_query(text: str) -> str:
    terms = ["特质"]
    for term in (
        "谨慎",
        "Careful",
        "careful",
        "权重",
        "外交技能",
        "行政技能",
        "军事技能",
        "侵略扩张",
        "AE",
        "改善关系",
        "外交声誉",
        "稳定",
    ):
        if term in text:
            terms.append(term)
    return " ".join(dict.fromkeys(terms))


def infer_trait_scope(text: str) -> str:
    scopes = []
    for label, value in (
        ("统治者", "ruler"),
        ("领袖", "ruler"),
        ("君主", "ruler"),
        ("继承人", "heir"),
        ("配偶", "consort"),
        ("将领", "general"),
        ("海军提督", "admiral"),
        ("AI", "ai"),
    ):
        if label in text:
            scopes.append(value)
    if not scopes and any(term in text for term in ("谨慎", "Careful", "careful")):
        scopes.append("ruler")
    return " ".join(dict.fromkeys(scopes))


def infer_trait_query(text: str) -> str:
    terms = ["\u7279\u8d28"]
    terms.extend(entity_source_terms(text, ("trait",), 5))
    for term in (
        "\u8c28\u614e",
        "\u5916\u4ea4\u5bb6",
        "\u6743\u91cd",
        "\u6982\u7387",
        "\u5916\u4ea4\u6280\u80fd",
        "\u884c\u653f\u6280\u80fd",
        "\u519b\u4e8b\u6280\u80fd",
        "Careful",
        "careful",
        "Diplomat",
        "diplomat",
        "trait",
    ):
        if term in text and term not in terms:
            terms.append(term)
    return " ".join(dict.fromkeys(t for t in terms if t))


def infer_scope_query(text: str) -> str:
    scopes = []
    for term in ("萨卢佐", "奥地利", "影响理念", "天主教", "神罗", "意大利小国任务"):
        if term in text:
            scopes.append(term)
    return " ".join(scopes)


def fallback_search_plan(question: str) -> dict:
    return enrich_search_plan(
        {"intent": "", "entities": [], "queries": [question], "achievement_queries": [], "mission_queries": [], "effect_source_queries": [], "page_context_queries": []},
        question,
    )


def execute_search_plan(plan: dict, retrieved: list[dict], default_limit: int) -> tuple[int, str | None]:
    searches_used = 0
    final_error = None
    executed_missions: set[tuple[str, str]] = set()
    for query in plan.get("achievement_queries", [])[:MAX_PLANNED_ACHIEVEMENT_SEARCHES]:
        if searches_used >= MAX_PLANNED_TOTAL_SEARCHES:
            break
        results, error = search_achievements(str(query), max(3, min(default_limit, 8)))
        final_error = error or final_error
        append_unique_results(retrieved, results)
        searches_used += 1

    for item in plan.get("mission_queries", [])[:MAX_PLANNED_MISSION_SEARCHES]:
        if searches_used >= MAX_PLANNED_TOTAL_SEARCHES:
            break
        if isinstance(item, dict):
            query = str(item.get("query", "")).strip()
            scope = str(item.get("scope", "")).strip()
        else:
            query = str(item).strip()
            scope = ""
        if not query:
            continue
        executed_missions.add((query, scope))
        results, error = search_mission_sources(query, scope, max(3, min(default_limit, 8)))
        final_error = error or final_error
        append_unique_results(retrieved, results)
        searches_used += 1

    mission_plan_queries = [
        q
        for q in plan.get("queries", [])
        if is_mission_query(str(q))
    ]
    if is_mission_query(str(plan.get("intent", ""))):
        mission_plan_queries.append(str(plan.get("intent", "")))
    for query in list(dict.fromkeys(mission_plan_queries))[:2]:
        if searches_used >= MAX_PLANNED_TOTAL_SEARCHES:
            break
        if (str(query), "") in executed_missions:
            continue
        results, error = search_mission_sources(str(query), "", max(3, min(default_limit, 8)))
        final_error = error or final_error
        append_unique_results(retrieved, results)
        searches_used += 1

    for item in plan.get("effect_source_queries", [])[:MAX_PLANNED_EFFECT_SEARCHES]:
        if searches_used >= MAX_PLANNED_TOTAL_SEARCHES:
            break
        effect_query = str(item.get("effect_query", "")).strip()
        source_type = normalize_source_type(str(item.get("source_type", "")).strip())
        scope = str(item.get("scope", "")).strip()
        if not effect_query:
            continue
        results, error = search_effect_sources(effect_query, source_type, scope, max(3, min(default_limit, 8)))
        final_error = error or final_error
        append_unique_results(retrieved, results)
        searches_used += 1

    for query in plan.get("queries", [])[:MAX_PLANNED_SEARCHES]:
        if searches_used >= MAX_PLANNED_TOTAL_SEARCHES:
            break
        results, error = search_index(str(query), max(3, min(default_limit, MAX_TOOL_RESULTS)))
        final_error = error or final_error
        append_unique_results(retrieved, results)
        searches_used += 1

    for item in plan.get("page_context_queries", [])[:MAX_PLANNED_PAGE_SEARCHES]:
        if searches_used >= MAX_PLANNED_TOTAL_SEARCHES:
            break
        path_hint = item.get("path_hint", "")
        query = item.get("query", "")
        candidate_paths = resolve_page_hints(path_hint, retrieved)
        for path in candidate_paths[:2]:
            if searches_used >= MAX_PLANNED_TOTAL_SEARCHES:
                break
            function = {
                "name": "search_page_context",
                "arguments": json.dumps({"path": path, "query": query, "radius": 1800}, ensure_ascii=False),
            }
            result = run_search_page_context_tool(function, retrieved)
            final_error = result.get("error") or final_error
            searches_used += 1
            if result.get("results"):
                break
    return searches_used, final_error


def is_mission_query(query: str) -> bool:
    lowered = query.lower()
    return "任务" in query or "任務" in query or "mission" in lowered or "missions" in lowered


def is_mission_overview_query(query: str) -> bool:
    lowered = query.lower()
    effect_words = (
        "效果",
        "奖励",
        "条件",
        "前置",
        "怎么",
        "如何",
        "减少",
        "增加",
        "获得",
        "ae",
        "侵略扩张",
        "外交",
        "传教",
        "行政",
        "造核",
        "稳定",
        "声誉",
        "花费",
    )
    return is_mission_query(query) and not any(word in lowered for word in effect_words)


def combined_search(query: str, limit: int = 10) -> tuple[list[dict], str | None]:
    query = query.strip()
    if not query:
        return [], "query is required"
    limit = max(1, min(int(limit or 10), 30))
    collected: list[dict] = []
    final_error = None
    local_hints = infer_local_search_hints(query)
    for achievement_query in local_hints.get("achievement_queries", [])[:MAX_PLANNED_ACHIEVEMENT_SEARCHES]:
        achievement_results, achievement_error = search_achievements(str(achievement_query), min(limit, 8))
        final_error = achievement_error or final_error
        append_unique_results(collected, achievement_results)
    for item in local_hints.get("mission_queries", [])[:MAX_PLANNED_MISSION_SEARCHES]:
        mission_results, mission_error = search_mission_sources(
            str(item.get("query", "")),
            str(item.get("scope", "")),
            min(limit, 8),
        )
        final_error = mission_error or final_error
        append_unique_results(collected, mission_results)
    for item in local_hints.get("effect_source_queries", [])[:MAX_PLANNED_EFFECT_SEARCHES]:
        effect_results, effect_error = search_effect_sources(
            str(item.get("effect_query", "")),
            str(item.get("source_type", "")),
            str(item.get("scope", "")),
            min(limit, 8),
        )
        final_error = effect_error or final_error
        append_unique_results(collected, effect_results)
    if collected and (local_hints.get("achievement_queries") or local_hints.get("mission_queries") or local_hints.get("effect_source_queries")):
        return collected[:limit], final_error
    if is_mission_query(query):
        mission_results, mission_error = search_mission_sources(query, "", min(limit, 8))
        final_error = mission_error or final_error
        append_unique_results(collected, mission_results)
        if mission_results and not is_mission_overview_query(query):
            return collected[:limit], final_error
    if "成就" in query or "achievement" in query.lower():
        achievement_results, achievement_error = search_achievements(query, min(limit, 8))
        final_error = achievement_error or final_error
        append_unique_results(collected, achievement_results)
    remaining = max(1, limit - len(collected))
    index_results, index_error = search_index(query, remaining)
    final_error = index_error or final_error
    append_unique_results(collected, index_results)
    return collected[:limit], final_error


def resolve_page_hints(path_hint: str, retrieved: list[dict]) -> list[str]:
    hint = path_hint.strip().replace("\\", "/")
    candidates = []
    hint_map = {
        "incidents_bur_inheritance": ["勃艮第继承危机.html", "Burgundian_events.html", "Burgundian_inheritance.html"],
        "勃艮第继承危机": ["勃艮第继承危机.html", "Burgundian_events.html", "Burgundian_inheritance.html"],
        "burgundian": ["Burgundian_events.html", "Burgundian_inheritance.html", "勃艮第继承危机.html"],
        "console_commands": ["控制台指令.html", "控制台.html"],
    }
    for key, paths in hint_map.items():
        if key.lower() in hint.lower():
            candidates.extend(paths)
    if hint.endswith(".html"):
        candidates.append(hint)
    for item in retrieved:
        path = str(item.get("path", ""))
        title = str(item.get("title", ""))
        if hint and (hint in path or hint in title or Path(hint).stem in title):
            candidates.append(path)
    wiki_root = DEFAULT_WIKI_DIR
    direct = wiki_root / hint
    if not hint.endswith(".html") and direct.with_suffix(".html").exists():
        candidates.append(direct.with_suffix(".html").relative_to(wiki_root).as_posix())
    return list(dict.fromkeys(candidates))


def build_llm_messages(question: str, retrieved: list[dict], history: list[dict[str, str]]) -> list[dict[str, object]]:
    messages = [{"role": "system", "content": read_answering_skill()}]
    for turn in history:
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": turn["assistant"]})
    messages.append(
        {
            "role": "user",
            "content": (
                f"当前问题：{question}\n\n"
                f"{format_retrieved_context(retrieved)}\n\n"
                f"如果用户在问成就条件/路线/难度，优先调用 search_achievements；"
                f"如果用户是在问某种效果来自哪些事件/任务/政策/理念/改革/宗教/特权/奇观/修正，优先调用 search_effect_sources；"
                f"如果这些片段不足以可靠回答，可以调用 search_wiki 继续检索；"
                f"如果已经找到目标页面但缺少表格/奖励/完成条件细节，可以调用 search_page_context 读取该页面内的命中上下文。"
            ),
        }
    )
    return messages


def call_llm(
    question: str,
    retrieved: list[dict],
    history: list[dict[str, str]] | None = None,
) -> tuple[str, str | None, list[dict], int]:
    config = read_llm_config()
    if config.get("_config_error"):
        return "", config["_config_error"], retrieved, 0
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "").rstrip("/")
    model = config.get("model", "gpt-4.1-mini")
    if not api_key:
        return "", f"未配置 LLM API；请填写 {llm_config_path()} 或设置 LLM_API_KEY。已完成检索，但不会生成 AI 回答。", retrieved, 0
    if not base_url:
        base_url = "https://api.openai.com/v1"

    messages = build_llm_messages(question, retrieved, history or [])
    tool_calls_used = 0
    final_error: str | None = None
    empty_answer_retries = 0

    while True:
        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tool_calls_used < MAX_TOOL_CALLS:
            payload["tools"] = AVAILABLE_TOOLS
            payload["tool_choice"] = "auto"

        try:
            message = request_chat_completion(base_url, api_key, payload)
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
            return "", f"LLM 调用失败：{exc}", retrieved, tool_calls_used

        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        if not tool_calls:
            if not str(content).strip() and empty_answer_retries < 1:
                empty_answer_retries += 1
                messages.append(
                    {
                        "role": "user",
                        "content": "上一条响应没有正文。请基于已有检索片段给出最终回答；如果证据不足，请明确说明不确定。",
                    }
                )
                continue
            return str(content), final_error, retrieved, tool_calls_used

        remaining = MAX_TOOL_CALLS - tool_calls_used
        if remaining <= 0:
            messages.append(
                {
                    "role": "user",
                    "content": "检索工具调用次数已达上限。请基于已有检索片段作答；如果仍不足，请明确说明不确定。",
                }
            )
            continue

        executable_calls = tool_calls[:remaining]
        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": executable_calls,
            }
        )
        for tool_call in executable_calls:
            tool_calls_used += 1
            tool_result = run_tool_call(tool_call, retrieved)
            if tool_result.get("error"):
                final_error = str(tool_result["error"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", f"wiki_tool_{tool_calls_used}"),
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
            )

        if tool_calls_used >= MAX_TOOL_CALLS:
            messages.append(
                {
                    "role": "user",
                    "content": "你已经用完最多 3 次检索工具调用。请基于已有片段给出最终回答；若证据仍不足，明确说明不确定。",
                }
            )


def request_chat_completion(base_url: str, api_key: str, payload: dict[str, object]) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        if os.environ.get("LLM_USE_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
            opener = urllib.request.build_opener()
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise urllib.error.URLError(f"HTTP {exc.code}: {detail[:800]}") from exc
    return body["choices"][0]["message"]


def run_tool_call(tool_call: dict, retrieved: list[dict]) -> dict:
    function = tool_call.get("function") or {}
    name = function.get("name")
    if name == "search_wiki":
        return run_search_wiki_tool(function, retrieved)
    if name == "search_page_context":
        return run_search_page_context_tool(function, retrieved)
    if name == "search_effect_sources":
        return run_search_effect_sources_tool(function, retrieved)
    if name == "search_achievements":
        return run_search_achievements_tool(function, retrieved)
    if name == "search_mission_sources":
        return run_search_mission_sources_tool(function, retrieved)
    return {"error": f"unsupported tool: {name}", "results": []}


def parse_tool_arguments(function: dict) -> dict:
    try:
        args = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    return args if isinstance(args, dict) else {}


def run_search_wiki_tool(function: dict, retrieved: list[dict]) -> dict:
    args = parse_tool_arguments(function)
    query = str(args.get("query", "")).strip()
    try:
        limit = int(args.get("limit", MAX_TOOL_RESULTS))
    except (TypeError, ValueError):
        limit = MAX_TOOL_RESULTS
    limit = max(1, min(limit, MAX_TOOL_RESULTS))
    if not query:
        return {"error": "search_wiki requires query", "results": []}

    results, error = search_index(query, limit)
    cited = append_unique_results(retrieved, results)
    return {
        "tool": "search_wiki",
        "query": query,
        "error": error,
        "results": [
            {
                "citation": f"[{index}]",
                "title": item["title"],
                "section": item["section"],
                "path": item["path"],
                "snippet": item["snippet"],
            }
            for index, item in cited
        ],
    }


def run_search_effect_sources_tool(function: dict, retrieved: list[dict]) -> dict:
    args = parse_tool_arguments(function)
    effect_query = str(args.get("effect_query", "")).strip()
    source_type = normalize_source_type(str(args.get("source_type", "")).strip())
    scope = str(args.get("scope", "")).strip()
    try:
        limit = int(args.get("limit", MAX_TOOL_RESULTS))
    except (TypeError, ValueError):
        limit = MAX_TOOL_RESULTS
    limit = max(1, min(limit, 8))
    if not effect_query:
        return {"tool": "search_effect_sources", "error": "search_effect_sources requires effect_query", "results": []}

    results, error = search_effect_sources(effect_query, source_type, scope, limit)
    cited = append_unique_results(retrieved, results)
    return {
        "tool": "search_effect_sources",
        "effect_query": effect_query,
        "source_type": source_type,
        "scope": scope,
        "error": error,
        "results": [
            {
                "citation": f"[{index}]",
                "source_type": item.get("source_type", ""),
                "source_title": item["title"],
                "source_id": item.get("source_id", ""),
                "option_or_section": item.get("option_or_section", ""),
                "conditions": item.get("conditions", ""),
                "effects": item.get("effects", ""),
                "duration": item.get("duration", ""),
                "scope": item.get("scope", ""),
                "path": item["path"],
                "snippet": item["snippet"],
            }
            for index, item in cited
        ],
    }


def run_search_achievements_tool(function: dict, retrieved: list[dict]) -> dict:
    args = parse_tool_arguments(function)
    query = str(args.get("query", "")).strip()
    try:
        limit = int(args.get("limit", MAX_TOOL_RESULTS))
    except (TypeError, ValueError):
        limit = MAX_TOOL_RESULTS
    limit = max(1, min(limit, 8))
    if not query:
        return {"tool": "search_achievements", "error": "search_achievements requires query", "results": []}

    results, error = search_achievements(query, limit)
    cited = append_unique_results(retrieved, results)
    return {
        "tool": "search_achievements",
        "query": query,
        "error": error,
        "results": [
            {
                "citation": f"[{index}]",
                "english_name": item.get("english_name", ""),
                "chinese_name": item.get("chinese_name", item["title"]),
                "description": item.get("description", ""),
                "starting_conditions": item.get("starting_conditions", ""),
                "completion_requirements": item.get("completion_requirements", ""),
                "notes": item.get("notes", ""),
                "dlc": item.get("dlc", ""),
                "version": item.get("version", ""),
                "difficulty": item.get("difficulty", ""),
                "path": item["path"],
                "snippet": item["snippet"],
            }
            for index, item in cited
        ],
    }


def run_search_mission_sources_tool(function: dict, retrieved: list[dict]) -> dict:
    args = parse_tool_arguments(function)
    query = str(args.get("query", "")).strip()
    scope = str(args.get("scope", "")).strip()
    try:
        limit = int(args.get("limit", MAX_TOOL_RESULTS))
    except (TypeError, ValueError):
        limit = MAX_TOOL_RESULTS
    limit = max(1, min(limit, 8))
    if not query:
        return {"tool": "search_mission_sources", "error": "search_mission_sources requires query", "results": []}

    results, error = search_mission_sources(query, scope, limit)
    cited = append_unique_results(retrieved, results)
    return {
        "tool": "search_mission_sources",
        "query": query,
        "scope": scope,
        "error": error,
        "results": [
            {
                "citation": f"[{index}]",
                "mission_title": item.get("mission_title", item["title"]),
                "mission_id": item.get("mission_id", ""),
                "country_or_tree": item.get("country_or_tree", ""),
                "slot_or_section": item.get("slot_or_section", ""),
                "conditions": item.get("conditions", ""),
                "effects": item.get("effects", ""),
                "prerequisites": item.get("prerequisites", ""),
                "version_note": item.get("version_note", ""),
                "path": item["path"],
                "snippet": item["snippet"],
            }
            for index, item in cited
        ],
    }


def run_search_page_context_tool(function: dict, retrieved: list[dict]) -> dict:
    args = parse_tool_arguments(function)
    rel_path = str(args.get("path", "")).strip().replace("\\", "/")
    query = str(args.get("query", "")).strip()
    try:
        radius = int(args.get("radius", 1600))
    except (TypeError, ValueError):
        radius = 1600
    radius = max(400, min(radius, 3000))

    if not rel_path or not query:
        return {"tool": "search_page_context", "error": "path and query are required", "results": []}

    wiki_root = DEFAULT_WIKI_DIR.resolve()
    target = (wiki_root / rel_path).resolve()
    try:
        target.relative_to(wiki_root)
    except ValueError:
        return {"tool": "search_page_context", "path": rel_path, "error": "path must stay inside wiki directory", "results": []}
    if not target.exists() or target.suffix.lower() != ".html":
        return {"tool": "search_page_context", "path": rel_path, "error": "wiki html page not found", "results": []}

    try:
        source = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"tool": "search_page_context", "path": rel_path, "error": str(exc), "results": []}

    content_start = find_content_start(source)
    searchable = source[content_start:]
    terms = page_context_terms(query)
    matches: list[tuple[int, int, int, str]] = []
    lowered = searchable.lower()
    for priority, term in enumerate(terms):
        for match in re.finditer(re.escape(term.lower()), lowered):
            pos = content_start + match.start()
            nearby = source[max(0, pos - 700) : min(len(source), pos + 700)]
            bonus = 0
            if 'class="heading"' in nearby or "eu4box" in nearby:
                bonus += 3
            if "<abbr title=" in nearby or "ID</abbr>" in nearby:
                bonus += 3
            if "mw-headline" in nearby:
                bonus += 1
            matches.append((priority, -bonus, pos, term))
            if len(matches) >= 40:
                break
    if not matches:
        return {"tool": "search_page_context", "path": rel_path, "query": query, "error": None, "results": []}

    contexts = []
    seen_spans: list[tuple[int, int]] = []
    for _, _, pos, term in sorted(matches)[:MAX_PAGE_CONTEXT_MATCHES]:
        start = max(0, pos - radius)
        end = min(len(source), pos + radius)
        if any(abs(start - old_start) < 500 for old_start, _ in seen_spans):
            continue
        seen_spans.append((start, end))
        text = clean_html_fragment(source[start:end])
        if not text:
            continue
        item = {
            "title": Path(rel_path).stem.replace("_", " "),
            "section": f"页面内搜索：{term}",
            "snippet": compact(text, MAX_PAGE_CONTEXT_CHARS),
            "path": rel_path,
            "score": -1.0,
            "url": target.as_uri(),
        }
        contexts.append(item)

    cited = append_unique_results(retrieved, contexts)
    return {
        "tool": "search_page_context",
        "path": rel_path,
        "query": query,
        "error": None,
        "results": [
            {
                "citation": f"[{index}]",
                "title": item["title"],
                "section": item["section"],
                "path": item["path"],
                "snippet": item["snippet"],
            }
            for index, item in cited
        ],
    }


def page_context_terms(query: str) -> list[str]:
    terms = []
    raw_parts = [query]
    raw_parts.extend(query.replace("/", " ").replace("|", " ").split())
    for part in raw_parts:
        part = part.strip()
        if len(part) >= 2:
            terms.append(part)
    if "AE" in query or "ae" in query:
        terms.extend(["侵略扩张", "Aggressive_expansion", "Aggressive expansion"])
    if "侵略扩张" in query:
        terms.extend(["Ae impact", "Aggressive_expansion", "AE"])
    return list(dict.fromkeys(terms))


def find_content_start(source: str) -> int:
    markers = [
        "liberty-content-main",
        'id="mw-content-text"',
        'id="bodyContent"',
        '<h1',
    ]
    positions = [source.find(marker) for marker in markers]
    positions = [pos for pos in positions if pos >= 0]
    return min(positions) if positions else 0


def clean_html_fragment(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", fragment)
    fragment = re.sub(r'(?is)<abbr\s+title=["\']([^"\']+)["\'][^>]*>\s*ID\s*</abbr>', r"ID \1", fragment)
    fragment = re.sub(r'(?is)<span\s+id=["\']([A-Za-z0-9_.:-]+)["\'][^>]*>\s*</span>', r"ID \1 ", fragment)
    fragment = re.sub(r"(?i)</(td|th|tr|li|p|div|h[1-6]|table)>", "\n", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    text = unescape(text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "EU4WikiKB/0.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return
        if parsed.path in {"/assets/rcl-bg.webp", "/assets/rcl-logo.svg", "/assets/eu4-bg.png", "/assets/eu4-bg-loop.mp4", "/assets/eu4-bg-loop-pingpong.mp4"}:
            self.send_asset(parsed.path.removeprefix("/assets/"))
            return
        if parsed.path == "/api/search":
            params = urllib.parse.parse_qs(parsed.query)
            results, error = combined_search(params.get("q", [""])[0], int(params.get("limit", [10])[0]))
            self.send_json({"results": results, "error": error})
            return
        if parsed.path == "/api/stats":
            self.send_json(read_stats())
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/api/ask":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"error": "invalid JSON"}, status=400)
            return
        question = str(payload.get("question", "")).strip()
        limit = int(payload.get("limit", 8))
        history = normalize_history(payload.get("history", []))
        plan, plan_error = plan_search(question, history)
        retrieved: list[dict] = []
        planned_searches, plan_exec_error = execute_search_plan(plan, retrieved, limit)
        if not retrieved:
            retrieved, error = combined_search(question, limit)
        else:
            error = None
        if error:
            self.send_json({"answer": "", "citations": [], "retrieved": retrieved, "error": error}, status=400)
            return
        llm_answer, llm_error, all_retrieved, tool_calls_used = call_llm(question, retrieved, history)
        answer = llm_answer or llm_error or plan_error or "没有生成回答。"
        self.send_json(
            {
                "answer": answer,
                "citations": all_retrieved[: min(8, len(all_retrieved))],
                "retrieved": all_retrieved,
                "search_plan": plan,
                "planned_searches": planned_searches,
                "tool_calls": tool_calls_used,
                "error": llm_error,
            }
        )

    def send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_asset(self, name: str) -> None:
        path = (ROOT / "assets" / name).resolve()
        assets_dir = (ROOT / "assets").resolve()
        if assets_dir not in path.parents or not path.exists() or not path.is_file():
            self.send_error(404)
            return
        content_types = {
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".mp4": "video/mp4",
        }
        content_type = content_types.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", content_type)
        if path.suffix.lower() == ".mp4":
            self.send_header("cache-control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("pragma", "no-cache")
        else:
            self.send_header("cache-control", "public, max-age=3600")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def read_stats() -> dict:
    path = db_path()
    if not path.exists():
        return {"ready": False, "error": f"index database not found: {path}"}
    con = sqlite3.connect(path)
    try:
        stats = {
            "ready": True,
            "db": str(path),
            "candidate_pages": get_stat(con, "candidate_pages") or 0,
            "indexed_pages": get_stat(con, "indexed_pages") or 0,
            "indexed_chunks": get_stat(con, "indexed_chunks") or 0,
            "coverage": get_stat(con, "coverage") or 0,
            "tokenizer": get_stat(con, "tokenizer"),
            "built_at": get_stat(con, "built_at"),
            "failures": get_stat(con, "failures") or [],
            "llm": llm_config_status(),
            "answering_skill": {
                "path": str(answering_skill_path()),
                "exists": answering_skill_path().exists(),
            },
        }
    finally:
        con.close()
    effect_path = effect_db_path()
    effect_stats = {
        "ready": False,
        "db": str(effect_path),
        "indexed_sources": 0,
        "indexed_pages": 0,
        "candidate_pages": 0,
        "type_counts": {},
        "built_at": None,
    }
    if effect_path.exists():
        effect_con = sqlite3.connect(effect_path)
        try:
            effect_stats.update(
                {
                    "ready": True,
                    "candidate_pages": get_stat(effect_con, "candidate_pages") or 0,
                    "indexed_pages": get_stat(effect_con, "indexed_pages") or 0,
                    "indexed_sources": get_stat(effect_con, "indexed_sources") or 0,
                    "type_counts": get_stat(effect_con, "type_counts") or {},
                    "built_at": get_stat(effect_con, "built_at"),
                }
            )
        finally:
            effect_con.close()
    stats["effect_sources"] = effect_stats
    mission_path = mission_db_path()
    mission_stats = {
        "ready": False,
        "db": str(mission_path),
        "indexed_missions": 0,
        "indexed_pages": 0,
        "candidate_pages": 0,
        "coverage": 0,
        "built_at": None,
    }
    if mission_path.exists():
        mission_con = sqlite3.connect(mission_path)
        try:
            mission_stats.update(
                {
                    "ready": True,
                    "candidate_pages": get_stat(mission_con, "candidate_pages") or 0,
                    "indexed_pages": get_stat(mission_con, "indexed_pages") or 0,
                    "indexed_missions": get_stat(mission_con, "indexed_missions") or 0,
                    "coverage": get_stat(mission_con, "coverage") or 0,
                    "built_at": get_stat(mission_con, "built_at"),
                }
            )
        finally:
            mission_con.close()
    stats["mission_sources"] = mission_stats
    achievement_path = achievement_db_path()
    achievement_stats = {
        "ready": False,
        "db": str(achievement_path),
        "indexed_achievements": 0,
        "built_at": None,
    }
    if achievement_path.exists():
        achievement_con = sqlite3.connect(achievement_path)
        try:
            achievement_stats.update(
                {
                    "ready": True,
                    "indexed_achievements": get_stat(achievement_con, "indexed_achievements") or 0,
                    "built_at": get_stat(achievement_con, "built_at"),
                    "source_page": get_stat(achievement_con, "source_page"),
                }
            )
        finally:
            achievement_con.close()
    stats["achievements"] = achievement_stats
    return stats


def main() -> int:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"EU4 Wiki KB running at http://{host}:{port}/")
    print(f"Using index: {db_path()}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
