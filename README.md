# dsh-session-move

把 [DeepSeek Harness](https://github.com/)（DSH）的会话在**工作区**之间、以及工作区与**任意文件夹**之间搬迁的单文件 Python 脚本。

DSH 自身没有提供「移动会话到别的项目」的功能。工作区的成员身份是**派生**的：只有当会话日志 header 里的 `cwd` 等于工作区记录的 `path` 时，它才算这个工作区的成员——所以搬会话必须停机改文件。这个脚本把整套文件手术做成了交互式工具，并自带备份与自动回滚。

```
会话日志 header 的 cwd  →  决定它属于哪个工作区
```

## 特性

- **三种搬法**：会话 → 工作区 / 会话 → 任意文件夹（可顺手登记为新工作区）/ 整个工作区文件夹 → 另一个文件夹
- **单文件**：`move-sessions.py` 不依赖同目录的其它脚本；帧压缩优先用 Python `zstandard`，没装则自动退回 `node:zlib`
- **最小改写**：会话日志是多帧串联格式、header 独占第 0 帧，脚本只重压第 0 帧，其余几百帧逐字节原样保留（只重算这一帧的校验和）
- **安全**：动手前先备份（workspace.json / projcache / 会话目录），任何一步失败自动整体回滚；整体搬工作区时还会把改过名的文件夹改回原名
- **预演**：`--dry-run` 会把检查、解帧、规划全走一遍，但一个字节都不写

## 环境要求

| 需要 | 说明 |
|---|---|
| Python | 3.8+（实测 3.11） |
| `zstandard` | 推荐 `python -m pip install zstandard`；**没装也能用**，会自动调用 `node.exe` |
| Node.js | 仅在缺少 `zstandard` 时用到（DSH 自带的运行时、`C:\Program Files\nodejs` 都会被自动找到） |
| DSH 状态 | 必须**完全退出**：关窗口不够，托盘图标也要退出。脚本会先检测，无法确认时拒绝执行 |

## 快速开始（Windows）

```
双击 move-sessions-py.bat
```

然后照着提示走：选「要做什么」（`1`/`2`/`3`）→ 选源 → 勾选会话 → 选目标 → 确认。

也可以不用启动器：

```bat
python move-sessions.py                :: 交互
python move-sessions.py --list         :: 只看看有哪些工作区和会话（只读，DSH 开着也能跑）
```

## 三种模式

| 模式 | 菜单 | 命令行 | 说明 |
|---|---|---|---|
| 1 · 会话 → 另一个工作区 | `1`（回车） | `--src <源> --dst <目标> --sessions <id>` | 目标必须已是工作区记录 |
| 2 · 会话 → 某个文件夹 | `2` | `--src <源> --to-dir <文件夹> [--no-register]` | 文件夹不存在会自动建；还不是工作区就顺手登记成新工作区 |
| 3 · 整个工作区文件夹 → 另一个文件夹 | `3` | `--move-workspace --src <源> --into <父文件夹> [--as <新名>]` | 文件夹**改名**（不复制），工作区记录 id 不变、只更新 `path`，区内所有会话跟着改 |

```bat
:: 模式 1
move-sessions-py.bat --src D:\script\ds --dst D:\script\ds_vul --sessions session-a313de2e -y

:: 模式 2（不存在会新建，未登记会登记；加 --no-register 则只搬不登记）
move-sessions-py.bat --src D:\script\ds --to-dir D:\work\codex_notes --sessions session-a313de2e -y

:: 模式 3
move-sessions-py.bat --move-workspace --src D:\script\ds_java_learn --into D:\script --as ds_learn -y
```

模式判定：带 `--move-workspace` = 模式 3，带 `--to-dir` = 模式 2，否则 = 模式 1；只要给了 `--src`/`--dst`/`--sessions` 任意一个，就不会再问「要做什么」。

## 参数

| 参数 | 说明 |
|---|---|
| `--list` | 只列出工作区和会话，不写任何文件（DSH 开着也能跑） |
| `--src` | 源工作区：编号 / 路径 / 标题，不给就交互选择 |
| `--dst` | 模式 1 的目标工作区：编号 / 路径 / 标题 |
| `--sessions` | 会话 id，逗号分隔；**只写开头一截也能匹配** |
| `--to-dir` | 模式 2：把会话搬到这个文件夹 |
| `--no-register` | 配合 `--to-dir`：只搬、不登记为工作区 |
| `--move-workspace` | 模式 3：整体搬走一个工作区文件夹 |
| `--into` | 模式 3：搬进哪个父文件夹（必须已存在） |
| `--as` | 模式 3：搬过去以后叫什么名字（不给 = 保持原名） |
| `--dry-run` | 预演：规划全走一遍，一个字节都不写 |
| `--yes` / `-y` | 跳过确认直接执行 |
| `--force` | 跳过「DSH 是否已退出」的检测（危险） |
| `--dsh-home` | DSH 配置目录，默认 `%DSH_HOME%` 或 `C:\Users\<你>\.dsh` |
| `--codec` | `zstandard` 或 `node`，默认自动挑 |
| `--backup-dir` | 备份放哪，默认脚本所在目录 |

**第一次用建议先加 `--dry-run`**，看一遍它打算改什么。

## 它到底改了什么

| # | 动作 | 位置 |
|---|---|---|
| 1 | 备份 | `backup-年月日-时分秒\`（workspace.json、projcache、会话目录） |
| 2 | 重写日志 header 的 `cwd` | `sessions\--<编码后的cwd>--\<会话id>\session.v4.jsonl.zstd` 的第 0 帧 |
| 3 | 同步投影缓存 | `storages\session_projcache\sessions\<会话id>.json` 的 `record.identity.cwd` |
| 4 | 改记账 | `storages\workspace.json` 的 `sessionIds`（模式 3 还改记录的 `path`） |
| 5 | 移动会话目录 | `sessions\--旧cwd--\<会话id>\` → `sessions\--新cwd--\<会话id>\` |
| 6 | 复核 | header、目录、记账、projcache 逐项校验 |

第 2 步和第 5 步都不能省：DSH 启动时会把「header 的 cwd 推出的路径」和「文件的真实路径」交叉校验，不一致会直接把日志判为 `corrupt`。

改完**必须重启 DSH**——工作区索引是启动时从磁盘 header 重建的。

## 出错怎么办

- 任何一步失败都会**自动回滚**：撤销已移动的目录，再从本次备份整体还原；模式 3 还会把改过名的文件夹改回原名。
- 退出码：`0` = 成功或你主动取消，`1` = 失败（已回滚），`2` = DSH 还在运行、被拒绝。
- 真要手工还原：把 `backup-*\workspace.json` 拷回 `storages\`，`backup-*\projcache\*.json` 拷回 `storages\session_projcache\sessions\`，`backup-*\sessions\<会话id>\` 拷回原来的 `sessions\--<旧cwd>--\`。
- 确认没问题后 `backup-*` 可以删；建议至少留到重开 DSH、看到会话出现在目标工作区之后再删。

## 已知限制

- 只针对 DSH 当前的存储格式（`session.v4.jsonl.zstd` + `workspace.json` version 2）。DSH 大版本升级后请先 `--list` 再看 `--dry-run` 的输出是否正常。
- 模式 3 是**改名**，跨盘会退化成复制（很慢，别这么干）。
- 不登记到任何工作区的会话不会报错，但不会出现在任何工作区分组里（只能在会话总列表里找到）。
- 只测过 Windows；路径编码规则（`--D-script-ds--` 这种）来自 DSH 自身实现。

## 文件

| 文件 | 说明 |
|---|---|
| `move-sessions.py` | 工具本体（单文件，v1.1） |
| `move-sessions-py.bat` | 双击用的启动器（找 Python、`chcp 65001`、跑完暂停） |
| `使用说明.md` | 详细中文说明（交互示例、输入语法、FAQ） |
| `README.md` | 本文 |
| `backup-*` | 每次搬迁产生的备份 |

## 免责声明

本工具直接改 DSH 的会话存储，属于非官方脚本。虽然内置备份与自动回滚，仍建议首次使用前把整个 `~/.dsh`（至少 `storages\` 与 `sessions\`）复制一份。
