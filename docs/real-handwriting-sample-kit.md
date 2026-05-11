# 真实手写样例集准备指南

本指南用于准备最小真实手写样例集，帮助 api-tester 对 `handwriting-ocr` 的真实 OCR、归档、Markdown 输出和人工校对流程做复验。样例集不提交到仓库；只把脱敏后的图片、清单和参考文本放到本机测试 vault。

## 放置路径

建议使用独立临时 vault，避免把个人 Obsidian 内容混入测试：

```bash
export HW_SAMPLE_VAULT=/tmp/hw-real-sample-vault
cd /workspace/project
.venv/bin/handwriting-ocr init --vault "$HW_SAMPLE_VAULT" --force
mkdir -p "$HW_SAMPLE_VAULT/Inbox/HandwritingImages/real-samples"
mkdir -p "$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/expected"
cp docs/sample-manifest.template.yaml "$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/sample-manifest.yaml"
cp docs/sample.expected.md.template "$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/expected/2026-05-11_001_zh_meeting_clear.expected.md"
```

图片放在：

```text
$HW_SAMPLE_VAULT/Inbox/HandwritingImages/real-samples/
```

清单放在：

```text
$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/sample-manifest.yaml
```

每张图片的参考文本放在：

```text
$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/expected/<sample_id>.expected.md
```

复验前把配置里的 `watch.input_dir` 临时改为绝对路径 `$HW_SAMPLE_VAULT/Inbox/HandwritingImages/real-samples`。如果手动编辑 YAML 时不展开环境变量，也可以写成相对配置文件目录的 `../Inbox/HandwritingImages/real-samples`，因为配置文件位于 `$HW_SAMPLE_VAULT/.handwriting-ocr/config.yaml`。如需隔离归档，也把 `watch.processed_dir` 和 `watch.error_dir` 指向真实样例目录下的 `_processed`、`_errors`；否则可把 18 张图片复制到默认 `Inbox/HandwritingImages/`。为了避免 mock sidecar 影响真实 OCR，真实样例目录内不要放同名 `.txt`。

准备完成后先运行准入校验。校验会检查 `sample-manifest.yaml`、18 张图片、命名规则、18 个 `.expected.md` 文件、manifest 与 expected frontmatter 的 `privacy_checked: true`、图片 SHA-256，以及配置里的 `watch.input_dir` 是否指向样例图片目录：

```bash
handwriting-ocr validate-samples --vault "$HW_SAMPLE_VAULT"
```

如果 manifest 不在默认位置，也可以显式传入：

```bash
handwriting-ocr validate-samples \
  --vault "$HW_SAMPLE_VAULT" \
  --manifest "$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/sample-manifest.yaml"
```

## 隐私脱敏

样例必须先脱敏再进入测试目录：

- 不包含真实姓名、身份证、护照、手机号、邮箱、住址、银行卡、订单号、病历号、公司客户名或未公开项目信息。
- 必须保留版面和笔迹难度时，用假名、假电话、示例地址和虚构金额重写一份再拍照。
- 照片 EXIF/GPS 信息需清除；建议导出为 PNG/JPG 后再放入样例目录。
- 背景中不要出现电脑屏幕、快递面单、证件、家庭照片、门牌号等额外隐私。
- 如需测试涂改、划线、边角阴影，用虚构内容制作，不能用真实敏感原件。

## 命名规则

文件名格式：

```text
YYYY-MM-DD_NNN_<lang>_<scenario>_<quality>.<ext>
```

字段说明：

- `YYYY-MM-DD`：样例创建日期，也可用于 `date_source: source_name` 复验。
- `NNN`：三位序号，从 `001` 到 `018`。
- `lang`：`zh`、`en`、`mixed` 或 `num`。
- `scenario`：单段场景短名，如 `meeting`、`todo`、`math`、`receipt`；不要包含额外下划线。
- `quality`：单段质量短名，如 `clear`、`shadow`、`tilted`、`faint`、`crowded`；不要包含额外下划线。
- `ext`：优先 `jpg`、`jpeg` 或 `png`；HEIC 需先转换。

示例：

```text
2026-05-11_001_zh_meeting_clear.jpg
2026-05-11_012_mixed_recipe_shadow.png
```

`sample_id` 等于去掉扩展名后的文件名。对应参考文本文件必须命名为：

```text
2026-05-11_001_zh_meeting_clear.expected.md
```

## 18 张最小样例

| 序号 | sample_id 后缀 | 最小覆盖点 | 建议内容 |
| --- | --- | --- | --- |
| 001 | `zh_meeting_clear` | 清晰中文横线纸 | 会议纪要、2-3 个项目符号 |
| 002 | `zh_todo_faint` | 浅色笔迹 | 待办清单、日期、优先级 |
| 003 | `zh_diary_tilted` | 轻微倾斜拍摄 | 3-5 行日记式短句 |
| 004 | `zh_vertical_layout` | 中文竖向或分栏 | 竖排词组、短标题 |
| 005 | `en_notes_clear` | 英文手写 | 英文课堂笔记或短段落 |
| 006 | `mixed_bilingual_clear` | 中英混写 | 中文句子夹英文术语 |
| 007 | `num_math_grid` | 数字和符号 | 简单公式、编号、百分比 |
| 008 | `zh_schedule_table` | 手绘表格 | 2-3 列日程表 |
| 009 | `num_receipt_amounts` | 金额、日期 | 虚构消费记录和合计 |
| 010 | `mixed_contact_redacted` | 脱敏联系人格式 | 假姓名、假电话、假邮箱 |
| 011 | `zh_mindmap_arrows` | 箭头和层级 | 简单脑图或流程 |
| 012 | `mixed_recipe_shadow` | 阴影和多行 | 配方、数量、步骤 |
| 013 | `zh_sticky_small` | 小纸张低分辨率 | 便签短句 |
| 014 | `zh_notes_crowded` | 密集排版 | 多行压缩笔记 |
| 015 | `zh_revision_crossed` | 划掉和修改 | 被划掉文本、改写文本 |
| 016 | `zh_contrast_faint` | 蓝色或低对比 | 蓝笔内容、浅背景 |
| 017 | `zh_photo_tilted` | 手机斜拍边角 | 纸张边缘可见、透视变形 |
| 018 | `mixed_pages_marker` | 多页编号语义 | 写明 `Page 1/2` 或连续页标记 |

每个样例至少 20 个可识别字符；极短便签样例可以少一些，但必须在 `sample-manifest.yaml` 里标记 `expected_character_count`。

## 参考文本格式

每张图片维护一个 `.expected.md` 文件。参考文本不是要求 OCR 完全逐字一致，而是给 api-tester 做人工和半自动复验的基准。请从 `docs/sample.expected.md.template` 复制后填写：

- `sample_id`、`image_file`、`language`、`scenario`、`quality_tags` 必须和清单一致。
- `expected_text` 写人工转写的正文，保持原始换行和列表层级。
- `must_include` 写复验时必须出现的关键词、数字、日期或短句。
- `acceptable_variants` 写允许的繁简、大小写、标点或同义变体。
- `ignore_regions` 写不要求识别的涂鸦、页码、水印、折痕阴影等。
- `notes_for_reviewer` 写人工判断规则，例如“划掉内容可不输出”。

## sample-manifest 填写规则

从 `docs/sample-manifest.template.yaml` 复制后，逐项填写：

- `dataset.id`：建议 `real-handwriting-min18-YYYYMMDD`。
- `dataset.owner`：样例维护者或代理名，不写个人真实姓名。
- `dataset.privacy_level`：固定使用 `redacted-local-only`，表示已脱敏且只在本机使用。
- `samples[].image_sha256`：用 `sha256sum` 或 `shasum -a 256` 生成。
- `samples[].expected_file`：相对 manifest 所在目录的路径，例如 `expected/<sample_id>.expected.md`。
- `samples[].review_priority`：`p0` 表示阻塞质量判断，`p1` 表示重要覆盖，`p2` 表示补充覆盖。
- `samples[].privacy_checked`：完成内容重写、背景检查、EXIF/GPS 清除和 expected 文本脱敏后必须改为 `true`。api-tester 复验时发现任何样例缺失该字段或值仍为 `false`，应判为采集包不完整。

生成 hash 示例：

```bash
cd "$HW_SAMPLE_VAULT/Inbox/HandwritingImages/real-samples"
sha256sum 2026-05-11_001_zh_meeting_clear.jpg
```

## api-tester 复验命令

以下命令用于 api-tester 在本机复验样例集准备质量和 CLI 处理链路。在线 OpenAI OCR 会上传图片；只有在确认样例已脱敏且接受联网后才使用 `ocr.mode: online`。离线 Paddle/Tesseract 复验应先运行 `doctor`。

```bash
cd /workspace/project
. .venv/bin/activate
export HW_SAMPLE_VAULT=/tmp/hw-real-sample-vault

handwriting-ocr doctor --config "$HW_SAMPLE_VAULT/.handwriting-ocr/config.yaml"
handwriting-ocr validate-samples --vault "$HW_SAMPLE_VAULT"
handwriting-ocr batch --config "$HW_SAMPLE_VAULT/.handwriting-ocr/config.yaml"
handwriting-ocr status --config "$HW_SAMPLE_VAULT/.handwriting-ocr/config.yaml"
```

`validate-samples` 成功时退出码为 `0` 并输出 `sample validation: PASS`；发现准入问题时退出码为 `1` 并逐条输出 `FAIL <CODE>:`，适合 api-tester 在真实 OCR 前先拒收不完整样例集。模板态 manifest、`privacy_checked: false`、`image_sha256: TODO`、expected 缺失或不足、图片数量不足、`watch.input_dir` 指错都会返回非 `0`。

如果需要手工排查目录数量，也可以运行：

```bash
test -f "$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/sample-manifest.yaml"
find "$HW_SAMPLE_VAULT/Inbox/HandwritingImages/real-samples" \
  -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | wc -l
find "$HW_SAMPLE_VAULT/.handwriting-ocr/real-samples/expected" \
  -maxdepth 1 -type f -name '*.expected.md' | wc -l
```

期望两个 `wc -l` 都返回 `18`。复验时逐张对照生成的 Markdown、manifest 里的 `must_include` 和 `.expected.md` 的 `expected_text`，记录明显漏识别、错行、错数字或隐私未脱敏问题。

## 提交与保留边界

- 仓库只保留本指南和模板，不保留真实图片或人工转写内容。
- 样例集默认保留在本机临时 vault 或受控私有存储。
- 如果需要把样例发给其他代理，先确认接收方只需要脱敏图片、manifest 和 expected 文件，不需要个人原始素材。
