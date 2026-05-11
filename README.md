# Handwriting OCR Obsidian

本地 CLI 工具：把输入文件夹里的手写图片识别成文字，并生成 Obsidian 可直接阅读的 Markdown。默认 `mock` OCR 不需要网络、API key 或外部付费凭据，适合先跑通批处理、监听、去重和 Markdown 模板；真实识别可选择本机离线 `tesseract`/`paddle` 或在线 OpenAI 图片 OCR。

## 安装

```bash
cd /workspace/project
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

如果你的系统只提供 `python` 且它指向 Python 3，请把上面的 `python3 -m venv .venv` 替换为 `python -m venv .venv`；在不少 Linux/macOS 干净环境中只有 `python3` 命令。

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

## 本机离线 OCR

### Tesseract

`tesseract` provider 在运行期只调用本机 `tesseract` 命令，不发送图片到网络。它安装简单，适合清晰印刷体或简单图片；中文手写质量取决于系统语言包和图片质量。

```bash
# Ubuntu/Debian 示例
sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng
```

```yaml
ocr:
  mode: "offline"
  provider: "tesseract"
  language: "zh-cn,en"
  command: "tesseract"
  timeout_seconds: 120
  tesseract:
    lang: "chi_sim+eng"  # 省略时由 language 映射
    psm: 6
    oem: 1
    tessdata_dir: ""
```

### PaddleOCR

`paddle` provider 在本机推理，更适合作为中文手写优先方案，但依赖较重。`paddleocr` 不在默认依赖中，可按需安装：

```bash
pip install -e ".[offline-paddle]"
```

隐私边界：PaddleOCR 推理在本机执行，但如果模型未预置，PaddleOCR 可能尝试下载模型。默认 `offline_no_network: true` 且 `allow_model_download: false`，因此必须配置已存在的 PaddleOCR 3.x 显式本地模型目录，至少包括 `text_detection_model_dir` 和 `text_recognition_model_dir`；启用方向分类、文档矫正或文本行方向模块时，还必须配置对应目录。否则 `doctor` 会失败，运行期也不会静默触发下载。

```yaml
ocr:
  mode: "offline"
  provider: "paddle"
  language: "zh-cn,en"
  offline_no_network: true
  paddle:
    engine: "paddle"
    device: "cpu"
    lang: "ch"
    text_detection_model_dir: "/path/to/local/paddle/text_detection"
    text_recognition_model_dir: "/path/to/local/paddle/text_recognition"
    doc_orientation_classify_model_dir: ""
    doc_unwarping_model_dir: ""
    textline_orientation_model_dir: ""
    allow_model_download: false
    use_doc_orientation_classify: false
    use_doc_unwarping: false
    use_textline_orientation: false
```

## 常用命令

```bash
handwriting-ocr batch --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr watch --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr status --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr retry-failed --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr doctor --config ~/ObsidianVault/.handwriting-ocr/config.yaml
```

`batch` 处理现有图片；`watch` 持续轮询新增图片，并在文件大小和 mtime 连续稳定后才处理，按 `Ctrl+C` 停止；`status` 显示 SQLite 中的成功、失败、重复和最近失败；`retry-failed` 重新处理失败记录；`doctor` 检查目录、SQLite、OCR 凭据、本机命令、语言包和离线模型配置。

## 后台常驻与开机自启

把工具长期作为本机后台任务运行时，推荐先前台验证：

```bash
handwriting-ocr doctor --config ~/ObsidianVault/.handwriting-ocr/config.yaml
handwriting-ocr watch --config ~/ObsidianVault/.handwriting-ocr/config.yaml
```

确认新增图片能生成 Markdown 后，再按 [常驻运行手册](docs/daemon-runbook.md) 配置后台服务。手册覆盖：

- Linux `systemd --user`：启动、开机自启、`journalctl` 日志、停止和重启。
- macOS `launchd`：登录启动、日志文件、停止和重启。
- Windows Task Scheduler：登录启动、PowerShell 包装脚本、日志、停止和重启。
- 日常运维：`doctor`、`status`、`retry-failed`、批量补处理和故障排查。

可复制的模板在 `scripts/templates/` 下。后台任务使用和前台相同的配置文件；修改 OCR provider、目录或 API key 后，先运行 `doctor`，再重启服务。
模板和手册示例支持带空格路径；替换 systemd、launchd、Windows 模板变量时请保留原有引号。后台日志以服务管理器捕获的 stdout/stderr 为准：Linux 用 `journalctl --user`，macOS/Windows 使用模板中配置的日志文件；`state.log_path` 目前只是保留配置项，不是 `watch` 的落盘日志。

## 真实手写样例集

需要用真实手写图片复验 OCR 质量时，请按 [真实手写样例集准备指南](docs/real-handwriting-sample-kit.md) 准备脱敏样例。指南包含 18 张最小样例覆盖清单、隐私脱敏要求、命名规则、参考文本格式、放置路径和 api-tester 复验命令。

样例集进入 OCR 复验前先运行准入校验：

```bash
handwriting-ocr validate-samples --vault "$HW_SAMPLE_VAULT"
```

该命令校验 `sample-manifest.yaml`、manifest 登记的 18 张图片、命名规则、18 个 `.expected.md` 文件、`privacy_checked: true`、`image_sha256` 和 `watch.input_dir`。成功退出码为 `0` 并输出 `sample validation: PASS`；发现准入问题时退出码为 `1` 并输出逐条可操作的 `FAIL <CODE>:`。默认准入会拒绝未登记在 manifest 中的额外图片或额外 `.expected.md` 文件。`--format json` 可用于 CI 或 api-tester 自动解析。

可复制模板：

- [sample-manifest.template.yaml](docs/sample-manifest.template.yaml)
- [sample.expected.md.template](docs/sample.expected.md.template)

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

`pattern` 支持 `YYYY`、`MM`、`DD` 和 `/`、`-`、`_`。`date_source` 解析出的日期称为 `note_date`：它会同时用于日期目录、`filename_template` 里的 `{{date}}`、模板变量 `{{date}}`，以及 `index.grouping: "date"` 的分组。`created_at` 继续表示实际处理时间。

`source_name` 会从文件名里的 `YYYY-MM-DD`、`YYYY_MM_DD` 或 `YYYYMMDD` 解析日期；`source_mtime` 会读取源图片修改时间。`source_name` 或 `source_mtime` 解析失败时，命令会输出 `date warning`，并回退到处理时间作为 `note_date`。

### Markdown 模板

默认模板保留 V1 的 frontmatter、标题、来源图片、识别正文、原始 OCR 和处理信息。需要自定义结构时可指定 UTF-8 模板文件：

```yaml
markdown:
  template:
    mode: "file"
    file_path: "handwriting-note-template.md"
    missing_behavior: "fallback"  # fallback | fail
```

模板变量使用受限 `{{variable}}` 替换，不执行表达式。支持变量：`title`、`created_at`、`date`、`source_basename`、`source_image`、`source_hash`、`ocr_mode`、`ocr_provider`、`ocr_model`、`language`、`status`、`tags_yaml`、`recognized_markdown`、`uncertain_items`、`raw_ocr`、`processing_info`。其中 `date` 是 `markdown.date_folder.date_source` 解析出的 `note_date`，`created_at` 是处理时间。

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

重复图片被去重时不会新增笔记，也不会新增索引项。`grouping: "date"` 使用每条笔记持久化在 SQLite 中的 `note_date` 分组；旧记录没有 `note_date` 时会回退到处理时间。索引页不可写时，Markdown 生成仍保持成功，命令输出会包含 index warning。

### V1 配置兼容

旧配置文件不需要手工新增 `markdown.date_folder`、`markdown.template` 或 `index`。这些新功能默认关闭，V1 的输出目录、文件命名、去重、归档、错误处理和 OCR provider 配置继续生效。工具不会迁移、移动、重命名或覆盖已经生成的历史 Markdown；首次开启索引时，会根据 SQLite 里的成功记录生成索引。

## 在线 OCR 与隐私

在线 OCR 需要配置 API key，并会把图片发送到 OpenAI Responses API，不属于离线模式。敏感内容请使用 `mock`、本机 `tesseract` 或已预置模型的本机 `paddle` provider。基础流程不依赖在线 OCR；使用前建议先运行 `doctor`。

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
- Tesseract 缺语言包：`doctor` 会提示缺少 `chi_sim`、`eng` 等 traineddata。安装对应系统包，或调整 `ocr.tesseract.lang`。
- PaddleOCR 提示本地模型目录缺失：默认禁用模型下载。请先在有网络环境中准备本地模型目录，再配置 `ocr.paddle.text_detection_model_dir` 和 `ocr.paddle.text_recognition_model_dir`；启用可选模块时一并配置对应目录。或者明确把 `allow_model_download` 改为 `true` 并接受首次运行可能联网。
- 离线模式误配 OpenAI：`ocr.mode: "offline"` 不能搭配 `provider: "openai"`；改为 `mode: "online"` 或换成本机 provider。
- 重复图片没有生成新笔记：工具按内容 SHA-256 去重，已成功处理的相同内容会记录为 `duplicate`，默认移动到 `processed/`，避免 `batch` 或 `watch` 反复计数。需要保留在输入目录时可把 `dedupe.on_duplicate` 改为 `keep`；此模式只会为同一 `source_path + source_hash` 写入第一条 duplicate 记录，后续轮询不会继续增加 duplicate 计数。
- OCR 失败：查看 `status` 最近失败，然后修复配置或凭据，运行 `retry-failed`。
- Obsidian 看不到图片：确认输出目录和 processed 目录都在同一个 vault 中，Markdown 使用相对 Obsidian embed 链接。

SQLite 只记录路径、hash、状态、provider、model、language 和错误信息，不保存图片二进制。`raw_ocr` 会写入 Markdown，可能包含敏感文本；工具不会额外把它复制到网络服务。

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

预期会在 `"$tmp/Inbox/HandwritingNotes/"` 下生成 `YYYY-MM-DD-meeting.md`，例如当天日期为 2026-05-11 时文件名为 `2026-05-11-meeting.md`。
