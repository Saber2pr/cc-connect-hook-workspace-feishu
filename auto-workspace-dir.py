#!/usr/bin/env python3
"""
auto-workspace-dir.py — 方案 C：新群首次说话时，自动发一条「一键绑定」交互卡片，
点按钮即把本群绑到一个唯一目录（用 chat_id 命名）。无需复制粘贴。

为什么是这个方案（前面几条路都被约束堵死了，记录清楚免得反复）：
  目标：cc-connect multi-workspace 下，每个飞书群绑一个唯一本地目录、互相隔离，
        且尽量少让用户手动操作。
  约束：
    1) 完全不改 cc-connect 代码（只允许改 config.toml）。
    2) 没有 im:chat 权限 → 任何飞书 API 都拿不到群名 → cc-connect 的 channelName
       恒为空 → 「按群名约定匹配 / init flow 自动建目录」这条路整体失效
       （新群首问会是 mkdir '' ，多个群会撞进同一个空名目录，隔离失效）。
    3) 实测过：cc-connect 的本地 webhook 注入 prompt 不走 multi-workspace 解析，
       注入的 /workspace init 或 yes 落在裸 session_key 的平行 session 上，
       碰不到真正等待确认的 init flow —— 所以「webhook 自动绑定」也失效。
  交集结论：不改 cc-connect 代码 + 无 im:chat 时，「全自动绑定」做不到。
           能做到的最省事形态 = 本方案 C：机器人自动发一条可复制的绑定命令，
           用户 @机器人 粘贴发送一次即可（chat_id 当目录名，唯一、不撞、不需要群名）。

它做什么（由 cc-connect 的 message.received hook / type=command 触发）：
  1) 从 CC_HOOK_SESSION_KEY 拆出飞书 chat_id（形如 oc_xxx）；
  2) 若该群已引导过（本地标记文件存在）→ 直接退出，不重复打扰；
  3) 否则往群里发一条交互卡片（msg_type=interactive），带一个按钮，按钮 value：
        {"action": "cmd:/workspace init <base_dir>/<chat_id>",
         "session_key": "feishu:<chatID>:<userID>"}
     然后写下标记文件，保证每群只发一次。
  用户点按钮 → 飞书发 card.action.trigger → cc-connect 的 onCardAction 命中
  "cmd:" 分支（feishu.go:944），以卡片 value 里的 session_key 当作用户在本群发了
  这条 /workspace init 命令；cc-connect（workspace_init_allow_local_paths=true）
  用这个显式路径 mkdir + 绑定，完成隔离。真正干活在子 Mac，主 Mac 这目录只是占位。
  比复制粘贴省一步；且卡片按钮走同一条 sessionKey，落点确定。

⚠️ 前提：飞书开发者后台要订阅 "card.action.trigger" 事件，否则按钮点了不回调。
   cc-connect 启动日志会打印 "interactive card mode enabled" 作为提醒。

调飞书发消息 REST（im:message）+ 本地 mkdir 一个占位目录（给 /workspace init
校验用，cc-connect 会先 os.Stat 目录必须已存在）。不查群名、不碰仓库、不外发数据。

用法（hook 触发，通常无需手动跑）：
  ./auto-workspace-dir.py --config ~/.cc-connect/config.toml
  session_key / platform 默认取环境变量 CC_HOOK_SESSION_KEY / CC_HOOK_PLATFORM。

凭证与 base_dir（按优先级）：
  1. 命令行 --app-id / --app-secret / --base-dir
  2. 环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET / CC_BASE_DIR
  3. --config <cc-connect config.toml>：自动读第一个 feishu 平台的 app_id/app_secret
     和该 project 的 base_dir（需要 tomllib，py3.11+）
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

FEISHU_BASE = "https://open.feishu.cn/open-apis"

# 每群「已引导」标记：避免 message.received 每条消息都触发、反复刷屏。
GUIDE_STATE_DIR = os.path.expanduser("~/.cc-connect/.auto-ws-guided")


def log(msg):
    print(f"[auto-ws] {msg}", flush=True)


def http_json(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}") from e


def get_tenant_token(app_id, app_secret):
    """换 tenant_access_token（企业自建应用）。"""
    resp = http_json(
        "POST",
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"取 token 失败: {resp.get('code')} {resp.get('msg')}")
    return resp["tenant_access_token"]


def send_text(token, chat_id, text):
    """往群里发一条纯文本消息（飞书 REST im:message，与 cc-connect 的 ws 长连接互不冲突）。"""
    resp = http_json(
        "POST",
        f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        body={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"发消息失败: {resp.get('code')} {resp.get('msg')}")


def build_bind_card(cmd_text, session_key):
    """构造「一键绑定」交互卡片（飞书 message card 2.0，msg_type=interactive）。

    按钮 value 里 action=cmd:<命令>、session_key=<本群会话key> —— 点击后飞书发
    card.action.trigger，cc-connect 的 onCardAction 命中 "cmd:" 分支
    （feishu.go:944），以这个 session_key 当作用户在本群发了 <命令>。
    带上 session_key 是为了走 sessionKeyFromCardAction 的显式分支（feishu.go:4004），
    与用户手打消息落在完全同一条 multi-workspace 会话上，避免退化拼接歧义。

    需要飞书开发者后台订阅 card.action.trigger 事件，否则按钮点了不回调。
    """
    value = {"action": f"cmd:{cmd_text}"}
    if session_key:
        value["session_key"] = session_key
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🤖 绑定本群工作目录"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "本群还没绑定工作目录。点下面按钮即可一键绑定，之后本群对话都会用这个目录。",
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "一键绑定本群目录"},
                        "type": "primary",
                        "value": value,
                    }
                ],
            },
        ],
    }


def send_card(token, chat_id, card):
    """往群里发一条交互卡片（msg_type=interactive）。同样只用 im:message。"""
    resp = http_json(
        "POST",
        f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        body={
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"发卡片失败: {resp.get('code')} {resp.get('msg')}")


def load_from_config(config_path):
    """从 cc-connect config.toml 读第一个 feishu 平台凭证 + 其 project 的 base_dir。"""
    try:
        import tomllib
    except ModuleNotFoundError:
        raise RuntimeError("--config 需要 Python 3.11+（tomllib）。请改用 --app-id/--app-secret/--base-dir")
    with open(os.path.expanduser(config_path), "rb") as f:
        cfg = tomllib.load(f)
    for proj in cfg.get("projects", []):
        base_dir = proj.get("base_dir", "")
        for plat in proj.get("platforms", []):
            if plat.get("type") == "feishu":
                opts = plat.get("options", {})
                app_id = opts.get("app_id", "")
                app_secret = opts.get("app_secret", "")
                if app_id and app_secret:
                    return app_id, app_secret, base_dir
    raise RuntimeError("config 里没找到带 app_id/app_secret 的 feishu 平台")


def resolve_creds(args):
    app_id = args.app_id or os.environ.get("FEISHU_APP_ID", "")
    app_secret = args.app_secret or os.environ.get("FEISHU_APP_SECRET", "")
    base_dir = args.base_dir or os.environ.get("CC_BASE_DIR", "")
    if (not app_id or not app_secret or not base_dir) and args.config:
        c_id, c_secret, c_base = load_from_config(args.config)
        app_id = app_id or c_id
        app_secret = app_secret or c_secret
        base_dir = base_dir or c_base
    missing = [n for n, v in [("app-id", app_id), ("app-secret", app_secret), ("base-dir", base_dir)] if not v]
    if missing:
        raise RuntimeError(f"缺少：{', '.join(missing)}（用 --{'/--'.join(missing)} 或 --config 提供）")
    return app_id, app_secret, base_dir


def chat_id_from_session_key(session_key):
    """从 cc-connect 的 session_key 拆出飞书 chat_id（以 oc_ 开头）。

    hook 注入的是裸 key "feishu:oc_xxx:ou_xxx"；multi-workspace 内部另有带
    "/path:" 前缀的形态，这里都能兼容——只认 oc_ 开头的那一段。
    """
    if not session_key:
        return ""
    for part in session_key.split(":"):
        if part.startswith("oc_"):
            return part
    parts = session_key.split(":")
    for i, part in enumerate(parts):
        if part == "feishu" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def already_guided(chat_id):
    return os.path.exists(os.path.join(GUIDE_STATE_DIR, chat_id))


def mark_guided(chat_id):
    os.makedirs(GUIDE_STATE_DIR, exist_ok=True)
    open(os.path.join(GUIDE_STATE_DIR, chat_id), "w").close()


def bare_session_key_from(session_key, chat_id):
    """还原 cc-connect 卡片回调期望的 bare session_key：feishu:<chatID>:<userID>。

    hook 注入的 CC_HOOK_SESSION_KEY 通常就是 bare 形态；若是 multi-workspace
    带路径前缀的形态（/path:feishu:oc:ou），截取从 feishu 段开始的三段。
    实在拆不出来就退化成 feishu:<chatID>（仅 chat 维度，share-session 场景够用）。
    """
    parts = (session_key or "").split(":")
    for i, part in enumerate(parts):
        if part == "feishu" and i + 2 < len(parts):
            return ":".join(parts[i:i + 3])
        if part == "feishu" and i + 1 < len(parts):
            return ":".join(parts[i:i + 2])
    return f"feishu:{chat_id}"


def on_message(app_id, app_secret, base_dir, session_key, platform):
    """hook（message.received）触发：新群第一次说话时发一条「一键绑定」交互卡片。
    只处理飞书群；每群只引导一次（标记文件幂等）。"""
    if platform and platform != "feishu":
        log(f"跳过：非飞书平台 {platform!r}")
        return
    chat_id = chat_id_from_session_key(session_key)
    if not chat_id:
        log(f"跳过：session_key 里没解析出 chat_id：{session_key!r}")
        return
    if already_guided(chat_id):
        log(f"已引导过，跳过（幂等）：{chat_id}")
        return

    target = os.path.join(os.path.abspath(os.path.expanduser(base_dir)), chat_id)
    # cc-connect 的 /workspace init <本地路径> 会先 os.Stat 校验目录已存在
    # （engine.go:6911），不存在就报「目录不存在」拒绝绑定。所以发卡片前先把这个
    # 占位目录建好——纯本地 mkdir，不碰 cc-connect、不需要任何飞书权限。
    # 真正干活在子 Mac，主 Mac 这目录只是给 init 校验用的占位。
    os.makedirs(target, exist_ok=True)
    cmd_text = f"/workspace init {target}"
    bare_key = bare_session_key_from(session_key, chat_id)
    token = get_tenant_token(app_id, app_secret)
    card = build_bind_card(cmd_text, bare_key)
    send_card(token, chat_id, card)
    # 发成功后再写标记：发失败下次还会重试，不会漏。
    mark_guided(chat_id)
    log(f"✅ 已建占位目录并发绑定卡片到群 {chat_id}，命令：{cmd_text}，回调 session_key={bare_key}")


def main():
    ap = argparse.ArgumentParser(description="新群首次说话时自动发可复制的 /workspace init 引导命令（方案 C，不改 cc-connect，hook 触发）")
    ap.add_argument("--app-id", help="飞书应用 app_id（或 FEISHU_APP_ID）")
    ap.add_argument("--app-secret", help="飞书应用 app_secret（或 FEISHU_APP_SECRET）")
    ap.add_argument("--base-dir", help="cc-connect 的 base_dir（或 CC_BASE_DIR）")
    ap.add_argument("--config", help="从 cc-connect config.toml 读凭证与 base_dir（py3.11+）")
    ap.add_argument("--session-key", default=os.environ.get("CC_HOOK_SESSION_KEY", ""),
                    help="hook 的 session_key（默认取环境变量 CC_HOOK_SESSION_KEY）")
    ap.add_argument("--platform", default=os.environ.get("CC_HOOK_PLATFORM", ""),
                    help="hook 的 platform（默认取环境变量 CC_HOOK_PLATFORM）")
    args = ap.parse_args()

    try:
        app_id, app_secret, base_dir = resolve_creds(args)
    except RuntimeError as e:
        log(f"配置错误：{e}")
        sys.exit(2)

    # hook 触发：只处理这一个群。失败不 exit 非零（避免拖累 cc-connect 的 hook 日志）。
    try:
        on_message(app_id, app_secret, base_dir, args.session_key, args.platform)
    except RuntimeError as e:
        log(f"on-message 失败：{e}")


if __name__ == "__main__":
    main()


# ============================================================================
# 纯配置 hook，不改 cc-connect 代码
# ============================================================================
#
# 在 cc-connect 的 config.toml 顶层加（hooks 是顶层，不是 project 级；已确认）：
#
#   [[hooks]]
#   event   = "message.received"
#   type    = "command"
#   async   = true
#   timeout = 15
#   command = "/opt/homebrew/bin/python3 绝对路径/auto-workspace-dir.py --config 绝对路径/.cc-connect/config.toml"
#
# ⚠️ 两个坑（实测踩过）：
#   1) 用 python3 绝对路径、且必须是 3.11+（脚本用 tomllib 读 config）。本机
#      /usr/bin/python3 是系统 3.9 没有 tomllib，要用 brew 的 /opt/homebrew/bin/python3。
#   2) 本机 shell_profile = "source ~/.zshrc"，cc-connect 会把它前置到每条 hook 命令。
#      非交互 shell 下旧 .zshrc 会报错（autoload/compinit/_bun 语法）导致 exit 127，
#      连脚本都跑不到。已在 ~/.zshrc 顶部加 `[[ $- != *i* ]] && return` 让非交互直接返回。
#
# 流程：
#   新群第一次说话 → hook 触发本脚本 → 发一条可复制的 /workspace init <base_dir>/<chat_id>
#   用户 @机器人 粘贴发送 → cc-connect 用显式路径 mkdir + 绑定 → 本群隔离完成。
#   每群只引导一次（~/.cc-connect/.auto-ws-guided/<chat_id> 标记）。
#
# 权限：只需 im:message（发消息）。不需要 im:chat（不查群名）。
#
# 重置某群引导：删掉 ~/.cc-connect/.auto-ws-guided/<chat_id> 即可让它再引导一次。
