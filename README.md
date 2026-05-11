# Handwriting Obsidian

本地手写图片整理工具：把指定文件夹中的图片识别为文本，并生成适合 Obsidian 阅读的 Markdown 笔记。

基础流程不需要任何外部付费凭据。默认 `mock` OCR 会读取同名 `.txt` sidecar 文件作为识别结果，例如 `note.png` 对应 `note.txt`；没有 sidecar 时会写入配置中的占位文本。之后可以把 OCR provider 换成本机 `tesseract`。

## 安装

```bash
cd /workspace/project
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

也可以不安装，直接从源码目录运行：

```bash
PYTHONPATH=src python -m handwriting_obsidian --help
```

## 配置

复制示例配置：

```bash
cp config.example.toml config.toml
```

关键字段：

- `input_dir`: 新上传手写图片的目录。
- `output_dir`: Markdown 输出目录，建议放在 Obsidian vault 中。
- `assets_dir`: 原图副本目录，生成的 Markdown 会引用这里的图片。
- `state_path`: 去重状态文件，同一图片内容不会重复处理。
- `tags`: 每篇笔记的默认标签。
- `[ocr].provider`: 默认 `mock`，无需服务凭据。

## 批处理已有图片

```bash
handwriting-obsidian batch --config config.toml
```

或：

```bash
python -m handwriting_obsidian batch --config config.toml
```

## 持续监听新图片

```bash
handwriting-obsidian watch --config config.toml
```

`watch` 使用轮询方式，适合本地文件夹、同步盘和 NAS 场景。按 `Ctrl+C` 停止。

## Markdown 输出格式

每张图片生成一篇 Markdown，包含：

- YAML front matter: 标题、创建时间、源文件、图片哈希、标签。
- 原图嵌入：`![](assets/...)`。
- 识别文本。
- 处理时间和原始路径。

示例：

```markdown
---
title: "meeting-notes"
created: "2026-05-11T10:30:00+00:00"
source_image: "/path/to/incoming/meeting-notes.png"
image_hash: "..."
tags:
  - handwriting
  - ocr
---

# meeting-notes

![](assets/meeting-notes-abc12345.png)

## 识别文本

今天的会议重点...
```

## Mock OCR 用法

把图片和同名 `.txt` 放在 `input_dir`：

```text
incoming/
  page-1.png
  page-1.txt
```

运行批处理后，`page-1.txt` 的内容会进入生成的 Markdown。这个流程可用于没有真实 OCR 服务时的开发、测试和整理模板验证。

## 可选：本机 Tesseract OCR

如果机器上已安装 `tesseract`，可在配置中使用：

```toml
[ocr]
provider = "tesseract"
command = "tesseract"
languages = "eng+chi_sim"
```

工具会执行 `tesseract <image> stdout -l <languages>`。没有安装时请继续使用默认 `mock` provider。

## 测试

```bash
cd /workspace/project
python -m pytest
```

没有安装 pytest 时也可以运行标准库测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

测试包含一个不依赖真实 OCR 服务的 mock/stub 流程，覆盖批处理、Markdown 生成、图片复制和去重状态。
