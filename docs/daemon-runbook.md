# 常驻运行手册

本手册说明如何把 `handwriting-ocr watch` 作为本机后台任务运行，让新增到监听目录的手写图片自动生成 Obsidian Markdown。以下示例假设项目安装在 `/workspace/project`，配置文件在 `~/ObsidianVault/.handwriting-ocr/config.yaml`。请按自己的路径替换。

## 运行前检查

1. 创建并安装环境：

   ```bash
   cd /workspace/project
   python -m venv .venv
   . .venv/bin/activate
   pip install -e ".[dev]"
   ```

2. 初始化或确认配置：

   ```bash
   handwriting-ocr init --vault ~/ObsidianVault
   handwriting-ocr doctor --config ~/ObsidianVault/.handwriting-ocr/config.yaml
   ```

3. 先前台试跑一次：

   ```bash
   handwriting-ocr watch --config ~/ObsidianVault/.handwriting-ocr/config.yaml
   ```

   新开一个终端放入测试图片；看到 `processed ... -> ...` 后按 `Ctrl+C` 停止前台进程，再配置后台运行。

## Linux: systemd --user

适合 Linux 桌面或长期登录用户。模板文件：`scripts/templates/systemd/handwriting-ocr.service`。

1. 复制模板：

   ```bash
   mkdir -p ~/.config/systemd/user
   cp /workspace/project/scripts/templates/systemd/handwriting-ocr.service ~/.config/systemd/user/handwriting-ocr.service
   ```

2. 编辑 `~/.config/systemd/user/handwriting-ocr.service`，替换：

   - `__PROJECT_DIR__` 为项目目录，例如 `/workspace/project`
   - `__CONFIG_PATH__` 为配置文件路径，例如 `/home/me/ObsidianVault/.handwriting-ocr/config.yaml`

3. 启用并启动：

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now handwriting-ocr.service
   ```

4. 查看状态和日志：

   ```bash
   systemctl --user status handwriting-ocr.service
   journalctl --user -u handwriting-ocr.service -f
   ```

5. 停止、重启和禁用：

   ```bash
   systemctl --user stop handwriting-ocr.service
   systemctl --user restart handwriting-ocr.service
   systemctl --user disable handwriting-ocr.service
   ```

如需开机后未登录也运行，可在有权限时执行 `loginctl enable-linger "$USER"`。

## macOS: launchd

适合 macOS 登录后自动运行。模板文件：`scripts/templates/launchd/com.example.handwriting-ocr.plist`。

1. 准备日志目录：

   ```bash
   mkdir -p ~/Library/Logs/handwriting-ocr
   ```

2. 复制模板：

   ```bash
   cp /workspace/project/scripts/templates/launchd/com.example.handwriting-ocr.plist ~/Library/LaunchAgents/com.example.handwriting-ocr.plist
   ```

3. 编辑 `~/Library/LaunchAgents/com.example.handwriting-ocr.plist`，替换：

   - `__PROJECT_DIR__` 为项目目录
   - `__CONFIG_PATH__` 为配置文件路径
   - `__LOG_DIR__` 为日志目录，例如 `/Users/me/Library/Logs/handwriting-ocr`
   - 如不用仓库里的 `.venv`，把 `ProgramArguments` 中的 Python 路径改成实际虚拟环境路径

4. 加载并启动：

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.handwriting-ocr.plist
   launchctl kickstart -k gui/$(id -u)/com.example.handwriting-ocr
   ```

5. 查看状态和日志：

   ```bash
   launchctl print gui/$(id -u)/com.example.handwriting-ocr
   tail -f ~/Library/Logs/handwriting-ocr/watch.log
   tail -f ~/Library/Logs/handwriting-ocr/watch.err.log
   ```

6. 停止、重启和卸载：

   ```bash
   launchctl bootout gui/$(id -u)/com.example.handwriting-ocr
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.handwriting-ocr.plist
   launchctl kickstart -k gui/$(id -u)/com.example.handwriting-ocr
   ```

## Windows: Task Scheduler

适合 Windows 登录后后台运行。模板文件：

- `scripts/templates/windows/handwriting-ocr-watch.ps1`
- `scripts/templates/windows/handwriting-ocr-watch-task.xml`

1. 复制 PowerShell 脚本到固定位置，例如 `C:\Users\me\handwriting-ocr-watch.ps1`。

2. 编辑脚本里的路径：

   - `$ProjectDir`
   - `$ConfigPath`
   - `$Python`
   - `$LogDir`

3. 先手动验证：

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Users\me\handwriting-ocr-watch.ps1
   ```

4. 创建任务：

   ```powershell
   schtasks /Create /TN "Handwriting OCR Watch" /SC ONLOGON /RL LIMITED /F /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\me\handwriting-ocr-watch.ps1"
   schtasks /Run /TN "Handwriting OCR Watch"
   ```

   如果需要 XML 导入，先替换 `handwriting-ocr-watch-task.xml` 里的 `__SCRIPT_PATH__` 和 `__AUTHOR__`，然后运行：

   ```powershell
   schtasks /Create /TN "Handwriting OCR Watch" /XML C:\Users\me\handwriting-ocr-watch-task.xml /F
   ```

5. 查看状态、停止、重启和删除：

   ```powershell
   schtasks /Query /TN "Handwriting OCR Watch" /V /FO LIST
   schtasks /End /TN "Handwriting OCR Watch"
   schtasks /Run /TN "Handwriting OCR Watch"
   schtasks /Delete /TN "Handwriting OCR Watch" /F
   Get-Content "$env:USERPROFILE\AppData\Local\handwriting-ocr\logs\watch.log" -Wait
   Get-Content "$env:USERPROFILE\AppData\Local\handwriting-ocr\logs\watch.err.log" -Wait
   ```

## 日常操作

- 配置变更后，先运行 `handwriting-ocr doctor --config <config>`，再重启后台任务。
- 批量补处理历史图片：停止后台任务，运行 `handwriting-ocr batch --config <config>`，再启动后台任务。
- 查看处理统计：`handwriting-ocr status --config <config>`。
- 修复失败项后重试：`handwriting-ocr retry-failed --config <config>`。
- 日志来源：
  - Linux systemd user: `journalctl --user -u handwriting-ocr.service`
  - macOS launchd: `~/Library/Logs/handwriting-ocr/watch.log` 和 `watch.err.log`
  - Windows: PowerShell 脚本里的 `$LogDir`

## 故障排查

- `doctor` 失败：先修复目录权限、SQLite 路径、OCR provider、Tesseract 语言包或 PaddleOCR 本地模型目录。不要直接启动后台任务。
- 后台服务启动后马上退出：查看 stderr 日志，通常是配置路径不存在、虚拟环境路径错误或缺少依赖。
- 新图片不处理：确认图片扩展名在 `extensions` 中，文件确实写入 `watch.input_dir`，且不是已经按 SHA-256 成功处理过的重复图片。
- 图片被写到 `error/`：运行 `handwriting-ocr status --config <config>` 查看最近失败；修复 OCR 配置后运行 `retry-failed`。
- Obsidian 笔记生成但图片不显示：确认 `watch.output_dir`、`watch.processed_dir` 在同一个 vault 中，且后台任务用户对这些目录有读写权限。
- 在线 OpenAI OCR 无结果：确认 `ocr.mode: "online"`、`ocr.provider: "openai"`，后台任务环境中存在 `OPENAI_API_KEY`。Linux 可写入 systemd `Environment=`，macOS/Windows 推荐在启动脚本或用户环境中显式配置。
