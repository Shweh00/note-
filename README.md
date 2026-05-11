# Handwriting OCR Obsidian

本地 CLI 工具：把输入文件夹里的手写图片识别成文字，并生成 Obsidian 可直接阅读的 Markdown。默认 `mock` OCR 不需要网络、API key 或外部付费凭据，适合先跑通批处理、监听、去重和 Markdown 模板；需要真实在线识别时可配置 OpenAI 图片 OCR。

## 安装

```bash
cd /workspace/project
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

不安装也可以从源码运行：

```bash
PYTHONPATH=src python -m handwriting_ocr --help
```

## 初始化

推荐把配置放进 Obsidian vault：

```bash
handwriting-ocr init --vault ~/ObsidianVault
```

这会创建：

- `Inbox/HandwritingImages/`
- `Inbox/HandwritingNotes/`
- `Inbox/HandwritingImages/processed/`
- `Inbox/HandwritingImages/error/`
- `.handwriting-ocr/config.yaml`
- `.handwriting-ocr/state.sqlite`

也可以复制仓库里的示例：

```bash
cp config.example.yaml config.yaml
```

## Mock OCR

默认配置：

```yaml
ocr:
  mode: "mock"
  provider: "mock"
```

把图片和同名 `.txt` 放入输入目录，例如 `page.png` 和 `page.txt`。批处理会把 `page.txt` 的内容作为识别结果写入 Markdown；没有 sidecar 时写入 `fallback_text`。这个流程完全离线。

## 常用命令

```bash
handwriting-ocr batch --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr watch --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr status --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr retry-failed --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr doctor --config ~/ObsidianVault/.handwriting-ocr/config.yaml
```

`batch` 处理现有图片；`watch` 持续轮询新增图片，并在文件大小和 mtime 连续稳定后才处理，按 `Ctrl+C` 停止；`status` 显示 SQLite 中的成功、失败、重复和最近失败；`retry-failed` 重新处理失败记录；`doctor` 检查目录、SQLite 和 OCR 凭据。

## Obsidian 输出

每张图片生成一篇 Markdown，包含 YAML frontmatter、Obsidian 图片嵌入、识别正文、可能不确定内容、原始 OCR 和处理信息。默认状态是 `to-review`，方便人工校对。

输出示例片段：

```markdown
---
title: "手写识别 - meeting-notes"
source_hash: "sha256:..."
ocr_provider: "mock"
status: "to-review"
tags:
  - handwriting
  - ocr
  - to-review
---

![[../HandwritingImages/processed/meeting-notes.jpg]]
```

### 按日期目录归档

默认保持 V1 行为：Markdown 直接写入 `watch.output_dir`。开启后，新笔记会写入 `watch.output_dir/YYYY/MM/DD/` 这类日期目录，SQLite 的 `output_path` 记录最终 Markdown 路径。

```yaml
markdown:
  filename_template: "{{date}}-{{source_basename}}.md"
  date_folder:
    enabled: true
    pattern: "YYYY/MM/DD"
    date_source: "processed_at"  # processed_at | source_mtime | source_name
```

`pattern` 支持 `YYYY`、`MM`、`DD` 和 `/`、`-`、`_`。`source_name` 会从文件名里的 `YYYY-MM-DD`、`YYYY_MM_DD` 或 `YYYYMMDD` 解析日期，解析失败时回退到处理时间。

### Markdown 模板

默认模板保留 V1 的 frontmatter、标题、来源图片、识别正文、原始 OCR 和处理信息。需要自定义结构时可指定 UTF-8 模板文件：

```yaml
markdown:
  template:
    mode: "file"
    file_path: "handwriting-note-template.md"
    missing_behavior: "fallback"  # fallback | fail
```

模板变量使用受限 `{{variable}}` 替换，不执行表达式。支持变量：`title`、`created_at`、`date`、`source_basename`、`source_image`、`source_hash`、`ocr_mode`、`ocr_provider`、`ocr_model`、`language`、`status`、`tags_yaml`、`recognized_markdown`、`uncertain_items`、`raw_ocr`、`processing_info`。

示例：

````markdown
---
title: "{{title}}"
created: {{created_at}}
status: "{{status}}"
tags:
{{tags_yaml}}
---

# {{title}}

![[{{source_image}}]]

## 识别正文

{{recognized_markdown}}

## 原始 OCR

```text
{{raw_ocr}}
```
````

模板缺失或为空时，`fallback` 会使用默认模板继续处理并输出 warning；`fail` 会让本次图片处理失败，不生成空 Markdown。模板里的未知变量会原样保留并输出 warning。

### 自动索引页

索引默认关闭。开启后，每次成功生成新笔记都会从 SQLite 成功记录重建索引的 managed block；区块外的用户文字会保留。`index.path` 为相对 `watch.output_dir` 的路径，也可以配置绝对路径。

```yaml
index:
  enabled: true
  path: "Index.md"
  title: "手写识别索引"
  grouping: "date"  # date | flat
  sort: "desc"      # desc | asc
  include_status: true
  include_source_link: true
  update_mode: "managed_block"
```

工具只更新以下标记之间的内容：

```markdown
<!-- handwriting-ocr:index:start -->
<!-- handwriting-ocr:index:end -->
```

重复图片被去重时不会新增笔记，也不会新增索引项。索引页不可写时，Markdown 生成仍保持成功，命令输出会包含 index warning。

### V1 配置兼容

旧配置文件不需要手工新增 `markdown.date_folder`、`markdown.template` 或 `index`。这些新功能默认关闭，V1 的输出目录、文件命名、去重、归档、错误处理和 OCR provider 配置继续生效。工具不会迁移、移动、重命名或覆盖已经生成的历史 Markdown；首次开启索引时，会根据 SQLite 里的成功记录生成索引。

## 在线 OCR 与隐私

在线 OCR 需要配置 API key，并会把图片发送到 OpenAI Responses API。敏感内容请使用 `mock` 或本机 `tesseract` provider。基础流程不依赖在线 OCR；使用前建议先运行 `doctor`。

```yaml
ocr:
  mode: "online"
  provider: "openai"
  model: "gpt-4.1-mini"
  language: "zh-cn,en"
  api_key_env: "OPENAI_API_KEY"
  timeout_seconds: 120
  retry_count: 3
```

```bash
export OPENAI_API_KEY="sk-..."
handwriting-ocr doctor --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr batch --config ~/ObsidianVault/.handwriting-ocr/config.yaml
```

## HEIC 限制

配置允许 `.heic` 扩展名，但当前 MVP 不内置 HEIC 转换依赖。如果系统环境不能读取或转换 HEIC，请先转成 JPG/PNG，或安装本机转换工具后扩展 provider。

## 常见问题

- 目录不可写：运行 `handwriting-ocr doctor --config ...`，它会检查输出、归档、错误目录。
- 重复图片没有生成新笔记：工具按内容 SHA-256 去重，已成功处理的相同内容会记录为 `duplicate`，默认移动到 `processed/`，避免 `batch` 或 `watch` 反复计数。需要保留在输入目录时可把 `dedupe.on_duplicate` 改为 `keep`；此模式只会为同一 `source_path + source_hash` 写入第一条 duplicate 记录，后续轮询不会继续增加 duplicate 计数。
- OCR 失败：查看 `status` 最近失败，然后修复配置或凭据，运行 `retry-failed`。
- Obsidian 看不到图片：确认输出目录和 processed 目录都在同一个 vault 中，Markdown 使用相对 Obsidian embed 链接。

## 测试

```bash
cd /workspace/project
.venv/bin/pytest --cov=handwriting_obsidian --cov-report=term-missing --cov-fail-under=95
```

安装 dev 依赖并激活虚拟环境后，也可以运行：

```bash
pytest --cov=handwriting_obsidian --cov-report=term-missing --cov-fail-under=95
```

无需网络的验收流程：

```bash
tmp=/tmp/hw-vault
rm -rf "$tmp"
handwriting-ocr init --vault "$tmp" --force
# 编辑 "$tmp/.handwriting-ocr/config.yaml"：开启 markdown.date_folder.enabled 和 index.enabled
printf '会议记录\n- 今日待办' > "$tmp/Inbox/HandwritingImages/meeting.txt"
printf 'fake image bytes' > "$tmp/Inbox/HandwritingImages/meeting.png"
handwriting-ocr batch --config "$tmp/.handwriting-ocr/config.yaml"
handwriting-ocr status --config "$tmp/.handwriting-ocr/config.yaml"
```
