# cc-connect-hooks

cc-connect 的配置驱动 hook 脚本集合。不改 cc-connect 代码，只通过 config.toml 的
`[[hooks]]` 挂载，用飞书 REST（仅 `im:message` 权限）实现自动化。

## auto-workspace-dir.py

multi-workspace 模式下，新飞书群第一次说话时自动发一条「一键绑定」交互卡片，
点按钮即把本群绑到一个唯一本地目录（用 chat_id 命名，互不冲突，无需群名）。

- 触发：cc-connect 的 `message.received` hook（type=command）。
- 机制：卡片按钮 `value.action = "cmd:/workspace init <base_dir>/<chat_id>"`，
  点击后飞书发 `card.action.trigger`，cc-connect 的 onCardAction 命中 `cmd:` 分支，
  以卡片 value 里的 `session_key` 当作用户在本群发了这条命令并绑定目录。
- 权限：只需 `im:message`，不查群名（不需要 `im:chat`）。
- 前提：飞书开发者后台订阅 `card.action.trigger` 事件，否则按钮不回调。

### 挂载（cc-connect config.toml 顶层）

```toml
[[hooks]]
event   = "message.received"
type    = "command"
async   = true
timeout = 15
command = "/opt/homebrew/bin/python3 /path/to/auto-workspace-dir.py --config /path/to/config.toml"
```

两个坑：
1. 用 python3 3.11+ 绝对路径（脚本用 tomllib 读 config）；macOS 系统 3.9 无 tomllib。
2. cc-connect 会把 `shell_profile`（如 `source ~/.zshrc`）前置到每条 hook 命令；
   非交互 shell 下老 .zshrc 可能报错致 exit 127。在 .zshrc 顶部加
   `[[ $- != *i* ]] && return` 让非交互直接返回。

重置某群引导：删掉 `~/.cc-connect/.auto-ws-guided/<chat_id>` 即可再引导一次。
