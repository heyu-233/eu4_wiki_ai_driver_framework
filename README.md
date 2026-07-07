# EU4 Wiki AI Driver Framework

一个轻量的本地 EU4 Wiki 知识库框架：离线把本地 wiki HTML 构建成 SQLite FTS 索引，运行时通过结构化检索召回片段，再按 OpenAI-compatible Chat Completions 接口调用大模型生成带引用的回答。

项目目标不是把整份 wiki 塞给模型，而是先用本地工具定位页面、任务、事件、成就、效果来源，再只把 Top K 片段发给模型。

## 功能

- 本地网页问答界面：`http://127.0.0.1:8765/`
- SQLite FTS 页面检索：`data/wiki_index.sqlite`
- 效果来源反查：事件、任务、决议、理念、政策、改革、宗教、阶层、奇观、修正、特质
- 成就专用索引：支持“某国家有哪些专属/相关成就”
- 任务专用索引：支持任务名、完成条件、奖励、前置任务、任务树范围
- 实体注册表：支持国家、任务页、事件、特质、宗教等别名和轻量纠错
- LLM 工具调用：模型最多追加 3 次本地检索
- 本地 skill 文件：`skills/eu4_wiki_answering.md` 作为 system prompt 主体

## 不提交的内容

本仓库只提交框架代码和 UI 资源，不提交本地爬取数据和生成索引：

- `wiki/`
- `images/`
- `data/*.sqlite`
- `config/llm_config.json`
- `logs/`

## 准备 wiki 数据

把爬取好的 EU4 wiki HTML 放到项目根目录的 `wiki/` 下，例如：

```text
wiki/
  Achievements.html
  Riga.html
  里加任务.html
  ...
```

## 构建索引

```powershell
python scripts/build_index.py --wiki-dir wiki --out data/wiki_index.sqlite --min-coverage 0.8
python scripts/build_effect_sources.py --wiki-dir wiki --out data/effect_sources.sqlite
python scripts/build_achievements.py --wiki-dir wiki --out data/achievements.sqlite
python scripts/build_mission_sources.py --wiki-dir wiki --out data/mission_sources.sqlite
python scripts/build_entity_registry.py --out data/entity_registry.sqlite
```

建议按上面的顺序构建。`build_entity_registry.py` 会读取前几个索引，生成别名、实体和纠错入口。

## 配置 LLM

复制示例配置：

```powershell
Copy-Item config/llm_config.example.json config/llm_config.json
```

填写：

```json
{
  "api_key": "replace-with-your-api-key",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4.1-mini"
}
```

也可以用环境变量覆盖：

```powershell
$env:LLM_API_KEY="..."
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="..."
python app/server.py
```

没有配置 API key 时，搜索功能仍然可用，`/api/ask` 会返回可读错误。

## 启动

```powershell
python app/server.py
```

或在 Windows 上双击：

```text
start_eu4_kb.bat
```

默认地址：

```text
http://127.0.0.1:8765/
```

## API

- `GET /`
- `GET /api/search?q=...&limit=10`
- `POST /api/ask`
- `GET /api/stats`

`POST /api/ask` 示例：

```json
{
  "question": "萨卢佐有什么减少 AE 的任务？",
  "limit": 8,
  "history": []
}
```

## 检索结构

运行时大致流程：

```text
用户问题
  -> 实体识别与轻量纠错
  -> 判断问题类型：成就 / 任务 / 效果来源 / 事件指令 / 特质 / 普通页面
  -> 优先结构化检索
  -> 页面全文检索兜底
  -> 将召回片段交给 LLM
  -> 带引用回答
```

示例：

- `迪特马尔生任务有哪些` 会纠错到 `迪特马尔申`
- `萨卢左减少AE任务` 会纠错到 `萨卢佐`
- `胡私教有什么效果` 会纠错到 `胡斯教/胡斯派`
- `玛丽小姐坠马事件` 会定位到勃艮第继承危机事件

## Skill

默认回答规则在：

```text
skills/eu4_wiki_answering.md
```

服务每次调用模型时都会读取该文件。修改 skill 后重启服务即可生效，不需要重建索引。
