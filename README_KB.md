# EU4 Wiki Knowledge Base

本目录新增了一个轻量本地知识库：离线扫描 `wiki/**/*.html` 构建 SQLite FTS5 索引，运行时用本地网页检索片段，并可通过 OpenAI-compatible API 生成带引用回答。

## Build the Index

```powershell
python scripts/build_index.py --wiki-dir wiki --out data/wiki_index.sqlite --min-coverage 0.8
python scripts/build_effect_sources.py --wiki-dir wiki --out data/effect_sources.sqlite
python scripts/build_achievements.py --wiki-dir wiki --out data/achievements.sqlite
```

构建脚本会排除 `File_`、`Special_`、`Talk_`、`Template_`、`User_`、`MediaWiki_` 等维护页，并按正文候选 HTML 的成功抽取数量计算覆盖率。

`build_effect_sources.py` 会额外构建“效果来源反查”索引，覆盖事件、任务、决议、理念、政策、政府改革、宗教/信仰、阶层/特权、伟大工程、修正列表和特质。它生成 `data/effect_sources.sqlite`，供 `search_effect_sources(effect_query, source_type, scope)` 使用。特质查询统一使用 `source_type="trait"`，对象范围放在 `scope`，例如 `ruler`、`heir`、`consort`、`general`。

`build_achievements.py` 会从成就总表抽取一行一个成就，生成 `data/achievements.sqlite`。成就不会默认混进普通机制检索，但当问题包含“成就/achievement”时会走 `search_achievements(query)`。

## Run the Server

```powershell
python app/server.py
```

打开 `http://127.0.0.1:8765/` 使用网页界面。

网页会在浏览器内保留最近 8 轮问答作为上下文，并随下一次“提问”发送给模型。“只搜索”不会写入上下文。

模型回答时可以调用本地检索工具追加检索，最多 3 次。`search_wiki` 检索页面索引，`search_page_context` 在已定位的 wiki HTML 页面内抓取关键词附近上下文，`search_effect_sources` 反查“哪些来源给某种效果”，`search_achievements` 检索成就条件和路线。追加检索结果会合并进最终引用和召回片段列表。

## Optional LLM Settings

推荐把 API 配置固化到本地文件：

```json
{
  "api_key": "你的 API key",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4.1-mini"
}
```

保存为 `config/llm_config.json` 后重启服务即可。这个文件已加入 `.gitignore`，避免误提交密钥。

也可以用环境变量临时覆盖本地配置：

```powershell
$env:LLM_API_KEY="..."
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4.1-mini"
python app/server.py
```

没有 `LLM_API_KEY` 时，搜索功能仍可用，`/api/ask` 会返回可读提示。

## Answering Skill

默认回答约束位于 `skills/eu4_wiki_answering.md`。服务启动后每次调用模型都会读取这个文件作为 system prompt，因此你可以直接编辑它来调整回答风格和规则，无需重建索引。

也可以用环境变量指定其他 skill 文件：

```powershell
$env:ANSWERING_SKILL="E:\\path\\to\\my_skill.md"
python app/server.py
```
