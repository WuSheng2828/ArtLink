#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ArtLink - ComfyUI QQ 机器人控制台 (单文件版) v1.2.0
==================================================
通过 QQ（NapCat）远程控制 ComfyUI 进行 AI 绘图和图片反推。
支持图片生成、文字反推、多层白名单、黑名单、自助注册、
管理菜单、快捷指令、消息撤回、排队提示、失败重试、历史记录等。

v1.2.0 新增：
  • 「随便来点」随机提示词功能（26分类词库，支持SFW/NSFW模式切换）
  • 外置词库文件 wordbank.json（可随时编辑增删）
  • 词库分块管理：wordbank_parts/ + merge_wordbank.bat
  • 再来一张改用数据库回退（重启后依然可用）
  • 后台选项卡重新整理（系统配置/权限管理/工作流管理/过滤/随便来点）
  • 随便来点群聊次数限制（跟随总上限/不限制/固定次数）
  • 随便来点使用发起人最后使用的工作流

v1.1.0 新增：
  • 工作流单独限次（跟随总上限 / 单独N次 / -1无限制）
  • 私聊「再来一张」(支持数字1-5及中文一二三四五，后台可配触发词和开关)
  • 私聊「绘图模式」(免触发词生图，超时自动退出)
  • 提示词过滤独立区域 + 折叠 + 过滤词预设功能
  • 四个白名单/黑名单全部可折叠
  • 私聊返回内容自定义勾选（提示词/尺寸/耗时/工作流名）
  • 控制台日志更详细（显示用户、类型、工作流、结果，不显示提示词）
"""

import os
import sys
import json
import time
import queue
import threading
import sqlite3
import datetime
import logging
import logging.handlers  # 日志轮转功能
import hashlib
import re
import subprocess  # 用于自动安装依赖
import random      # 随机提示词用
from pathlib import Path

# 禁止生成 __pycache__ 目录，减少文件碎片
sys.dont_write_bytecode = True

# =====================================================================
# 环境自适应：优先使用便携 Python，不存在则使用系统 Python
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent  # 脚本所在目录（项目根目录）
PYTHON_DIR = BASE_DIR / "Python311"         # 便携 Python 目录

# 检测便携 Python 是否可用
PORTABLE_PYTHON = PYTHON_DIR / "python.exe"
USE_PORTABLE = PORTABLE_PYTHON.exists()

if USE_PORTABLE:
    # 加载便携环境
    sys.path.insert(0, str(PYTHON_DIR / "Lib" / "site-packages"))
    os.environ["PATH"] = str(PYTHON_DIR) + ";" + str(PYTHON_DIR / "Scripts") + ";" + os.environ["PATH"]

# 尝试导入第三方库，检查依赖是否齐全
def check_dependency(name):
    """检测指定 Python 包是否已安装"""
    try:
        __import__(name)
        return True
    except ImportError:
        return False

MISSING_DEPS = []  # 记录缺失的依赖
for dep in ["requests", "websocket", "flask"]:
    if not check_dependency(dep):
        MISSING_DEPS.append(dep)

if MISSING_DEPS:
    if USE_PORTABLE:
        # 便携环境缺依赖 → 自动安装（便携环境不会影响用户系统）
        print(f"[环境检查] 检测到缺失依赖: {', '.join(MISSING_DEPS)}，正在自动安装...")
        pip_exe = PYTHON_DIR / "Scripts" / "pip.exe"
        if pip_exe.exists():
            for dep in MISSING_DEPS:
                try:
                    subprocess.check_call([str(pip_exe), "install", dep],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print(f"  已安装: {dep}")
                except Exception as e:
                    print(f"  安装 {dep} 失败: {e}")
    else:
        # 系统环境缺依赖 → 友好提示，不自动安装（保护用户系统环境）
        print("=" * 60)
        print("  ❌ 缺少必要依赖，无法启动 ArtLink")
        print("=" * 60)
        print(f"  缺失的包: {', '.join(MISSING_DEPS)}")
        print()
        print("  请手动执行以下命令安装：")
        print(f"  pip install {' '.join(MISSING_DEPS)}")
        print()
        print("  或者下载便携 Python 环境包，解压到 Python311 文件夹。")
        print("=" * 60)
        sys.exit(1)

import requests
import websocket
from flask import Flask, request, jsonify, render_template_string

# =====================================================================
# 日志系统配置（支持轮转，防止日志无限增长）
# =====================================================================
LOG_FILE = BASE_DIR / "artlink.log"
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(message)s'

# 创建日志处理器：单个文件最大 5MB，保留最近 3 个备份
log_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, encoding='utf-8', maxBytes=5*1024*1024, backupCount=3
)
log_handler.setFormatter(logging.Formatter(LOG_FORMAT))

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

logging.basicConfig(level=logging.INFO, handlers=[log_handler, console_handler])
logger = logging.getLogger("ArtLink")

# =====================================================================
# 全局配置常量
# =====================================================================
APP_HOST = "127.0.0.1"    # Web 管理后台地址（仅本地访问）
APP_PORT = 5000           # Web 管理后台端口
NAPCAT_HTTP = "http://127.0.0.1:3000"    # NapCat HTTP API 地址（用于发送消息/调用API）
NAPCAT_WS = "ws://127.0.0.1:3001"        # NapCat WebSocket 地址（用于接收QQ消息）
COMFYUI_SERVER = "http://127.0.0.1:8188" # ComfyUI 服务地址

# 默认输入目录（反推图片存放位置，首次启动写入数据库，后续可在后台修改）
COMFYUI_INPUT = Path(r"ComfyUI/input")

# 全局状态变量
comfyui_online = True          # ComfyUI 是否在线
comfyui_fail_count = 0         # 连续失败次数
comfyui_lock = threading.Lock()  # 线程锁

# NapCat 在线状态
napcat_online = True
napcat_fail_count = 0
napcat_lock = threading.Lock()

queue_counter = 0              # 当前排队任务数
queue_lock = threading.Lock()  # 排队计数器锁

# 私聊无权限静默期：记录每个 QQ 最近一次被警告的时间
private_warned = {}
warn_lock = threading.Lock()
WARN_INTERVAL = 30  # 30 秒内不再重复提示

DB_PATH = BASE_DIR / "artlink.db"  # SQLite 数据库文件路径

# =====================================================================
# 工具函数
# =====================================================================
def is_admin(qq):
    """检查给定的 QQ 号是否在管理员表中"""
    db = get_db()
    row = db.execute("SELECT 1 FROM admin_list WHERE qq=?", (qq,)).fetchone()
    db.close()
    return row is not None

# =====================================================================
# 撤回任务记录
# =====================================================================
group_recent_tasks = {}   # 结构：{group_id: [{sender_qq, sent_msg_ids, timestamp}, ...]}
task_lock = threading.Lock()

def add_recent_task(group_id, sender_qq, msg_ids):
    """记录一个任务的发送消息ID，用于后续撤回"""
    if not group_id:
        return
    with task_lock:
        if group_id not in group_recent_tasks:
            group_recent_tasks[group_id] = []
        # 最新的任务放在列表最前面
        group_recent_tasks[group_id].insert(0, {
            "sender_qq": sender_qq,
            "sent_msg_ids": msg_ids,
            "timestamp": time.time()
        })
        # 清理超过2分钟的记录（QQ撤回时限）
        cutoff = time.time() - 120
        group_recent_tasks[group_id] = [
            t for t in group_recent_tasks[group_id] if t["timestamp"] > cutoff
        ]

def get_user_recent_task(group_id, sender_qq):
    """获取指定用户在该群最近一个 2 分钟内的任务"""
    with task_lock:
        tasks = group_recent_tasks.get(group_id, [])
        for idx, t in enumerate(tasks):
            if t["sender_qq"] == sender_qq:
                return idx, t
    return None, None

# =====================================================================
# 尺寸配置（基于 config 表，支持动态增减）
# =====================================================================
def load_size_presets():
    """从 config 表加载所有图片尺寸预设，按触发词长度降序排列"""
    size_list_str = get_config("size_preset_list", "")
    if not size_list_str:
        return []
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    presets = []
    for name in names:
        width = int(get_config(f"size_{name}_width", "1024"))
        height = int(get_config(f"size_{name}_height", "1024"))
        trigger = get_config(f"size_{name}_trigger", name)
        presets.append({"name": name, "width": width, "height": height, "trigger_word": trigger})
    # 按触发词长度从长到短排序，避免短触发词误匹配
    presets.sort(key=lambda x: len(x["trigger_word"]), reverse=True)
    return presets

def get_default_size():
    """返回默认图片尺寸：正方 1024×1024"""
    return ("正方", 1024, 1024)

def save_size_preset(name, width, height, trigger_word):
    """添加或更新一个图片尺寸预设"""
    size_list_str = get_config("size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if name not in names:
        names.append(name)
        set_config("size_preset_list", ",".join(names))
    set_config(f"size_{name}_width", str(width))
    set_config(f"size_{name}_height", str(height))
    set_config(f"size_{name}_trigger", trigger_word)

def delete_size_preset(name):
    """删除一个图片尺寸预设"""
    size_list_str = get_config("size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if name in names:
        names.remove(name)
        set_config("size_preset_list", ",".join(names) if names else "")
    for key in [f"size_{name}_width", f"size_{name}_height", f"size_{name}_trigger"]:
        db = get_db()
        db.execute("DELETE FROM config WHERE key=?", (key,))
        db.commit()
        db.close()

def init_size_presets():
    """首次启动时，自动写入默认的三个图片尺寸预设"""
    if not get_config("size_preset_list"):
        save_size_preset("正方", 1024, 1024, "正方")
        save_size_preset("横版", 1344, 768, "横版")
        save_size_preset("竖版", 768, 1344, "竖版")
        logger.info("已初始化默认图片尺寸预设")

# =====================================================================
# 视频尺寸配置（独立于图片尺寸）
# =====================================================================
def load_video_size_presets():
    """从 config 表加载所有视频尺寸预设，按触发词长度降序排列"""
    size_list_str = get_config("video_size_preset_list", "")
    if not size_list_str:
        return []
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    presets = []
    for name in names:
        width = int(get_config(f"video_size_{name}_width", "512"))
        height = int(get_config(f"video_size_{name}_height", "512"))
        trigger = get_config(f"video_size_{name}_trigger", name)
        presets.append({"name": name, "width": width, "height": height, "trigger_word": trigger})
    presets.sort(key=lambda x: len(x["trigger_word"]), reverse=True)
    return presets

def get_default_video_size():
    """返回默认视频尺寸：正方 512×512"""
    return ("正方", 512, 512)

def save_video_size_preset(name, width, height, trigger_word):
    """添加或更新一个视频尺寸预设"""
    size_list_str = get_config("video_size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if name not in names:
        names.append(name)
        set_config("video_size_preset_list", ",".join(names))
    set_config(f"video_size_{name}_width", str(width))
    set_config(f"video_size_{name}_height", str(height))
    set_config(f"video_size_{name}_trigger", trigger_word)

def delete_video_size_preset(name):
    """删除一个视频尺寸预设"""
    size_list_str = get_config("video_size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if name in names:
        names.remove(name)
        set_config("video_size_preset_list", ",".join(names) if names else "")
    for key in [f"video_size_{name}_width", f"video_size_{name}_height", f"video_size_{name}_trigger"]:
        db = get_db()
        db.execute("DELETE FROM config WHERE key=?", (key,))
        db.commit()
        db.close()

def init_video_size_presets():
    """首次启动时，自动写入默认的三个视频尺寸预设"""
    if not get_config("video_size_preset_list"):
        save_video_size_preset("正方", 512, 512, "正方")
        save_video_size_preset("横版", 768, 432, "横版")
        save_video_size_preset("竖版", 432, 768, "竖版")
        logger.info("已初始化默认视频尺寸预设")

# =====================================================================
# 工作流 JSON 文件扫描
# =====================================================================
def scan_workflow_files():
    """扫描项目目录下所有 .json 文件（递归），返回相对路径列表"""
    json_files = []
    # 遍历项目目录及其子文件夹
    for root, dirs, files in os.walk(str(BASE_DIR)):
        # 排除便携 Python 环境目录，避免扫描大量无关文件
        if "Python311" in dirs:
            dirs.remove("Python311")
        for f in files:
            if f.endswith(".json"):
                full_path = Path(root) / f
                # 计算相对于项目根目录的路径
                relative_path = full_path.relative_to(BASE_DIR)
                json_files.append(str(relative_path).replace("\\", "/"))
    json_files.sort()  # 按字母排序
    return json_files

# =====================================================================
# 数据库初始化
# =====================================================================
def init_db():
    """初始化数据库，创建所有表，兼容旧版本升级"""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()

    # 所有数据表定义
    tables = [
        "CREATE TABLE IF NOT EXISTS private_whitelist (qq TEXT PRIMARY KEY, remark TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS group_whitelist (group_id TEXT PRIMARY KEY, remark TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS group_member_whitelist (group_id TEXT, qq TEXT, remark TEXT DEFAULT '', PRIMARY KEY (group_id, qq))",
        "CREATE TABLE IF NOT EXISTS group_blacklist (group_id TEXT, qq TEXT, remark TEXT DEFAULT '', PRIMARY KEY (group_id, qq))",
        "CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, qq TEXT, group_id TEXT, workflow TEXT, output_type TEXT, prompt_text TEXT, size TEXT, status TEXT, duration REAL, remark TEXT)",
        "CREATE TABLE IF NOT EXISTS trigger_words (word TEXT PRIMARY KEY, workflow TEXT)",
        "CREATE TABLE IF NOT EXISTS workflows (name TEXT PRIMARY KEY, file_path TEXT, positive_node_title TEXT DEFAULT '', latent_node_title TEXT DEFAULT '', image_node_title TEXT DEFAULT '', note TEXT DEFAULT '', example TEXT DEFAULT '', output_type TEXT DEFAULT 'image', usage_mode TEXT DEFAULT 'global', usage_limit INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS user_size (user_key TEXT PRIMARY KEY, width INTEGER, height INTEGER)",
        "CREATE TABLE IF NOT EXISTS daily_usage (qq TEXT, group_id TEXT, date TEXT, count INTEGER, PRIMARY KEY (qq, group_id, date))",
        "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS admin_list (qq TEXT PRIMARY KEY, remark TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS workflow_daily_usage (qq TEXT, group_id TEXT, workflow TEXT, date TEXT, count INTEGER, PRIMARY KEY (qq, group_id, workflow, date))",
        "CREATE TABLE IF NOT EXISTS filter_presets (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, words TEXT)",
    ]
    for sql in tables:
        c.execute(sql)

    # 兼容旧版本：为可能缺失的字段自动补全
    new_columns = [
        ("duration", "history"), ("remark", "private_whitelist"),
        ("remark", "group_whitelist"), ("remark", "group_member_whitelist"),
        ("example", "workflows"), ("output_type", "workflows"),
        ("usage_mode", "workflows"), ("usage_limit", "workflows"),
    ]
    for col, tab in new_columns:
        try:
            c.execute(f"ALTER TABLE {tab} ADD COLUMN {col} TEXT DEFAULT ''")
        except:
            pass
    # usage_limit 是整数列，单独处理
    try:
        c.execute("ALTER TABLE workflows ADD COLUMN usage_limit INTEGER DEFAULT 0")
    except:
        pass
    # group_whitelist 的 enabled 列（默认 1，用于每群独立开关）
    try:
        c.execute("ALTER TABLE group_whitelist ADD COLUMN enabled INTEGER DEFAULT 1")
    except:
        pass
    # workflows 的 nodes_config 列（JSON格式，支持多节点配置）
    try:
        c.execute("ALTER TABLE workflows ADD COLUMN nodes_config TEXT DEFAULT ''")
    except:
        pass

    # 从旧版 config 迁移管理员到新表
    try:
        old_row = c.execute("SELECT value FROM config WHERE key='admin_qq'").fetchone()
        if old_row and old_row["value"].strip():
            admin_qq = old_row["value"].strip()
            if not c.execute("SELECT 1 FROM admin_list WHERE qq=?", (admin_qq,)).fetchone():
                c.execute("INSERT OR IGNORE INTO admin_list (qq) VALUES (?)", (admin_qq,))
                logger.info(f"已从 config 迁移管理员 {admin_qq}")
            c.execute("DELETE FROM config WHERE key='admin_qq'")
    except:
        pass

    conn.commit()
    conn.close()

def get_db():
    """获取数据库连接（结果可用列名访问）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def get_config(key, default=None):
    """从 config 表读取配置项，不存在则返回默认值"""
    db = get_db()
    cur = db.execute("SELECT value FROM config WHERE key=?", (key,))
    row = cur.fetchone()
    db.close()
    return row["value"] if row else default

def set_config(key, value):
    """写入配置项，如已存在则更新"""
    db = get_db()
    db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, str(value)))
    db.commit()
    db.close()

def init_config():
    """首次启动时写入所有默认配置项"""
    defaults = {
        "comfyui_input_dir": str(COMFYUI_INPUT),
        "napcat_http": NAPCAT_HTTP,
        "napcat_ws": NAPCAT_WS,
        "comfyui_server": COMFYUI_SERVER,
        "max_daily_count": "20",
        "public_help": (
            "📋 帮助\n"
            "【指令】\n"
            "  绘图 + 提示词 → 文生图（消耗次数）\n"
            "  反推 + 发送图片 → 图片反推（不消耗次数）\n"
            "  撤回 → 撤回自己 2 分钟内最新生成的任务\n"
            "  注册 → 自助加入白名单\n"
            "  尺寸触发词 → 切换尺寸（后台可自定义）\n"
            "  说明(帮助/help等) → 查看帮助\n"
            "  随便来点 → 随机提示词生图（需后台开启）\n"
            "  停止 → 停止当前任务\n"
            "次数每日 0 点刷新"
        ),
        "admin_help": "",
        "help_triggers": "说明,帮助,help,？,?,帮助文档,使用帮助,指令,菜单,功能,教程,怎么用,使用说明",
        "filter_enabled": "false",    # 过滤功能默认关闭
        "filter_words": "",           # 默认无过滤词
        "regen_enabled": "true",      # 再来一张默认开启
        "regen_triggers": "再来,继续,加图,再来一次",  # 再来一张触发词
        "drawing_mode_trigger": "绘图模式",  # 绘图模式进入触发词
        "drawing_mode_duration": "5",       # 绘图模式持续时间（分钟）
        "private_display_flags": "prompt,size,duration,workflow",  # 私聊返回显示项
        "regen_private_enabled": "true",  # 私聊再来一张默认开启
        "regen_group_enabled": "true",    # 群聊再来一张默认开启
        "group_display_flags": "duration", # 群聊默认仅显示耗时
        # ── 随便来点配置默认值 ──
        "random_enabled": "false",         # 随便来点功能默认关闭
        "random_trigger": "随便来点",       # 触发词
        "random_enabled_private": "true",  # 私聊默认开启
        "random_enabled_group": "true",    # 群聊默认开启
        "random_sfw": "true",              # SFW 默认勾选
        "random_nsfw": "false",            # NSFW 默认不勾选
        "random_usage_mode": "global",     # 随便来点群聊次数模式：global/limited/unlimited
        "random_usage_limit": "0",         # 固定次数值
        # ── 随便来点独立显示设置 ──
        "random_private_display_flags": "prompt,size,duration,workflow",  # 随便来点私聊默认显示全部
        "random_group_display_flags": "prompt,duration",                  # 随便来点群聊默认显示提示词+耗时
        # ── 白名单开关默认值 ──
        "private_whitelist_enabled": "true",  # 私聊白名单默认开启
        "group_whitelist_enabled": "true",    # 群聊白名单默认开启
        # ── 停止任务配置 ──
        "stop_triggers": "停止,终止,取消任务",
        "stop_private_enabled": "true",
        "stop_group_enabled": "true",
    }
    for k, v in defaults.items():
        if get_config(k) is None:
            set_config(k, v)
    init_size_presets()
    init_video_size_presets()

def notify_admin(text):
    """向所有管理员发送通知消息"""
    db = get_db()
    admins = db.execute("SELECT qq FROM admin_list").fetchall()
    db.close()
    for row in admins:
        send_private_message(row["qq"], f"🔔 [ArtLink]\n{text}")

def check_napcat_online():
    """检查 NapCat 是否在线，通过请求 get_version_info API"""
    global napcat_online, napcat_fail_count
    try:
        resp = requests.get(f"{get_config('napcat_http', NAPCAT_HTTP)}/get_version_info", timeout=5)
        if resp.status_code == 200:
            with napcat_lock:
                if not napcat_online:
                    napcat_online = True
                    napcat_fail_count = 0
                    logger.info("NapCat 已恢复连接")
                    notify_admin("✅ NapCat 已恢复连接")
                napcat_fail_count = 0
            return True
    except Exception:
        pass

    with napcat_lock:
        napcat_fail_count += 1
        if napcat_online and napcat_fail_count >= 3:
            napcat_online = False
            logger.warning("NapCat 失去连接")
            notify_admin("⚠️ NapCat 失去连接，请检查！")
    return False
# =====================================================================
# Flask 应用
# =====================================================================
app = Flask(__name__)

# =====================================================================
# 管理后台 HTML 模板 - CSS 部分（5套皮肤 + 美化）
# =====================================================================
ADMIN_HTML_CSS = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>ArtLink v2.0</title>
<style>
    /* ═══════════════════════════════════════════════════════════
       皮肤系统 — CSS 变量（默认=经典蓝极简原版）
       ═══════════════════════════════════════════════════════════ */
    :root, [data-theme="default"] {
        --bg: linear-gradient(135deg,#f0f2f5 0%,#e8ecf1 100%);
        --bg-card: #ffffff;
        --bg-card2: #fafbfc;
        --bg-input: #ffffff;
        --text: #2c3e50;
        --text2: #34495e;
        --text3: #7f8c8d;
        --text4: #95a5a6;
        --text5: #555;
        --primary: #3498db;
        --primary2: #2980b9;
        --primary-light: rgba(52,152,219,.06);
        --primary-glow: rgba(52,152,219,.15);
        --success: #27ae60;
        --danger: #e74c3c;
        --warning: #f39c12;
        --border: #ecf0f1;
        --border2: #e8ecf1;
        --border3: #dce1e6;
        --border4: #e0e6ed;
        --shadow: 0 1px 4px rgba(0,0,0,.06);
        --shadow-hover: 0 4px 12px rgba(0,0,0,.1);
        --table-head-bg: linear-gradient(135deg,#3498db,#2980b9);
        --table-head-text: #ffffff;
        --table-even: #f8f9fa;
        --table-hover: #eef5fc;
        --tab-active-bg: rgba(52,152,219,.08);
        --chip-bg: #fff;
        --chip-border: #e0e6ed;
        --trigger-grad: linear-gradient(135deg,#3498db,#2471a3);
        --modal-bg: #fff;
        --modal-overlay: rgba(0,0,0,.4);
        --toast-bg: #2c3e50;
        --toast-text: #fff;
        --radius-sm: 6px;
        --radius-md: 10px;
        --radius-lg: 12px;
        --radius-xl: 20px;
        --glow: none;
        --backdrop: none;
        --card-border: 1px solid #e8ecf1;
        --scrollbar-track: #f0f0f0;
        --scrollbar-thumb: #c0c0c0;
        --info-connect: #1a7d63;
        --info-permission: #b8730a;
        --info-tip: #2c6db5;
        --info-filter: #7b4db5;
    }

    /* ── 深色科技 ── */
    [data-theme="dark"] {
        --bg: #0f172a;
        --bg-card: rgba(255,255,255,0.05);
        --bg-card2: rgba(255,255,255,0.03);
        --bg-input: rgba(255,255,255,0.08);
        --text: #e2e8f0;
        --text2: #cbd5e1;
        --text3: #94a3b8;
        --text4: #64748b;
        --text5: #94a3b8;
        --primary: #818cf8;
        --primary2: #6366f1;
        --primary-light: rgba(129,140,248,.1);
        --primary-glow: rgba(129,140,248,.25);
        --success: #10b981;
        --danger: #ef4444;
        --warning: #f59e0b;
        --border: rgba(255,255,255,0.08);
        --border2: rgba(255,255,255,0.06);
        --border3: rgba(255,255,255,0.1);
        --border4: rgba(255,255,255,0.08);
        --shadow: 0 2px 8px rgba(0,0,0,.3);
        --shadow-hover: 0 4px 20px rgba(0,0,0,.4);
        --table-head-bg: rgba(129,140,248,.15);
        --table-head-text: #e2e8f0;
        --table-even: rgba(255,255,255,0.02);
        --table-hover: rgba(129,140,248,.08);
        --tab-active-bg: rgba(129,140,248,.15);
        --chip-bg: rgba(255,255,255,0.05);
        --chip-border: rgba(255,255,255,0.1);
        --trigger-grad: linear-gradient(135deg,#818cf8,#6366f1);
        --modal-bg: #1e293b;
        --modal-overlay: rgba(0,0,0,.6);
        --toast-bg: #1e293b;
        --toast-text: #e2e8f0;
        --radius-sm: 8px;
        --radius-md: 14px;
        --radius-lg: 16px;
        --radius-xl: 24px;
        --glow: 0 0 30px rgba(129,140,248,.15);
        --backdrop: blur(20px);
        --card-border: 1px solid rgba(255,255,255,0.08);
        --scrollbar-track: #1e293b;
        --scrollbar-thumb: #334155;
        --info-connect: #5ee8c0;
        --info-permission: #f5b866;
        --info-tip: #8eb8f0;
        --info-filter: #c4b0e8;
    }

    /* ── 浅色精致 ── */
    [data-theme="light"] {
        --bg: linear-gradient(135deg,#f8fafc 0%,#eff6ff 100%);
        --bg-card: #ffffff;
        --bg-card2: #f8fafc;
        --bg-input: #ffffff;
        --text: #1e293b;
        --text2: #334155;
        --text3: #64748b;
        --text4: #94a3b8;
        --text5: #475569;
        --primary: #6366f1;
        --primary2: #4f46e5;
        --primary-light: rgba(99,102,241,.08);
        --primary-glow: rgba(99,102,241,.2);
        --success: #10b981;
        --danger: #ef4444;
        --warning: #f59e0b;
        --border: #e2e8f0;
        --border2: #e2e8f0;
        --border3: #cbd5e1;
        --border4: #e2e8f0;
        --shadow: 0 1px 3px rgba(0,0,0,.04), 0 1px 2px rgba(0,0,0,.06);
        --shadow-hover: 0 10px 25px rgba(0,0,0,.08);
        --table-head-bg: linear-gradient(135deg,#6366f1,#4f46e5);
        --table-head-text: #ffffff;
        --table-even: #f8fafc;
        --table-hover: #eef2ff;
        --tab-active-bg: rgba(99,102,241,.08);
        --chip-bg: #fff;
        --chip-border: #e2e8f0;
        --trigger-grad: linear-gradient(135deg,#6366f1,#4f46e5);
        --modal-bg: #fff;
        --modal-overlay: rgba(0,0,0,.4);
        --toast-bg: #1e293b;
        --toast-text: #fff;
        --radius-sm: 8px;
        --radius-md: 14px;
        --radius-lg: 16px;
        --radius-xl: 24px;
        --glow: none;
        --backdrop: none;
        --card-border: 1px solid #e2e8f0;
        --scrollbar-track: #f1f5f9;
        --scrollbar-thumb: #cbd5e1;
        --info-connect: #0d9488;
        --info-permission: #c2780a;
        --info-tip: #4f6ecc;
        --info-filter: #8b5cf6;
    }

    /* ── 暗夜绿 ── */
    [data-theme="green"] {
        --bg: #0a0a0a;
        --bg-card: rgba(255,255,255,0.03);
        --bg-card2: rgba(255,255,255,0.02);
        --bg-input: rgba(255,255,255,0.06);
        --text: #d1d5db;
        --text2: #9ca3af;
        --text3: #6b7280;
        --text4: #4b5563;
        --text5: #9ca3af;
        --primary: #10b981;
        --primary2: #059669;
        --primary-light: rgba(16,185,129,.1);
        --primary-glow: rgba(16,185,129,.2);
        --success: #10b981;
        --danger: #ef4444;
        --warning: #f59e0b;
        --border: rgba(255,255,255,0.06);
        --border2: rgba(255,255,255,0.04);
        --border3: rgba(255,255,255,0.08);
        --border4: rgba(255,255,255,0.06);
        --shadow: 0 2px 8px rgba(0,0,0,.5);
        --shadow-hover: 0 4px 20px rgba(0,0,0,.6);
        --table-head-bg: rgba(16,185,129,.12);
        --table-head-text: #d1d5db;
        --table-even: rgba(255,255,255,0.015);
        --table-hover: rgba(16,185,129,.06);
        --tab-active-bg: rgba(16,185,129,.12);
        --chip-bg: rgba(255,255,255,0.03);
        --chip-border: rgba(255,255,255,0.08);
        --trigger-grad: linear-gradient(135deg,#10b981,#059669);
        --modal-bg: #171717;
        --modal-overlay: rgba(0,0,0,.7);
        --toast-bg: #171717;
        --toast-text: #d1d5db;
        --radius-sm: 4px;
        --radius-md: 8px;
        --radius-lg: 10px;
        --radius-xl: 14px;
        --glow: none;
        --backdrop: none;
        --card-border: 1px solid rgba(255,255,255,0.06);
        --scrollbar-track: #0a0a0a;
        --scrollbar-thumb: #1f2937;
        --info-connect: #4adec0;
        --info-permission: #f5b866;
        --info-tip: #7db8f0;
        --info-filter: #c4b0e8;
    }

    /* ── 暖橙 ── */
    [data-theme="warm"] {
        --bg: linear-gradient(135deg,#fef7ed 0%,#fef2e0 100%);
        --bg-card: #fffbf5;
        --bg-card2: #fef7ed;
        --bg-input: #fffbf5;
        --text: #3d3027;
        --text2: #5c4a3a;
        --text3: #8b7355;
        --text4: #a8927c;
        --text5: #6b5a4a;
        --primary: #f59e0b;
        --primary2: #d97706;
        --primary-light: rgba(245,158,11,.1);
        --primary-glow: rgba(245,158,11,.2);
        --success: #10b981;
        --danger: #ef4444;
        --warning: #f59e0b;
        --border: #f0e0cc;
        --border2: #f0e0cc;
        --border3: #e5d5c0;
        --border4: #f0e0cc;
        --shadow: 0 2px 6px rgba(139,115,85,.08);
        --shadow-hover: 0 6px 20px rgba(139,115,85,.12);
        --table-head-bg: linear-gradient(135deg,#f59e0b,#d97706);
        --table-head-text: #ffffff;
        --table-even: #fef7ed;
        --table-hover: #fef0dc;
        --tab-active-bg: rgba(245,158,11,.1);
        --chip-bg: #fffbf5;
        --chip-border: #f0e0cc;
        --trigger-grad: linear-gradient(135deg,#f59e0b,#d97706);
        --modal-bg: #fffbf5;
        --modal-overlay: rgba(0,0,0,.35);
        --toast-bg: #3d3027;
        --toast-text: #fffbf5;
        --radius-sm: 8px;
        --radius-md: 14px;
        --radius-lg: 16px;
        --radius-xl: 24px;
        --glow: none;
        --backdrop: none;
        --card-border: 1px solid #f0e0cc;
        --scrollbar-track: #fef7ed;
        --scrollbar-thumb: #e5d5c0;
        --info-connect: #0d9488;
        --info-permission: #b85e00;
        --info-tip: #3b6cb4;
        --info-filter: #8b5cf6;
    }

    /* ═══════════════════════════════════════════════
       全局样式
       ═══════════════════════════════════════════════ */
    *, *::before, *::after{box-sizing:border-box}
    html{scroll-behavior:smooth}
    body{
        font-family:Microsoft YaHei,-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica Neue,sans-serif;
        margin:0;padding:20px 24px;
        background:var(--bg);
        min-height:100vh;color:var(--text);
        transition:background .3s,color .3s;
        -webkit-font-smoothing:antialiased
    }
    a{color:var(--primary);text-decoration:none}
    .container{max-width:1400px;margin:0 auto}
    h1{
        font-size:24px;font-weight:700;margin:0 0 4px 0;
        background:linear-gradient(135deg,var(--text),var(--primary));
        -webkit-background-clip:text;-webkit-text-fill-color:transparent;
        background-clip:text
    }
    .subtitle{font-size:13px;color:var(--text3);margin:0 0 16px 0}

    /* ═════════════ 统计卡片 ═════════════ */
    .stats-grid{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
    .stat-card{
        flex:1;min-width:90px;padding:14px 12px;
        background:var(--bg-card);border-radius:var(--radius-lg);text-align:center;
        box-shadow:var(--shadow);transition:transform .2s,box-shadow .2s;
        border:var(--card-border);backdrop-filter:var(--backdrop)
    }
    .stat-card:hover{transform:translateY(-3px);box-shadow:var(--shadow-hover)}
    .stat-card .num{font-size:24px;font-weight:700;color:var(--text);line-height:1.2}
    .stat-card .label{font-size:11px;color:var(--text4);margin-top:4px}
    .status-dot{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:middle}
    .status-online{background:var(--success);box-shadow:0 0 8px rgba(39,174,96,.5)}
    .status-offline{background:var(--danger);box-shadow:0 0 8px rgba(231,76,60,.5)}

    /* ═════════════ 区块卡片 ═════════════ */
    .section{
        background:var(--bg-card);border-radius:var(--radius-lg);padding:18px 20px;
        margin:0 0 14px 0;border:var(--card-border);
        box-shadow:var(--shadow);transition:box-shadow .2s;backdrop-filter:var(--backdrop)
    }
    .section:hover{box-shadow:var(--shadow-hover)}
    .section h2{
        font-size:16px;font-weight:600;margin:0 0 12px 0;color:var(--text);
        padding-bottom:8px;border-bottom:2px solid var(--border)
    }
    .section h3{font-size:14px;font-weight:600;margin:14px 0 8px 0;color:var(--text2)}
    hr{margin:14px 0;border:none;border-top:1px solid var(--border)}

    /* ═════════════ 表格 ═════════════ */
    .table-wrap{overflow-x:auto;margin:8px -2px 10px -2px;border-radius:var(--radius-md)}
    table{width:100%;border-collapse:collapse;font-size:13px;min-width:580px}
    th,td{padding:8px 10px;border:1px solid var(--border);text-align:left;word-break:break-word;vertical-align:middle}
    th{background:var(--table-head-bg);color:var(--table-head-text);font-weight:600;font-size:12px;white-space:nowrap}
    tr:nth-child(even){background:var(--table-even)}
    tr:hover{background:var(--table-hover);transition:background .15s}

    /* ═════════════ 按钮 ═════════════ */
    .btn{
        display:inline-flex;align-items:center;justify-content:center;
        padding:6px 16px;cursor:pointer;background:var(--primary);color:#fff;
        border:none;border-radius:var(--radius-sm);font-size:13px;font-weight:500;
        transition:all .2s;margin:2px;line-height:1.4;white-space:nowrap
    }
    .btn:hover{background:var(--primary2);transform:translateY(-1px);box-shadow:0 2px 8px var(--primary-glow)}
    .btn:active{transform:translateY(0)}
    .btn.danger{background:var(--danger)}.btn.danger:hover{background:#c0392b;box-shadow:0 2px 8px rgba(231,76,60,.3)}
    .btn.warning{background:var(--warning)}.btn.warning:hover{background:#d68910;box-shadow:0 2px 8px rgba(243,156,18,.3)}
    .btn.success{background:var(--success)}.btn.success:hover{background:#1e8449;box-shadow:0 2px 8px rgba(39,174,96,.3)}
    .btn.dark{background:#2c3e50}.btn.dark:hover{background:#1a252f;box-shadow:0 2px 8px rgba(44,62,80,.3)}
    .btn.small{padding:4px 10px;font-size:11px}

    /* ═════════════ 输入框 ═════════════ */
    input,select,textarea{
        padding:6px 10px;border:1px solid var(--border3);border-radius:var(--radius-sm);
        font-size:13px;background:var(--bg-input);color:var(--text);
        transition:border-color .2s,box-shadow .2s;font-family:inherit
    }
    input:focus,select:focus,textarea:focus{
        outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-glow)
    }
    textarea{resize:vertical}
    select{min-width:60px}

    /* ═════════════ 内联表单 ═════════════ */
    .inline-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
    .inline-form label{font-size:13px;color:var(--text5);white-space:nowrap}

    /* ═════════════ 徽章 ═════════════ */
    .badge{display:inline-flex;align-items:center;padding:2px 10px;background:var(--success);color:#fff;border-radius:var(--radius-xl);font-size:11px;font-weight:500}
    .badge-red{background:var(--danger)}.badge-blue{background:var(--primary)}
    .badge-orange{background:var(--warning)}.badge-purple{background:#9b59b6}
    .info{color:var(--text4);font-size:12px;margin:4px 0;line-height:1.5}
    .info-red{color:var(--danger);font-size:12px;margin:4px 0;line-height:1.5;font-weight:500}
    /* ⭐ 提示文字颜色分类 */
    .info-connect{color:var(--info-connect);font-size:12px;margin:4px 0;line-height:1.5;font-weight:500}
    .info-permission{color:var(--info-permission);font-size:12px;margin:4px 0;line-height:1.5;font-weight:500}
    .info-tip{color:var(--info-tip);font-size:12px;margin:4px 0;line-height:1.5;font-weight:500}
    .info-filter{color:var(--info-filter);font-size:12px;margin:4px 0;line-height:1.5;font-weight:500}

    /* ═════════════ 标签 ═════════════ */
    .tag-image,.tag-text,.tag-video{display:inline-flex;align-items:center;justify-content:center;padding:2px 10px;border-radius:var(--radius-xl);font-size:11px;font-weight:500;min-width:48px;white-space:nowrap;line-height:1.5}
    .tag-image{background:#eaf2fd;color:var(--primary);border:1px solid #d4e4f7}
    .tag-text{background:#f0e6f6;color:#9b59b6;border:1px solid #e8d5f0}
    .tag-video{background:#fef5e7;color:#e67e22;border:1px solid #fdebd0}
    .tag-global{background:#eef2f5;color:#2c3e50;padding:1px 8px;border-radius:var(--radius-xl);font-size:10px}
    .tag-limit{background:#fef5e7;color:#e67e22;padding:1px 8px;border-radius:var(--radius-xl);font-size:10px}
    .tag-unlimited{background:#e8f8f0;color:#27ae60;padding:1px 8px;border-radius:var(--radius-xl);font-size:10px}
    .remark{color:var(--text4);font-size:12px}
    .blacklist-row{background:#fef5f5!important}

    /* ═════════════ 选项卡 ═════════════ */
    .tab-bar{
        display:flex;gap:0;margin:0 0 16px 0;border-bottom:2px solid var(--border4);
        overflow-x:auto;flex-wrap:nowrap;-webkit-overflow-scrolling:touch;
        background:var(--bg-card);border-radius:var(--radius-lg) var(--radius-lg) 0 0;
        padding:0 4px;z-index:100
    }
    .tab-bar.sticky{position:sticky;top:0}
    .tab-btn{
        flex-shrink:0;padding:10px 16px;cursor:pointer;background:transparent;
        border:none;border-bottom:2px solid transparent;margin-bottom:-2px;
        font-size:13px;font-weight:500;color:var(--text3);
        transition:all .2s;white-space:nowrap;border-radius:var(--radius-sm) var(--radius-sm) 0 0
    }
    .tab-btn:hover{color:var(--text);background:var(--primary-light)}
    .tab-btn.active{color:var(--primary);border-bottom-color:var(--primary);background:var(--tab-active-bg);font-weight:600}
    .tab-content{display:none}
    .tab-content.active{display:block}
    @keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
    @keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}

    /* ═════════════ Toast ═════════════ */
    .toast{
        position:fixed;bottom:24px;right:24px;background:var(--toast-bg);color:var(--toast-text);
        padding:12px 20px;border-radius:var(--radius-md);z-index:9999;
        box-shadow:0 8px 24px rgba(0,0,0,.25);font-size:13px;
        animation:slideUp .3s ease;display:flex;align-items:center;gap:8px;
        border:1px solid var(--border)
    }
    @keyframes toastIn{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}

    /* ═════════════ 弹窗 ═════════════ */
    .modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:var(--modal-overlay);z-index:10000;justify-content:center;align-items:center}
    .modal-overlay.active{display:flex}
    .modal-box{background:var(--modal-bg);border-radius:var(--radius-lg);padding:20px;width:520px;max-width:90vw;max-height:85vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.15);border:var(--card-border)}
    .modal-box h3{font-size:16px;margin:0 0 12px 0;color:var(--text)}
    .modal-box .field{margin-bottom:10px}
    .modal-box .field label{display:block;font-size:12px;color:var(--text4);margin-bottom:3px}
    .modal-box .field input,.modal-box .field select{width:100%}
    .modal-box .modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}

    /* ═════════════ 节点行 ═════════════ */
    .node-row{display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid var(--border)}
    .node-row .node-type-tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:4px;min-width:48px;text-align:center}
    .node-row .node-type-tag.positive{background:#eaf2fd;color:#3498db}
    .node-row .node-type-tag.latent{background:#e8f8f0;color:#27ae60}
    .node-row .node-type-tag.image_input{background:#fef5e7;color:#e67e22}
    .node-row .node-type-tag.video_width{background:#e0f5f5;color:#0d7a7a}
    .node-row .node-type-tag.video_height{background:#f0e6fa;color:#7b2d8e}
    .node-row .node-type-tag.duration{background:#fde8f0;color:#c2185b}
    .node-row input{flex:1;width:auto!important;min-width:60px}
    .node-row select{width:auto!important;min-width:80px}

    /* ═════════════ 设置卡片 ═════════════ */
    .settings-card{background:var(--bg-card2);border:var(--card-border);border-radius:var(--radius-md);padding:14px 16px;margin-bottom:10px;backdrop-filter:var(--backdrop)}
    .settings-card h4{font-size:14px;font-weight:600;margin:0 0 6px 0;color:var(--text)}
    .settings-card .card-desc{font-size:12px;color:var(--text4);margin:0 0 8px 0}
    .settings-card .card-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0}
    .settings-card .card-row label{font-size:12px;color:var(--text3);white-space:nowrap;min-width:56px}

    /* ═════════════ 工作流卡片 ═════════════ */
    .wf-card-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:10px 0}
    .wf-card{background:var(--bg-card);border:var(--card-border);border-radius:var(--radius-md);padding:14px 16px;transition:box-shadow .2s,border-color .2s;display:flex;flex-direction:column;gap:8px;backdrop-filter:var(--backdrop)}
    .wf-card:hover{box-shadow:var(--shadow-hover);border-color:var(--primary)}
    .wf-card .wf-card-header{display:flex;justify-content:space-between;align-items:center}
    .wf-card .wf-card-name{font-weight:600;font-size:14px;color:var(--text)}
    .wf-card .wf-card-file{font-size:11px;color:var(--text4);word-break:break-all}
    .wf-card .wf-card-nodes{font-size:12px;display:flex;flex-wrap:wrap;gap:3px}
    .wf-card .wf-card-note,.wf-card .wf-card-example{font-size:12px}
    .wf-card .wf-card-actions{display:flex;gap:6px;justify-content:flex-end;margin-top:4px}

    /* ═════════════ 节点标签 ═════════════ */
    .node-tag{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;white-space:nowrap}
    .node-tag.positive{background:#eaf2fd;color:#3498db}
    .node-tag.latent{background:#e8f8f0;color:#27ae60}
    .node-tag.image_input{background:#fef5e7;color:#e67e22}
    .node-tag.video_width{background:#e0f5f5;color:#0d7a7a}
    .node-tag.video_height{background:#f0e6fa;color:#7b2d8e}
    .node-tag.duration{background:#fde8f0;color:#c2185b}
    .node-tag.note{background:#f0e6f6;color:#7b6f9e}
    .node-tag.example{background:#e3f0fb;color:#5b8cc7}
    .node-tag.note::before{content:'📝 ';font-size:10px}
    .node-tag.example::before{content:'💡 ';font-size:10px}

    /* ═════════════ 触发词 ═════════════ */
    .trigger-grid{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}
    .trigger-chip{background:var(--chip-bg);border:1px solid var(--chip-border);border-radius:var(--radius-md);padding:10px 14px;text-align:center;min-width:120px;transition:box-shadow .2s,border-color .2s;display:flex;flex-direction:column;align-items:center;gap:6px;backdrop-filter:var(--backdrop)}
    .trigger-chip:hover{box-shadow:var(--shadow-hover);border-color:var(--primary)}
    .trigger-chip .trigger-word{font-weight:700;font-size:15px;color:#fff;background:var(--trigger-grad);padding:6px 16px;border-radius:var(--radius-xl);cursor:default}
    .trigger-chip .trigger-arrow{color:var(--text4);font-size:11px}
    .trigger-chip .trigger-wf{font-size:12px;color:var(--text5);font-weight:500}
    .trigger-chip .trigger-usage{font-size:11px}
    .trigger-chip .trigger-usage select{font-size:11px;padding:2px 6px;min-width:auto}
    .trigger-chip .trigger-usage input{font-size:11px;padding:2px 6px;width:56px}
    .trigger-add-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px;background:var(--bg-card2);border-radius:var(--radius-md);margin-top:4px}

    /* ═════════════ 其他 ═════════════ */
    .usage-row{background:var(--bg-card2);padding:10px 12px;border-radius:var(--radius-md);margin:6px 0;display:flex;align-items:center;flex-wrap:wrap;gap:8px;border:var(--card-border)}
    .usage-inline{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
    .checkbox-group{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0}
    .checkbox-group label{cursor:pointer;font-size:13px;display:flex;align-items:center;gap:4px;padding:4px 10px;background:var(--bg-card2);border-radius:var(--radius-sm);border:1px solid var(--border);transition:all .15s}
    .checkbox-group label:hover{border-color:var(--primary);background:var(--primary-light)}
    .checkbox-group input[type=checkbox]{margin:0;width:16px;height:16px;accent-color:var(--primary)}
    .wordbank-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin:10px 0}
    .wordbank-cat{background:var(--bg-card2);padding:10px 12px;border-radius:var(--radius-md);border:1px solid var(--border);transition:border-color .15s}
    .wordbank-cat:hover{border-color:var(--primary)}
    .wordbank-cat h4{margin:0 0 5px 0;font-size:13px;color:var(--text);font-weight:600}
    .wordbank-tag{display:inline-block;padding:1px 8px;margin:2px;border-radius:4px;font-size:11px;line-height:1.6}
    .wordbank-tag.sfw{background:#e6f7ec;color:#1a7a3a;border:1px solid #c3e6cb}
    .wordbank-tag.nsfw{background:#fde8e8;color:#a52828;border:1px solid #f5c6cb}
    .pagination{display:flex;gap:10px;align-items:center;justify-content:center;margin:12px 0;flex-wrap:wrap}
    .preset-box{background:var(--bg-card2);padding:10px 14px;border-radius:var(--radius-md);margin:6px 0;display:flex;align-items:center;flex-wrap:wrap;gap:10px;border:1px solid var(--border)}
    .preset-box .preset-name{font-weight:600;min-width:70px;font-size:13px}
    .preset-box .preset-words{color:var(--text5);font-size:12px;flex:1;min-width:100px;word-break:break-word}

    /* ═════════════ 权限管理卡片 ═════════════ */
    .perm-card{background:var(--bg-card2);border:var(--card-border);border-radius:var(--radius-md);padding:14px 16px;margin-bottom:10px;backdrop-filter:var(--backdrop);border-left:3px solid var(--border)}
    .perm-card.perm-access{border-left-color:var(--primary)}
    .perm-card.perm-members{border-left-color:var(--warning)}
    .perm-card.perm-admin{border-left-color:#9b59b6}
    .perm-card h4{font-size:14px;font-weight:600;margin:0 0 6px 0;color:var(--text);padding-bottom:6px;border-bottom:1px solid var(--border)}
    .perm-card .perm-row{display:flex;gap:12px;flex-wrap:wrap}
    .perm-card .perm-col{flex:1;min-width:280px}
    .perm-card .perm-col h5{font-size:12px;font-weight:600;color:var(--text3);margin:4px 0 6px 0}
    .perm-card .perm-inline{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0}
    .perm-card .perm-inline label{font-size:12px;color:var(--text5);white-space:nowrap}
    .perm-card .table-wrap table{min-width:auto}
    .perm-card .table-wrap th{font-size:11px}
    .perm-card .table-wrap td{font-size:12px}

    /* ═════════════ 过滤卡片 ═════════════ */
    .filter-card{background:var(--bg-card2);border:var(--card-border);border-radius:var(--radius-md);padding:14px 16px;margin-bottom:10px;backdrop-filter:var(--backdrop)}
    .filter-card h4{font-size:14px;font-weight:600;margin:0 0 6px 0;color:var(--text)}
    .filter-card .card-desc{font-size:12px;color:var(--text4);margin:0 0 8px 0}
    .filter-status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:middle}
    .filter-status-dot.on{background:var(--success);box-shadow:0 0 6px rgba(39,174,96,.4)}
    .filter-status-dot.off{background:var(--danger);box-shadow:0 0 6px rgba(231,76,60,.4)}
    .filter-preset-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-md);padding:10px 14px;margin:6px 0;display:flex;align-items:center;flex-wrap:wrap;gap:8px;transition:border-color .15s,box-shadow .15s}
    .filter-preset-card:hover{border-color:var(--primary);box-shadow:var(--shadow-hover)}
    .filter-preset-card .preset-name{font-weight:600;font-size:13px;color:var(--text);min-width:60px}
    .filter-preset-card .preset-tags{display:flex;flex-wrap:wrap;gap:3px;flex:1;min-width:120px}
    .filter-preset-card .preset-tags .wordbank-tag{font-size:11px;padding:1px 8px;margin:1px;border-radius:4px;line-height:1.5;background:#ede9fe;color:#6d28d9;border:1px solid #ddd6fe}
    .filter-preset-card .preset-actions{display:flex;gap:4px;flex-shrink:0}
    .perm-empty{color:var(--text4);font-size:12px;font-style:italic;padding:8px 0}

    /* ═════════════ 回到顶部 ═════════════ */
    #backToTop{
        position:fixed;bottom:80px;right:24px;width:40px;height:40px;
        background:var(--primary);color:#fff;border:none;
        border-radius:50%;cursor:pointer;font-size:18px;
        display:none;align-items:center;justify-content:center;
        z-index:9998;box-shadow:0 4px 12px rgba(0,0,0,.2);
        transition:transform .2s,opacity .2s
    }
    #backToTop:hover{transform:scale(1.1)}
    #backToTop.show{display:flex}

    /* ═════════════ 皮肤切换按钮 ═════════════ */
    #themeSwitcher{
        position:fixed;bottom:24px;right:24px;width:44px;height:44px;
        background:var(--primary);color:#fff;border:none;
        border-radius:50%;cursor:pointer;font-size:20px;
        z-index:9998;box-shadow:0 4px 16px rgba(0,0,0,.25);
        transition:transform .2s;display:flex;align-items:center;justify-content:center
    }
    #themeSwitcher:hover{transform:scale(1.1)}
    #themePanel{
        position:fixed;bottom:76px;right:24px;background:var(--bg-card);
        border:var(--card-border);border-radius:var(--radius-md);
        padding:14px;z-index:9997;box-shadow:0 8px 32px rgba(0,0,0,.2);
        display:none;min-width:200px;backdrop-filter:var(--backdrop)
    }
    #themePanel.show{display:block;animation:slideUp .2s ease}
    #themePanel h4{font-size:13px;margin:0 0 8px 0;color:var(--text)}
    #themePanel .theme-option{
        display:flex;align-items:center;gap:8px;padding:6px 10px;
        cursor:pointer;border-radius:var(--radius-sm);font-size:12px;
        color:var(--text);transition:background .15s;margin:2px 0
    }
    #themePanel .theme-option:hover{background:var(--primary-light)}
    #themePanel .theme-option.active{background:var(--primary-light);font-weight:600}
    #themePanel .theme-dot{
        width:16px;height:16px;border-radius:50%;flex-shrink:0;
        border:2px solid var(--border3)
    }
    #themePanel .theme-dot.d1{background:linear-gradient(135deg,#3498db,#2980b9)}
    #themePanel .theme-dot.d2{background:linear-gradient(135deg,#818cf8,#6366f1)}
    #themePanel .theme-dot.d3{background:linear-gradient(135deg,#6366f1,#4f46e5)}
    #themePanel .theme-dot.d4{background:linear-gradient(135deg,#10b981,#059669)}
    #themePanel .theme-dot.d5{background:linear-gradient(135deg,#f59e0b,#d97706)}
    #themePanel .theme-divider{height:1px;background:var(--border);margin:8px 0}
    #themePanel .theme-toggle{
        display:flex;align-items:center;justify-content:space-between;
        padding:4px 6px;font-size:12px;color:var(--text4)
    }
    #themePanel .theme-toggle input{margin:0;width:14px;height:14px;accent-color:var(--primary)}

    /* ═════════════ 美化增强 ═════════════ */
    /* 统计卡片 emoji */
    .stat-card:nth-child(1) .label::before{content:'🎨 '}
    .stat-card:nth-child(2) .label::before{content:'📝 '}
    .stat-card:nth-child(3) .label::before{content:'👥 '}
    .stat-card:nth-child(4) .label::before{content:'📦 '}
    .stat-card:nth-child(5) .label::before{content:'🤖 '}
    .stat-card:nth-child(6) .label::before{content:'🐱 '}

    /* 卡片淡入（默认皮肤不启用） */
    body.anim-cards .section, body.anim-cards .stat-card, body.anim-cards .wf-card, body.anim-cards .settings-card, body.anim-cards .perm-card, body.anim-cards .filter-card{
        animation:fadeIn .4s ease both
    }
    body.anim-cards .section:nth-child(1){animation-delay:0s}
    body.anim-cards .section:nth-child(2){animation-delay:.05s}
    body.anim-cards .section:nth-child(3){animation-delay:.1s}
    body.anim-cards .stat-card:nth-child(1){animation-delay:0s}
    body.anim-cards .stat-card:nth-child(2){animation-delay:.04s}
    body.anim-cards .stat-card:nth-child(3){animation-delay:.08s}
    body.anim-cards .stat-card:nth-child(4){animation-delay:.12s}
    body.anim-cards .stat-card:nth-child(5){animation-delay:.16s}
    body.anim-cards .stat-card:nth-child(6){animation-delay:.2s}

    /* 选项卡切换动画 */
    body.anim-tabs .tab-content.active{animation:fadeIn .25s ease}

    /* 滚动条 */
    ::-webkit-scrollbar{width:8px;height:8px}
    ::-webkit-scrollbar-track{background:var(--scrollbar-track)}
    ::-webkit-scrollbar-thumb{background:var(--scrollbar-thumb);border-radius:4px}
    ::-webkit-scrollbar-thumb:hover{background:var(--primary)}

    /* ═════════════ 响应式 ═════════════ */
    @media(max-width:1200px){.wf-card-grid{grid-template-columns:repeat(2,1fr)}}
    @media(max-width:900px){
        body{padding:12px}.container{max-width:100%}
        .stats-grid .stat-card{min-width:70px;padding:10px 8px}
        .stat-card .num{font-size:20px}
        .tab-btn{padding:8px 12px;font-size:12px}
        .inline-form{gap:6px}
        .wordbank-grid{grid-template-columns:1fr}
        .wf-card-grid{grid-template-columns:repeat(2,1fr)}
        .perm-card .perm-row{flex-direction:column}
        .perm-card .perm-col{min-width:auto}
        #themeSwitcher{bottom:16px;right:16px;width:40px;height:40px}
        #themePanel{bottom:64px;right:16px}
    }
    @media(max-width:768px){.wf-card-grid{grid-template-columns:1fr}}
    @media(max-width:600px){
        body{padding:8px}
        .section{padding:12px;border-radius:var(--radius-md)}
        h1{font-size:20px}
        .stats-grid{gap:8px}
        .stat-card{min-width:60px;padding:8px 6px}
        .stat-card .num{font-size:17px}
        .stat-card .label{font-size:10px}
        .tab-bar{gap:0;overflow-x:auto}
        .tab-btn{padding:8px 10px;font-size:12px}
        .inline-form{flex-direction:column;align-items:stretch}
        .inline-form label{white-space:normal;font-size:12px}
        input,select,textarea{width:100%;font-size:13px}
        .btn{width:100%;justify-content:center}
        .checkbox-group{flex-direction:column;gap:6px}
        .checkbox-group label{width:100%}
        .usage-row{flex-direction:column;align-items:stretch;gap:6px}
        .usage-inline{flex-direction:column;align-items:stretch}
        .preset-box{flex-direction:column;align-items:stretch}
        .filter-preset-card{flex-direction:column;align-items:stretch}
        .perm-card .perm-row{flex-direction:column}
        .perm-card .perm-col{min-width:auto}
        .wordbank-grid{grid-template-columns:1fr}
        .wf-card-grid{grid-template-columns:1fr}
        .trigger-chip{min-width:100px;flex:1}
        .table-wrap{overflow-x:auto}
        table{font-size:12px;min-width:480px}
        th,td{padding:6px 8px}
        .toast{bottom:12px;right:12px;left:12px;text-align:center;font-size:12px}
        .badge{font-size:10px;padding:1px 8px}
    }
</style>"""
# =====================================================================
# 管理后台 HTML 模板 - HTML 主体部分
# =====================================================================
ADMIN_HTML_BODY = r"""</head><body>
<div class="container">
<h1>&#127912; ArtLink v2.0</h1>
<p class="subtitle">ComfyUI QQ 机器人 · 单文件部署 · 可视化管理</p>

<div class="stats-grid">
<div class="stat-card"><div class="num">{{stats.today_images}}</div><div class="label">今日生图</div></div>
<div class="stat-card"><div class="num">{{stats.today_texts}}</div><div class="label">今日反推</div></div>
<div class="stat-card"><div class="num">{{stats.today_users}}</div><div class="label">活跃用户</div></div>
<div class="stat-card"><div class="num">{{stats.total_images}}</div><div class="label">累计生图</div></div>
<div class="stat-card"><div class="num"><span class="status-dot {% if stats.comfyui_online %}status-online{% else %}status-offline{% endif %}"></span></div><div class="label">ComfyUI {% if stats.comfyui_online %}在线{% else %}离线{% endif %} <button class="btn small" onclick="checkComfyui()" style="font-size:9px;padding:1px 6px;margin-left:2px">检测</button></div></div>
<div class="stat-card"><div class="num"><span class="status-dot {% if stats.napcat_online %}status-online{% else %}status-offline{% endif %}"></span></div><div class="label">NapCat {% if stats.napcat_online %}在线{% else %}离线{% endif %} <button class="btn small" onclick="checkNapcat()" style="font-size:9px;padding:1px 6px;margin-left:2px">检测</button></div></div>
</div>

<div class="tab-bar">
    <div class="tab-btn active" data-tab="tab_system" onclick="switchTab('tab_system',this)">&#9881;&#65039; 系统配置</div>
    <div class="tab-btn" data-tab="tab_permission" onclick="switchTab('tab_permission',this)">&#128274; 权限管理</div>
    <div class="tab-btn" data-tab="tab_workflow" onclick="switchTab('tab_workflow',this)">&#128295; 工作流管理</div>
    <div class="tab-btn" data-tab="tab_filter" onclick="switchTab('tab_filter',this)">&#128481;&#65039; 提示词过滤</div>
    <div class="tab-btn" data-tab="tab_random" onclick="switchTab('tab_random',this)">&#127922; 随便来点</div>
</div>

<!-- 选项卡1：系统配置 -->
<div id="tab_system" class="tab-content active">
<div class="section"><h2>&#9881;&#65039; 系统设置</h2>

<div class="settings-card">
    <h4>&#128268; 连接配置</h4>
    <p class="card-desc">ArtLink 与 ComfyUI、NapCat 之间的通讯地址</p>
    <div class="card-row">
        <label>ComfyUI 输入:</label>
        <input type="text" id="comfyui_input_dir" value="{{config.comfyui_input_dir}}" size="40">
        <button class="btn small" onclick="updateConfig('comfyui_input_dir')">保存</button>
        <span class="info-connect">反推图片存放目录</span>
    </div>
    <div class="card-row">
        <label>ComfyUI 地址:</label>
        <input type="text" id="comfyui_server" value="{{config.comfyui_server}}" size="30">
        <button class="btn small" onclick="updateConfig('comfyui_server')">保存</button>
        <span class="info-connect">用于提交生成任务、轮询结果和中断任务的 API 地址</span>
    </div>
    <div class="card-row">
        <label>NapCat HTTP:</label>
        <input type="text" id="napcat_http" value="{{config.napcat_http}}" size="30">
        <button class="btn small" onclick="updateConfig('napcat_http')">保存</button>
        <span class="info-connect">← 发送消息/调用API</span>
    </div>
    <div class="card-row">
        <label>NapCat WS:</label>
        <input type="text" id="napcat_ws" value="{{config.napcat_ws}}" size="30">
        <button class="btn small" onclick="updateConfig('napcat_ws')">保存</button>
        <span class="info-connect">← 接收QQ消息</span>
    </div>
</div>

<div class="settings-card">
    <h4>&#128273; 使用控制</h4>
    <p class="card-desc">限制使用频率与访问权限</p>
    <div class="card-row">
        <label>每日上限:</label>
        <input type="number" id="max_daily_count" value="{{config.max_daily_count}}" style="width:70px"> 次
        <button class="btn small" onclick="updateConfig('max_daily_count')">保存</button>
        <span class="info-permission">仅限制群聊，私聊无限制；-1 为无限制</span>
    </div>
    <div class="card-row">
        <label>私聊白名单:</label>
        <select id="private_whitelist_enabled" onchange="updateConfig('private_whitelist_enabled')">
            <option value="true" {% if config.private_whitelist_enabled=='true' %}selected{% endif %}>开启</option>
            <option value="false" {% if config.private_whitelist_enabled=='false' %}selected{% endif %}>关闭</option>
        </select>
        <span class="info-permission">关闭后任何QQ号都可私聊机器人</span>
    </div>
    <div class="card-row">
        <label>群聊白名单:</label>
        <select id="group_whitelist_enabled" onchange="updateConfig('group_whitelist_enabled')">
            <option value="true" {% if config.group_whitelist_enabled=='true' %}selected{% endif %}>开启</option>
            <option value="false" {% if config.group_whitelist_enabled=='false' %}selected{% endif %}>关闭</option>
        </select>
        <span class="info-permission">关闭后所有群消息静默不理</span>
    </div>
</div>

<div class="settings-card">
    <h4>&#128172; 帮助说明</h4>
    <p class="card-desc">用户发送触发词（？/帮助/help 等）时展示的文本内容</p>
    <div class="card-row" style="flex-direction:column;align-items:stretch">
        <label>公共说明（所有人可见）:</label>
        <textarea id="public_help" rows="8" style="width:100%;max-width:100%">{{config.public_help}}</textarea>
        <button class="btn small" onclick="updateConfig('public_help')">保存</button>
    </div>
    <div class="card-row" style="flex-direction:column;align-items:stretch;margin-top:8px">
        <label>管理员说明（管理员私聊发送 ?/帮助 可见，留空用系统默认）:</label>
        <textarea id="admin_help" rows="6" style="width:100%;max-width:100%" placeholder="留空则使用系统默认管理员菜单说明">{{config.admin_help or ''}}</textarea>
        <button class="btn small" onclick="updateConfig('admin_help')">保存</button>
    </div>
    <div class="card-row">
        <label>触发词:</label>
        <input type="text" id="help_triggers" value="{{config.help_triggers}}" size="50" placeholder="逗号分隔">
        <button class="btn small" onclick="updateConfig('help_triggers')">保存</button>
        <span class="info-tip">多个用英文逗号分隔，管理员看到内容与普通用户不同</span>
    </div>
</div>

<div class="settings-card">
    <h4>&#128257; 再来一张</h4>
    <p class="card-desc">用上一次生图的提示词再生成，无需重新输入（仅追溯图片任务）</p>
    <div class="card-row">
        <label>私聊:</label>
        <select id="regen_private_enabled" onchange="updateConfig('regen_private_enabled')"><option value="true" {% if config.regen_private_enabled=='true' %}selected{% endif %}>开启</option><option value="false" {% if config.regen_private_enabled=='false' %}selected{% endif %}>关闭</option></select>
        <span class="info-tip">支持1-5张（发送「再来3张」）</span>
    </div>
    <div class="card-row">
        <label>群聊:</label>
        <select id="regen_group_enabled" onchange="updateConfig('regen_group_enabled')"><option value="true" {% if config.regen_group_enabled=='true' %}selected{% endif %}>开启</option><option value="false" {% if config.regen_group_enabled=='false' %}selected{% endif %}>关闭</option></select>
        <span class="info-tip">仅支持1张（无需@机器人）</span>
    </div>
    <div class="card-row">
        <label>触发词:</label>
        <input type="text" id="regen_triggers" value="{{config.regen_triggers}}" size="40" placeholder="逗号分隔">
        <button class="btn small" onclick="updateConfig('regen_triggers')">保存</button>
    </div>
</div>

<!-- 停止任务设置卡片 -->
<div class="settings-card">
    <h4>&#128477; 停止任务</h4>
    <p class="card-desc">发送触发词即可停止自己当前正在进行的任务</p>
    <div class="card-row">
        <label>触发词:</label>
        <input type="text" id="stop_triggers" value="{{config.stop_triggers}}" size="30" placeholder="逗号分隔">
        <button class="btn small" onclick="updateConfig('stop_triggers')">保存</button>
        <span class="info-tip">多个用英文逗号分隔</span>
    </div>
    <div class="card-row">
        <label>私聊:</label>
        <select id="stop_private_enabled" onchange="updateConfig('stop_private_enabled')">
            <option value="true" {% if config.stop_private_enabled=='true' %}selected{% endif %}>开启</option>
            <option value="false" {% if config.stop_private_enabled=='false' %}selected{% endif %}>关闭</option>
        </select>
        <label style="margin-left:12px">群聊:</label>
        <select id="stop_group_enabled" onchange="updateConfig('stop_group_enabled')">
            <option value="true" {% if config.stop_group_enabled=='true' %}selected{% endif %}>开启</option>
            <option value="false" {% if config.stop_group_enabled=='false' %}selected{% endif %}>关闭</option>
        </select>
        <span class="info-tip">群聊无需@机器人，仅停止自己任务</span>
    </div>
</div>

<div class="settings-card">
    <h4>&#127912; 绘图模式</h4>
    <p class="card-desc">私聊免触发词连续生图，发送提示词即出图，超时自动退出</p>
    <div class="card-row">
        <label>进入:</label>
        <input type="text" id="drawing_mode_trigger" value="{{config.drawing_mode_trigger}}" size="16">
        <button class="btn small" onclick="updateConfig('drawing_mode_trigger')">保存</button>
        <label style="margin-left:12px">超时:</label>
        <input type="number" id="drawing_mode_duration" value="{{config.drawing_mode_duration}}" style="width:60px"> 分钟
        <button class="btn small" onclick="updateConfig('drawing_mode_duration')">保存</button>
    </div>
</div>

<div class="settings-card">
    <h4>&#128203; 返回内容</h4>
    <p class="card-desc">控制生图完成后返图附带哪些信息</p>
    <div class="card-row" style="flex-direction:column;align-items:flex-start;gap:4px">
        <span style="font-size:12px;font-weight:500">私聊显示：</span>
        <div class="checkbox-group" style="margin:0">
            <label><input type="checkbox" id="disp_prompt" {% if 'prompt' in config.private_display_flags %}checked{% endif %} onchange="savePrivateDisplayFlags()"> 提示词</label>
            <label><input type="checkbox" id="disp_size" {% if 'size' in config.private_display_flags %}checked{% endif %} onchange="savePrivateDisplayFlags()"> 尺寸</label>
            <label><input type="checkbox" id="disp_duration" {% if 'duration' in config.private_display_flags %}checked{% endif %} onchange="savePrivateDisplayFlags()"> 耗时</label>
            <label><input type="checkbox" id="disp_workflow" {% if 'workflow' in config.private_display_flags %}checked{% endif %} onchange="savePrivateDisplayFlags()"> 工作流名</label>
        </div>
    </div>
    <div class="card-row" style="flex-direction:column;align-items:flex-start;gap:4px;margin-top:6px">
        <span style="font-size:12px;font-weight:500">群聊显示：</span>
        <div class="checkbox-group" style="margin:0">
            <label><input type="checkbox" id="gdisp_prompt" {% if 'prompt' in config.group_display_flags %}checked{% endif %} onchange="saveGroupDisplayFlags()"> 提示词</label>
            <label><input type="checkbox" id="gdisp_size" {% if 'size' in config.group_display_flags %}checked{% endif %} onchange="saveGroupDisplayFlags()"> 尺寸</label>
            <label><input type="checkbox" id="gdisp_duration" {% if 'duration' in config.group_display_flags %}checked{% endif %} onchange="saveGroupDisplayFlags()"> 耗时</label>
            <label><input type="checkbox" id="gdisp_workflow" {% if 'workflow' in config.group_display_flags %}checked{% endif %} onchange="saveGroupDisplayFlags()"> 工作流名</label>
        </div>
    </div>
</div>
</div>
</div>

<!-- 选项卡2：权限管理 -->
<div id="tab_permission" class="tab-content">
<div class="section"><h2>&#128274; 权限管理</h2>

<!-- 卡片1：访问控制（私聊+群聊 双栏并排） -->
<div class="perm-card perm-access">
    <h4>&#128100; 访问控制</h4>
    <div class="perm-row">
        <div class="perm-col">
            <h5>私聊白名单 <span class="badge">{{private_count}}人</span></h5>
            <div class="perm-inline"><input type="text" id="private_qq" placeholder="QQ号"><input type="text" id="private_remark" placeholder="备注" style="width:120px"><button class="btn small" onclick="addPrivateQQ()">添加</button></div>
            <div class="table-wrap"><table><tr><th>QQ号</th><th>备注</th><th>操作</th></tr>{% for p in private_list %}<tr><td>{{p.qq}}</td><td><span class="remark">{{p.remark or ''}}</span></td><td><button class="btn success small" onclick="editPrivateRemark('{{p.qq}}')">备注</button><button class="btn danger small" onclick="delPrivateQQ('{{p.qq}}')">删除</button></td></tr>{% endfor %}</table></div>
            {% if not private_list %}<div class="perm-empty">暂无数据，通过上方表单添加</div>{% endif %}
        </div>
        <div class="perm-col">
            <h5>群聊白名单 <span class="badge">{{group_count}}个群</span></h5>
            <div class="perm-inline"><input type="text" id="group_id" placeholder="群号"><input type="text" id="group_remark" placeholder="备注" style="width:120px"><button class="btn small" onclick="addGroup()">添加群</button></div>
            <div class="table-wrap"><table><tr><th>群号</th><th>备注</th><th>状态</th><th>操作</th></tr>{% for g in group_list %}<tr><td>{{g.group_id}}</td><td><span class="remark">{{g.remark or ''}}</span></td><td><select onchange="toggleGroupEnabled('{{g.group_id}}',this.value)" style="font-size:11px"><option value="1" {% if g.enabled!=0 %}selected{% endif %}>开启</option><option value="0" {% if g.enabled==0 %}selected{% endif %}>关闭</option></select></td><td><button class="btn success small" onclick="editGroupRemark('{{g.group_id}}')">备注</button><button class="btn danger small" onclick="delGroup('{{g.group_id}}')">删除</button></td></tr>{% endfor %}</table></div>
            {% if not group_list %}<div class="perm-empty">暂无数据，通过上方表单添加</div>{% endif %}
        </div>
    </div>
</div>

<!-- 卡片2：成员管理（群成员+黑名单 上下分区） -->
<div class="perm-card perm-members">
    <h4>&#128101; 群成员白名单</h4>
    <div class="perm-inline"><label>选择群:</label><select id="member_group_select" onchange="loadMembers()"><option value="">--选择--</option>{% for g in group_list %}<option value="{{g.group_id}}">{{g.group_id}}{% if g.remark %} ({{g.remark}}){% endif %}</option>{% endfor %}</select><input type="text" id="member_qq" placeholder="QQ号"><input type="text" id="member_remark" placeholder="备注" style="width:120px"><button class="btn small" onclick="addMember()">添加</button><button class="btn warning small" onclick="resetGroupUsage()">重置群次数</button></div>
    <div class="table-wrap"><table id="member_table"><tr><th>QQ号</th><th>备注</th><th>今日已用</th><th>操作</th></tr></table></div>
    <hr style="margin:10px 0">
    <h4 style="cursor:pointer" onclick="toggleBlacklist()">&#128683; 黑名单 <span class="badge badge-red">{{blacklist_count}}人</span> <span id="blacklist_arrow" style="font-size:14px;margin-left:8px">&#9660;</span></h4>
    <div id="blacklist_body">
    <div class="perm-inline"><label>选择群:</label><select id="blacklist_group_select" onchange="loadBlacklist()"><option value="">--选择--</option>{% for g in group_list %}<option value="{{g.group_id}}">{{g.group_id}}{% if g.remark %} ({{g.remark}}){% endif %}</option>{% endfor %}</select></div>
    <div class="table-wrap"><table id="blacklist_table"><tr><th>QQ号</th><th>备注</th><th>操作</th></tr></table></div>
    </div>
</div>

<!-- 卡片3：管理员 & 历史 -->
<div class="perm-card perm-admin">
    <h4>&#128081; 管理员 <span class="badge">{{admin_count}}人</span></h4>
    <div class="perm-inline"><input type="text" id="admin_qq" placeholder="QQ号"><input type="text" id="admin_remark" placeholder="备注" style="width:120px"><button class="btn small" onclick="addAdmin()">添加</button></div>
    <div class="table-wrap"><table><tr><th>QQ号</th><th>备注</th><th>操作</th></tr>{% for a in admin_list %}<tr><td>{{a.qq}}</td><td><span class="remark">{{a.remark or ''}}</span></td><td><button class="btn danger small" onclick="delAdmin('{{a.qq}}')">删除</button></td></tr>{% endfor %}</table></div>
    {% if not admin_list %}<div class="perm-empty">暂无管理员</div>{% endif %}
    <hr style="margin:10px 0">
    <h4 style="cursor:pointer" onclick="toggleHistory()">&#128203; 生成历史 <span class="badge badge-blue">{{history_count}}条</span> <span id="history_arrow" style="font-size:14px;margin-left:8px">&#9660;</span></h4>
    <div id="history_body">
    <div class="perm-inline">
        <label>状态:</label><select id="history_status" onchange="searchHistory()"><option value="">全部</option><option value="成功">成功</option><option value="失败">失败</option><option value="进行中">进行中</option></select>
        <input type="text" id="history_search" placeholder="QQ/群号搜索" style="width:200px">
        <button class="btn" onclick="searchHistory(1)">搜索</button>
        <button class="btn danger small" onclick="confirmClearHistory()">清空</button>
    </div>
    <div class="table-wrap"><table id="history_table"><tr><th>时间</th><th>QQ</th><th>群</th><th>类型</th><th>工作流</th><th>尺寸</th><th>耗时</th><th>状态</th></tr></table></div>
    <div class="pagination" id="history_pager"></div>
    </div>
</div>

</div>
</div>

<!-- 选项卡3：工作流管理 -->
<div id="tab_workflow" class="tab-content">
<div class="section">
    <h2>&#127919; 触发词 &amp; 用量限制 <span class="badge">{{trigger_data|length}}个</span></h2>
    <p class="info">每一个触发词对应一个工作流，可随时调整用量模式</p>
    <div class="trigger-grid">
    {% for td in trigger_data %}
    <div class="trigger-chip">
        <span class="trigger-word">{{td.word}}</span>
        <span class="trigger-arrow">&#8594;</span>
        <span class="trigger-wf">{{td.workflow}}</span>
        <div class="trigger-usage">
            <select id="wu_mode_{{td.workflow}}" onchange="onTriggerChipModeChange('{{td.workflow}}')">
                <option value="global" {% if td.usage_mode=='global' %}selected{% endif %}>跟随上限</option>
                <option value="limited" {% if td.usage_mode=='limited' %}selected{% endif %}>单独上限</option>
                <option value="unlimited" {% if td.usage_mode=='unlimited' %}selected{% endif %}>无限制</option>
            </select>
            <input type="number" id="wu_limit_{{td.workflow}}" value="{{td.usage_limit if td.usage_mode=='limited' else ''}}" placeholder="次数" style="width:56px;{% if td.usage_mode!='limited' %}display:none{% endif %}" onchange="saveTriggerUsage('{{td.workflow}}')">
        </div>
        <button class="btn danger small" onclick="delTrigger('{{td.word}}')">&#128465;</button>
    </div>
    {% endfor %}
    {% if not trigger_data %}<span class="info">暂无触发词，请在下方添加</span>{% endif %}
    </div>
    <div class="trigger-add-row">
        <input type="text" id="trigger_word" placeholder="触发词（如：绘图）" size="14">
        <select id="trigger_wf_select">
            {% for wf in workflows %}
            <option value="{{wf.name}}">{{wf.name}}</option>
            {% endfor %}
        </select>
        <select id="trigger_usage_mode_select">
            <option value="global">跟随上限</option>
            <option value="limited">单独上限</option>
            <option value="unlimited">无限制</option>
        </select>
        <input type="number" id="trigger_usage_limit_val" value="" placeholder="次数" style="width:60px;display:none">
        <button class="btn success small" onclick="addTriggerWithUsage()">&#10133; 添加</button>
    </div>
</div>

<div class="section">
    <h2>&#128193; 工作流 <span class="badge">{{workflows|length}}个</span></h2>
    <div class="wf-card-grid">
    {% for wf in workflows %}
    <div class="wf-card">
        <div class="wf-card-header">
            <span class="wf-card-name">{{wf.name}}</span>
            {% if wf.output_type=='video' %}<span class="tag-video">&#127916; 视频</span>{% elif wf.output_type=='text' %}<span class="tag-text">&#128221; 文字</span>{% else %}<span class="tag-image">&#128444; 图片</span>{% endif %}
        </div>
        <span class="wf-card-file">&#128193; {{wf.file_path}}</span>
        <div class="wf-card-nodes">
            {% for nt in wf.nodes_tags %}
            <span class="node-tag {{nt.type}}">{{nt.label}}</span>
            {% endfor %}
            {% if not wf.nodes_tags %}<span class="info">无节点配置</span>{% endif %}
        </div>
        {% if wf.note %}
        <div class="wf-card-note"><span class="node-tag note">{{wf.note}}</span></div>
        {% endif %}
        {% if wf.example %}
        <div class="wf-card-example"><span class="node-tag example">{{wf.example}}</span></div>
        {% endif %}
        <div class="wf-card-actions">
            <button class="btn small" onclick="editWorkflow('{{wf.name}}')">&#9998; 编辑</button>
            <button class="btn danger small" onclick="delWorkflow('{{wf.name}}')">&#128465; 删除</button>
        </div>
    </div>
    {% endfor %}
    </div>
    <div class="inline-form" style="margin-top:10px">
        <button class="btn success" onclick="openWorkflowModal()">&#10133; 添加工作流</button>
    </div>
</div>

<div class="section"><h2>&#128209; 图片自定义尺寸 <span class="badge">{{size_presets|length}}种</span></h2>
<div class="table-wrap"><table id="size_table"><tr><th>名称</th><th>宽 × 高</th><th>触发词</th><th>操作</th></tr>{% for sp in size_presets %}<tr><td style="font-weight:500">{{sp.name}}</td><td>{{sp.width}} × {{sp.height}}</td><td><input type="text" id="trigger_{{sp.name}}" value="{{sp.trigger_word}}" size="10" style="width:80px"><button class="btn success small" onclick="updateSizeTrigger('{{sp.name}}')">改触发词</button></td><td><button class="btn danger small" onclick="delSize('{{sp.name}}')">删除</button></td></tr>{% endfor %}</table></div>
<h3>添加图片尺寸</h3>
<div class="inline-form"><input type="text" id="new_size_name" placeholder="名称" style="width:90px"><input type="number" id="new_size_width" placeholder="宽" style="width:80px"><input type="number" id="new_size_height" placeholder="高" style="width:80px"><input type="text" id="new_size_trigger" placeholder="触发词" style="width:90px"><button class="btn" onclick="addSize()">添加</button></div>
<hr>
<h2>&#127916; 视频自定义尺寸 <span class="badge" id="video_size_badge">{{video_size_presets|length}}种</span></h2>
<div class="table-wrap"><table id="video_size_table"><tr><th>名称</th><th>宽 × 高</th><th>操作</th></tr>{% for vsp in video_size_presets %}<tr><td style="font-weight:500">{{vsp.name}}</td><td>{{vsp.width}} × {{vsp.height}}</td><td><button class="btn danger small" onclick="delVideoSize('{{vsp.name}}')">删除</button></td></tr>{% endfor %}</table></div>
<h3>添加视频尺寸</h3>
<div class="inline-form"><input type="text" id="new_video_size_name" placeholder="名称" style="width:90px"><input type="number" id="new_video_size_width" placeholder="宽" style="width:80px"><input type="number" id="new_video_size_height" placeholder="高" style="width:80px"><button class="btn" onclick="addVideoSize()">添加</button></div>
</div>
</div>

<!-- 选项卡4：提示词过滤 -->
<div id="tab_filter" class="tab-content">
<div class="section"><h2>&#128481;&#65039; 提示词过滤</h2>

<!-- 卡片1：过滤规则 -->
<div class="filter-card">
    <h4>&#128295; 过滤规则 <span class="badge badge-blue">{{filter_word_count}}个</span></h4>
    <div class="inline-form"><label>过滤开关:</label>
    <span class="filter-status-dot {% if config.filter_enabled=='true' %}on{% else %}off{% endif %}"></span>
    <select id="filter_enabled" onchange="updateConfig('filter_enabled')">
        <option value="false" {% if config.filter_enabled=='false' %}selected{% endif %}>关闭</option>
        <option value="true" {% if config.filter_enabled=='true' %}selected{% endif %}>开启</option>
    </select>
    <span class="info-filter">管理员可私聊发送 过滤开/过滤关 控制</span></div>
    <div class="inline-form" style="align-items:flex-start"><label style="margin-top:6px">过滤池:</label>
    <textarea id="filter_words" rows="6" style="width:100%;max-width:600px;font-size:13px;font-family:inherit" placeholder="每行一个过滤词，或用逗号分隔，如：nsfw, nude, blood">{{config.filter_words}}</textarea></div>
    <div class="inline-form"><button class="btn" onclick="saveFilterWords()">保存过滤池</button><span class="info-filter">开启后自动从提示词中移除这些词</span></div>
</div>

<!-- 卡片2：过滤词预设 -->
<div class="filter-card">
    <h4>&#128230; 过滤词预设</h4>
    <div id="filter_presets_area">
    {% for fp in filter_presets %}
    <div class="filter-preset-card">
        <span class="preset-name">{{fp.name}}</span>
        <div class="preset-tags">
            {% set words_list = fp.words.split(',') %}
            {% for w in words_list[:20] %}
            {% set wt = w.strip() %}
            {% if wt %}<span class="wordbank-tag">{{wt}}</span>{% endif %}
            {% endfor %}
            {% if words_list|length > 20 %}<span class="info">…还有{{words_list|length - 20}}个</span>{% endif %}
        </div>
        <div class="preset-actions">
            <button class="btn success small" onclick="applyPreset({{fp.id}})">应用</button>
            <button class="btn danger small" onclick="delPreset({{fp.id}})">删除</button>
        </div>
    </div>
    {% endfor %}
    {% if not filter_presets %}<span class="info">暂无预设</span>{% endif %}
    </div>
    <div class="inline-form" style="margin-top:8px">
    <input type="text" id="preset_name" placeholder="预设名称" size="15">
    <input type="text" id="preset_words" placeholder="过滤词，逗号分隔" size="35">
    <button class="btn" onclick="addPreset()">添加预设</button>
    </div>
</div>

</div>
</div>

<!-- 选项卡5：随便来点 -->
<div id="tab_random" class="tab-content">
<div class="section">
    <h2>&#127922; 随便来点 设置</h2>
    <div class="inline-form"><label>总开关:</label>
        <select id="random_enabled" onchange="updateConfig('random_enabled')">
            <option value="true" {% if config.random_enabled=='true' %}selected{% endif %}>开启</option>
            <option value="false" {% if config.random_enabled=='false' %}selected{% endif %}>关闭</option>
        </select>
        <span class="info-tip">关闭后所有"随便来点"指令无效</span>
    </div>
    <div class="inline-form"><label>触发词:</label>
        <input type="text" id="random_trigger" value="{{config.random_trigger}}" size="20" placeholder="随便来点">
        <button class="btn" onclick="updateConfig('random_trigger')">保存</button>
        <span class="info-tip">用户发送此词触发随机生图</span>
    </div>
    <div class="inline-form"><label>私聊开关:</label>
        <select id="random_enabled_private" onchange="updateConfig('random_enabled_private')">
            <option value="true" {% if config.random_enabled_private=='true' %}selected{% endif %}>开启</option>
            <option value="false" {% if config.random_enabled_private=='false' %}selected{% endif %}>关闭</option>
        </select>
        <span class="info-tip">私聊直接发送触发词即可</span>
    </div>
    <div class="inline-form"><label>群聊开关:</label>
        <select id="random_enabled_group" onchange="updateConfig('random_enabled_group')">
            <option value="true" {% if config.random_enabled_group=='true' %}selected{% endif %}>开启</option>
            <option value="false" {% if config.random_enabled_group=='false' %}selected{% endif %}>关闭</option>
        </select>
        <span class="info-tip">群聊需要 @机器人 + 触发词</span>
    </div>
    <hr>
    <h3>&#127912; 随机模式选择</h3>
    <div class="checkbox-group">
        <label><input type="checkbox" id="random_sfw" {% if config.random_sfw=='true' %}checked{% endif %} onchange="saveRandomFlags()"> SFW（安全）</label>
        <label><input type="checkbox" id="random_nsfw" {% if config.random_nsfw=='true' %}checked{% endif %} onchange="saveRandomFlags()"> NSFW（成人）</label>
    </div>
    <span class="info-tip">至少勾选一个模式。两个都勾选则混合随机。</span>
    <hr>
    <h3>&#128202; 群聊次数限制</h3>
    <div class="inline-form"><label>次数模式:</label>
        <select id="random_usage_mode" onchange="onRandomUsageModeChange()">
            <option value="global" {% if config.random_usage_mode=='global' %}selected{% endif %}>跟随总上限</option>
            <option value="limited" {% if config.random_usage_mode=='limited' %}selected{% endif %}>固定次数</option>
            <option value="unlimited" {% if config.random_usage_mode=='unlimited' %}selected{% endif %}>不限制</option>
        </select>
        <input type="number" id="random_usage_limit" value="{{config.random_usage_limit if config.random_usage_mode=='limited' else ''}}" placeholder="次数" style="width:70px;{% if config.random_usage_mode != 'limited' %}display:none{% endif %}" onchange="saveRandomUsageConfig()">
        <button class="btn success small" id="random_usage_save" onclick="saveRandomUsageConfig()" style="display:none">保存</button>
        <span class="info-tip">仅在群聊中生效，私聊不受限制</span>
    </div>
    <hr>
    <h3>&#128203; 随便来点 返回显示设置</h3>
    <p class="info-tip">独立于系统设置，仅对「随便来点」生效</p>
    <div class="inline-form" style="flex-direction:column;align-items:flex-start;gap:4px">
        <span style="font-size:13px;font-weight:500">私聊显示：</span>
        <div class="checkbox-group" style="margin:0">
            <label><input type="checkbox" id="rdisp_private_prompt" {% if 'prompt' in config.random_private_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('private')"> 提示词</label>
            <label><input type="checkbox" id="rdisp_private_size" {% if 'size' in config.random_private_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('private')"> 尺寸</label>
            <label><input type="checkbox" id="rdisp_private_duration" {% if 'duration' in config.random_private_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('private')"> 耗时</label>
            <label><input type="checkbox" id="rdisp_private_workflow" {% if 'workflow' in config.random_private_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('private')"> 工作流名</label>
        </div>
    </div>
    <div class="inline-form" style="flex-direction:column;align-items:flex-start;gap:4px;margin-top:8px">
        <span style="font-size:13px;font-weight:500">群聊显示：</span>
        <div class="checkbox-group" style="margin:0">
            <label><input type="checkbox" id="rdisp_group_prompt" {% if 'prompt' in config.random_group_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('group')"> 提示词</label>
            <label><input type="checkbox" id="rdisp_group_size" {% if 'size' in config.random_group_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('group')"> 尺寸</label>
            <label><input type="checkbox" id="rdisp_group_duration" {% if 'duration' in config.random_group_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('group')"> 耗时</label>
            <label><input type="checkbox" id="rdisp_group_workflow" {% if 'workflow' in config.random_group_display_flags %}checked{% endif %} onchange="saveRandomDisplayFlags('group')"> 工作流名</label>
        </div>
    </div>
    <hr>
    <h3>&#128221; 词库管理 <span class="badge" id="wordbank_count_badge">{{config.random_wordbank_count}}条</span></h3>
    <div class="inline-form">
        <label>搜索词条(去重检查)：</label>
        <input type="text" id="wb_search_keyword" placeholder="输入关键词" size="25">
        <button class="btn" onclick="searchWordbankWord()">搜索</button>
        <button class="btn success" onclick="reloadWordbank()">刷新词库</button>
        <div id="wb_search_result" style="margin-left:10px;font-size:12px;color:#888;display:inline-block"></div>
    </div>
    <hr style="margin:8px 0">
    <div class="wordbank-grid" id="wordbank_grid">
        {% for cat in random_wordbank %}
        <div class="wordbank-cat">
            <h4>{{cat.name}} <span class="badge-blue" style="display:inline-flex;align-items:center;padding:1px 8px;background:#3498db;color:#fff;border-radius:10px;font-size:10px;margin-left:4px">{{cat.words|length}}条</span></h4>
            <div>
                {% for w in cat.words[:30] %}
                <span class="wordbank-tag {{w.get('type','sfw')}}">{{w.text}}{% if w.zh %}<small style="color:#999;margin-left:3px">({{w.zh}})</small>{% endif %}</span>
                {% endfor %}
                {% if cat.words|length > 30 %}
                <span class="info">…还有{{cat.words|length - 30}}条</span>
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>
</div>
</div>

<!-- 添加/编辑工作流弹窗 -->
<div class="modal-overlay" id="wfModal">
<div class="modal-box">
<h3 id="wfModalTitle">&#10133; 添加工作流</h3>
<input type="hidden" id="modal_edit_mode" value="0">
<input type="hidden" id="modal_edit_old_name" value="">
<div class="field"><label>名称</label><input type="text" id="modal_wf_name" placeholder="工作流名称"></div>
<div class="field"><label>文件路径</label><select id="modal_wf_file_select" style="min-width:120px" onchange="document.getElementById('modal_wf_file').value=this.value"></select><input type="text" id="modal_wf_file" placeholder="或手动输入json路径" size="22" style="margin-top:4px"></div>
<div class="field"><label>输出类型</label><select id="modal_wf_output_type"><option value="image">&#128444;&#65039; 图片</option><option value="text">&#128221; 文字</option><option value="video">&#127916; 视频</option></select></div>
<div class="field">
    <label>节点配置 <button type="button" class="btn small" onclick="addNodeRow()" style="margin-left:8px">&#10133; 添加节点</button></label>
    <div id="modal_nodes_container" style="margin-top:4px"></div>
</div>
<div class="field"><label>备注</label><input type="text" id="modal_wf_note" placeholder="备注"></div>
<div class="field"><label>示例</label><input type="text" id="modal_wf_example" placeholder="示例提示词"></div>
<div class="modal-actions"><button class="btn" onclick="closeWorkflowModal()">取消</button><button class="btn success" id="wfModalSubmitBtn" onclick="confirmAddWorkflow()">添加</button></div>
</div>
</div>
"""
# =====================================================================
# 管理后台 HTML 模板 - JavaScript 部分（+皮肤切换 +美化开关 +回到顶部）
# =====================================================================
ADMIN_HTML_JS = r"""<script>
function postApi(url, data) {
    saveActiveTab();
    var xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onreadystatechange = function() {
        if (xhr.readyState == 4) {
            if (xhr.status == 200) {
                var d = JSON.parse(xhr.responseText);
                if (d.status == "ok") { location.reload(); }
                else { alert(d.message || "操作失败"); }
            } else { alert("请求失败"); }
        }
    };
    xhr.send(JSON.stringify(data));
}

function saveActiveTab() {
    var active = document.querySelector('.tab-btn.active');
    if (active) {
        var tabId = active.getAttribute('data-tab') || active.getAttribute('onclick')?.match(/'([^']+)'/)?.[1];
        if (tabId) localStorage.setItem('artlink_active_tab', tabId);
    }
}

function switchTab(tabId, btn) {
    document.querySelectorAll('.tab-content').forEach(function(el) { el.classList.remove('active'); });
    document.querySelectorAll('.tab-btn').forEach(function(el) { el.classList.remove('active'); });
    document.getElementById(tabId).classList.add('active');
    if (btn) btn.classList.add('active');
    localStorage.setItem('artlink_active_tab', tabId);
}

function restoreActiveTab() {
    var saved = localStorage.getItem('artlink_active_tab');
    if (saved) {
        var tabContent = document.getElementById(saved);
        var tabBtn = document.querySelector('.tab-btn[data-tab="' + saved + '"]');
        if (tabContent && tabBtn) {
            document.querySelectorAll('.tab-content').forEach(function(el) { el.classList.remove('active'); });
            document.querySelectorAll('.tab-btn').forEach(function(el) { el.classList.remove('active'); });
            tabContent.classList.add('active');
            tabBtn.classList.add('active');
        }
    }
}

function toggleHistory() {
    var body = document.getElementById('history_body');
    var arrow = document.getElementById('history_arrow');
    if (body.style.display === 'none') {
        body.style.display = 'block';
        arrow.innerHTML = '&#9660;';
    } else {
        body.style.display = 'none';
        arrow.innerHTML = '&#9654;';
    }
}

function toggleBlacklist() {
    var body = document.getElementById('blacklist_body');
    var arrow = document.getElementById('blacklist_arrow');
    if (body.style.display === 'none') {
        body.style.display = 'block';
        arrow.innerHTML = '&#9660;';
    } else {
        body.style.display = 'none';
        arrow.innerHTML = '&#9654;';
    }
}

function updateConfig(k) {
    var v = document.getElementById(k).value;
    postApi("/api/config/update", {key:k, value:v});
}
function saveFilterWords() {
    var v = document.getElementById("filter_words").value;
    postApi("/api/config/update", {key:"filter_words", value:v});
}
function savePrivateDisplayFlags() {
    var flags = [];
    if (document.getElementById("disp_prompt").checked) flags.push("prompt");
    if (document.getElementById("disp_size").checked) flags.push("size");
    if (document.getElementById("disp_duration").checked) flags.push("duration");
    if (document.getElementById("disp_workflow").checked) flags.push("workflow");
    postApi("/api/config/update", {key:"private_display_flags", value:flags.join(",")});
}
function saveGroupDisplayFlags() {
    var flags = [];
    if (document.getElementById("gdisp_prompt").checked) flags.push("prompt");
    if (document.getElementById("gdisp_size").checked) flags.push("size");
    if (document.getElementById("gdisp_duration").checked) flags.push("duration");
    if (document.getElementById("gdisp_workflow").checked) flags.push("workflow");
    postApi("/api/config/update", {key:"group_display_flags", value:flags.join(",")});
}
function saveRandomFlags() {
    saveActiveTab();
    var sfw = document.getElementById('random_sfw').checked ? 'true' : 'false';
    var nsfw = document.getElementById('random_nsfw').checked ? 'true' : 'false';
    fetch('/api/config/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:'random_sfw',value:sfw})});
    fetch('/api/config/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:'random_nsfw',value:nsfw})});
    showToast('✅ 已保存');
}
function onRandomUsageModeChange() {
    var mode = document.getElementById("random_usage_mode").value;
    var limitInput = document.getElementById("random_usage_limit");
    var saveBtn = document.getElementById("random_usage_save");
    if (mode === "limited") {
        limitInput.style.display = "inline-block";
        limitInput.focus();
        saveBtn.style.display = "none";
    } else {
        limitInput.style.display = "none";
        saveBtn.style.display = "inline-block";
    }
}
function saveRandomUsageConfig() {
    saveActiveTab();
    var mode = document.getElementById("random_usage_mode").value;
    var limit = mode === "limited" ? parseInt(document.getElementById("random_usage_limit").value) || 0 : 0;
    Promise.all([
        fetch('/api/config/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:'random_usage_mode',value:mode})}),
        fetch('/api/config/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:'random_usage_limit',value:String(limit)})})
    ]).then(function() { showToast('✅ 已保存'); location.reload(); });
}
function saveRandomDisplayFlags(type) {
    saveActiveTab();
    var flags = [];
    var prefix = type === 'private' ? 'rdisp_private_' : 'rdisp_group_';
    if (document.getElementById(prefix + 'prompt').checked) flags.push("prompt");
    if (document.getElementById(prefix + 'size').checked) flags.push("size");
    if (document.getElementById(prefix + 'duration').checked) flags.push("duration");
    if (document.getElementById(prefix + 'workflow').checked) flags.push("workflow");
    var key = type === 'private' ? 'random_private_display_flags' : 'random_group_display_flags';
    fetch('/api/config/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key,value:flags.join(',')})})
    .then(function(r){return r.json()}).then(function(d){
        if(d.status==='ok') showToast('✅ 已保存');
    });
}
function showToast(msg) {
    var old = document.querySelector('.toast');
    if (old) old.remove();
    var t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function() { t.remove(); }, 2000);
}

function checkNapcat() {
    saveActiveTab();
    fetch('/api/check/napcat', {method:'POST'}).then(function(r){return r.json()}).then(function(d){
        if(d.status==='ok'){
            showToast('NapCat ' + (d.online ? '✅ 在线' : '❌ 离线'));
            location.reload();
        }
    });
}
function checkComfyui() {
    saveActiveTab();
    fetch('/api/check/comfyui', {method:'POST'}).then(function(r){return r.json()}).then(function(d){
        if(d.status==='ok'){
            showToast('ComfyUI ' + (d.online ? '✅ 在线' : '❌ 离线'));
            location.reload();
        }
    });
}

// ═══════════════════════════════════════════════════
// 皮肤系统
// ═══════════════════════════════════════════════════
function initTheme() {
    var theme = localStorage.getItem('artlink_theme') || 'default';
    applyTheme(theme);
    applySettings();
    createThemeUI();
    createBackToTop();
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('artlink_theme', theme);
    // 默认皮肤 = 极简模式，不加美化类
    if (theme === 'default') {
        document.body.classList.remove('anim-cards', 'anim-tabs');
        document.querySelector('.tab-bar')?.classList.remove('sticky');
        document.getElementById('backToTop')?.classList.remove('show');
    } else {
        applySettings();
    }
    updateThemePanel(theme);
}

function applySettings() {
    var theme = localStorage.getItem('artlink_theme') || 'default';
    var anim = localStorage.getItem('artlink_anim') !== 'false';
    var btt = localStorage.getItem('artlink_btt') !== 'false';
    var sticky = localStorage.getItem('artlink_sticky') !== 'false';

    // 默认皮肤强制关闭所有美化
    if (theme === 'default') {
        anim = false; btt = false; sticky = false;
    }

    // 动画
    if (anim) {
        document.body.classList.add('anim-cards', 'anim-tabs');
    } else {
        document.body.classList.remove('anim-cards', 'anim-tabs');
    }
    // 回到顶部
    var btn = document.getElementById('backToTop');
    if (btn) {
        if (btt) { btn.classList.add('show'); } else { btn.classList.remove('show'); }
    }
    // 固定导航
    var tabBar = document.querySelector('.tab-bar');
    if (tabBar) {
        if (sticky) { tabBar.classList.add('sticky'); } else { tabBar.classList.remove('sticky'); }
    }
    // 更新开关UI
    updateToggleUI('anim', anim);
    updateToggleUI('btt', btt);
    updateToggleUI('sticky', sticky);
}

function updateThemePanel(theme) {
    var opts = document.querySelectorAll('#themePanel .theme-option');
    opts.forEach(function(o) { o.classList.remove('active'); });
    var active = document.querySelector('#themePanel .theme-option[data-theme="' + theme + '"]');
    if (active) active.classList.add('active');
}

function updateToggleUI(key, val) {
    var cb = document.getElementById('toggle_' + key);
    if (cb) cb.checked = val;
}

function createThemeUI() {
    // 皮肤切换按钮
    var btn = document.createElement('button');
    btn.id = 'themeSwitcher';
    btn.innerHTML = '🎨';
    btn.title = '切换皮肤';
    btn.onclick = function(e) {
        e.stopPropagation();
        var panel = document.getElementById('themePanel');
        panel.classList.toggle('show');
    };
    document.body.appendChild(btn);

    // 皮肤面板
    var panel = document.createElement('div');
    panel.id = 'themePanel';
    panel.innerHTML = '<h4>🎨 选择皮肤</h4>' +
        '<div class="theme-option" data-theme="default" onclick="applyTheme(\'default\')"><span class="theme-dot d1"></span>💙 经典蓝</div>' +
        '<div class="theme-option" data-theme="dark" onclick="applyTheme(\'dark\')"><span class="theme-dot d2"></span>🌙 深色科技</div>' +
        '<div class="theme-option" data-theme="light" onclick="applyTheme(\'light\')"><span class="theme-dot d3"></span>☀️ 浅色精致</div>' +
        '<div class="theme-option" data-theme="green" onclick="applyTheme(\'green\')"><span class="theme-dot d4"></span>🌿 暗夜绿</div>' +
        '<div class="theme-option" data-theme="warm" onclick="applyTheme(\'warm\')"><span class="theme-dot d5"></span>🔥 暖橙</div>' +
        '<div class="theme-divider"></div>' +
        '<h4>⚙️ 美化开关</h4>' +
        '<div class="theme-toggle"><span>🎬 动画效果</span><input type="checkbox" id="toggle_anim" onchange="toggleSetting(\'anim\',this.checked)"></div>' +
        '<div class="theme-toggle"><span>🔝 回到顶部</span><input type="checkbox" id="toggle_btt" onchange="toggleSetting(\'btt\',this.checked)"></div>' +
        '<div class="theme-toggle"><span>📌 固定导航</span><input type="checkbox" id="toggle_sticky" onchange="toggleSetting(\'sticky\',this.checked)"></div>';
    document.body.appendChild(panel);

    // 点击其他地方关闭面板
    document.addEventListener('click', function(e) {
        if (!panel.contains(e.target) && e.target !== btn) {
            panel.classList.remove('show');
        }
    });

    updateThemePanel(localStorage.getItem('artlink_theme') || 'default');
}

function toggleSetting(key, val) {
    var theme = localStorage.getItem('artlink_theme') || 'default';
    if (theme === 'default') {
        showToast('⚠️ 经典蓝为极简模式，美化开关不生效');
        updateToggleUI(key, false);
        return;
    }
    localStorage.setItem('artlink_' + key, val ? 'true' : 'false');
    applySettings();
}

function createBackToTop() {
    var btn = document.createElement('button');
    btn.id = 'backToTop';
    btn.innerHTML = '⬆';
    btn.title = '回到顶部';
    btn.onclick = function() { window.scrollTo({top:0,behavior:'smooth'}); };
    document.body.appendChild(btn);

    window.addEventListener('scroll', function() {
        var theme = localStorage.getItem('artlink_theme') || 'default';
        var btt = localStorage.getItem('artlink_btt') !== 'false';
        if (theme === 'default') btt = false;
        if (btt && window.scrollY > 300) {
            btn.classList.add('show');
        } else {
            btn.classList.remove('show');
        }
    });
}

// ═══════════════════════════════════════════════════
// 原有功能函数
// ═══════════════════════════════════════════════════
function addAdmin(){var q=document.getElementById('admin_qq').value.trim(),r=document.getElementById('admin_remark').value.trim();if(q){postApi('/api/admin/add',{qq:q,remark:r})}else{alert('请输入QQ号')}}
function delAdmin(q){if(confirm('确认删除管理员？')){postApi('/api/admin/delete',{qq:q})}}
function addPrivateQQ(){var q=document.getElementById('private_qq').value.trim(),r=document.getElementById('private_remark').value.trim();if(q){postApi('/api/private_whitelist/add',{qq:q,remark:r})}else{alert('请输入QQ号')}}
function delPrivateQQ(q){if(confirm('确认删除？')){postApi('/api/private_whitelist/delete',{qq:q})}}
function editPrivateRemark(q){var r=prompt('新备注:');if(r!==null){postApi('/api/private_whitelist/remark',{qq:q,remark:r.trim()})}}
function addGroup(){var g=document.getElementById('group_id').value.trim(),r=document.getElementById('group_remark').value.trim();if(g){postApi('/api/group_whitelist/add',{group_id:g,remark:r})}else{alert('请输入群号')}}
function delGroup(g){if(confirm('确认删除该群及所有成员？')){postApi('/api/group_whitelist/delete',{group_id:g})}}
function editGroupRemark(g){var r=prompt('新备注:');if(r!==null){postApi('/api/group_whitelist/remark',{group_id:g,remark:r.trim()})}}
function toggleGroupEnabled(g, v) {
    saveActiveTab();
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/group_whitelist/toggle", true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onreadystatechange = function() {
        if (xhr.readyState == 4 && xhr.status == 200) {
            var d = JSON.parse(xhr.responseText);
            if (d.status == "ok") { showToast('✅ 已切换'); }
            else { alert('切换失败'); }
        }
    };
    xhr.send(JSON.stringify({group_id: g, enabled: parseInt(v)}));
}
function addMember(){var g=document.getElementById('member_group_select').value,q=document.getElementById('member_qq').value.trim(),r=document.getElementById('member_remark').value.trim();if(!g){alert('请选择群')}else if(!q){alert('请输入QQ号')}else{postApi('/api/group_member/add',{group_id:g,qq:q,remark:r})}}
function delMember(g,q){if(confirm('确认删除？')){postApi('/api/group_member/delete',{group_id:g,qq:q})}}
function editMemberRemark(g,q){var r=prompt('新备注:');if(r!==null){postApi('/api/group_member/remark',{group_id:g,qq:q,remark:r.trim()})}}
function resetMemberUsage(g,q){if(confirm('重置？')){postApi('/api/usage/reset_member',{group_id:g,qq:q})}}
function resetGroupUsage(){var g=document.getElementById('member_group_select').value;if(!g){alert('请选择群');return}if(confirm('确认重置？')){postApi('/api/usage/reset_group',{group_id:g})}}
function banMember(g,q){if(!confirm('拉黑？'))return;postApi('/api/blacklist/add',{group_id:g,qq:q})}
function unbanMember(g,q){if(!confirm('取消拉黑？'))return;postApi('/api/blacklist/remove',{group_id:g,qq:q})}
function delTrigger(w){postApi('/api/trigger/delete',{word:w})}
function updateSizeTrigger(n){var w=document.getElementById('trigger_'+n).value.trim();if(w){postApi('/api/size/update_trigger',{name:n,trigger_word:w})}}
function addSize(){var n=document.getElementById('new_size_name').value.trim(),w=parseInt(document.getElementById('new_size_width').value)||0,h=parseInt(document.getElementById('new_size_height').value)||0,t=document.getElementById('new_size_trigger').value.trim();if(!n||!w||!h||!t){alert('名称、宽、高、触发词都不能为空');return}postApi('/api/size/add',{name:n,width:w,height:h,trigger_word:t})}
function delSize(n){var rows=document.querySelectorAll('#size_table tr');if(rows.length<=2){alert('至少保留一个尺寸，无法删除');return}if(confirm('确认删除尺寸 '+n+'？')){postApi('/api/size/delete',{name:n})}}
function addVideoSize(){var n=document.getElementById('new_video_size_name').value.trim(),w=parseInt(document.getElementById('new_video_size_width').value)||0,h=parseInt(document.getElementById('new_video_size_height').value)||0;if(!n||!w||!h){alert('名称、宽、高都不能为空');return}postApi('/api/video_size/add',{name:n,width:w,height:h})}
function delVideoSize(n){var rows=document.querySelectorAll('#video_size_table tr');if(rows.length<=2){alert('至少保留一个尺寸，无法删除');return}if(confirm('确认删除视频尺寸 '+n+'？')){postApi('/api/video_size/delete',{name:n})}}

function onTriggerChipModeChange(wfName) {
    var mode = document.getElementById('wu_mode_' + wfName).value;
    var limitInput = document.getElementById('wu_limit_' + wfName);
    if (mode === 'limited') { limitInput.style.display = 'inline-block'; limitInput.focus(); }
    else { limitInput.style.display = 'none'; }
    if (mode !== 'limited') { saveTriggerUsage(wfName); }
}
function saveTriggerUsage(wfName) {
    var mode = document.getElementById('wu_mode_' + wfName).value;
    var limit = 0;
    if (mode === 'limited') { limit = parseInt(document.getElementById('wu_limit_' + wfName).value) || 0; if (isNaN(limit) || limit < 0) { alert('请输入有效次数'); return; } }
    if (mode === 'unlimited') { limit = -1; }
    postApi('/api/workflow/usage/update', {workflow_name: wfName, usage_mode: mode, usage_limit: limit});
}
function addTriggerWithUsage() {
    var w = document.getElementById('trigger_word').value.trim();
    var wf = document.getElementById('trigger_wf_select').value;
    var mode = document.getElementById('trigger_usage_mode_select').value;
    var limit = 0;
    if (mode === 'limited') { limit = parseInt(document.getElementById('trigger_usage_limit_val').value) || 0; }
    if (!w) { alert('请输入触发词'); return; }
    if (!wf) { alert('请选择工作流'); return; }
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/trigger/add", true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onreadystatechange = function() {
        if (xhr.readyState == 4 && xhr.status == 200) {
            var d = JSON.parse(xhr.responseText);
            if (d.status == "ok") {
                var xhr2 = new XMLHttpRequest();
                xhr2.open("POST", "/api/workflow/usage/update", true);
                xhr2.setRequestHeader("Content-Type", "application/json");
                xhr2.onreadystatechange = function() { if (xhr2.readyState == 4 && xhr2.status == 200) { location.reload(); } };
                xhr2.send(JSON.stringify({workflow_name: wf, usage_mode: mode, usage_limit: limit}));
            } else { alert(d.message || "添加失败"); }
        }
    };
    xhr.send(JSON.stringify({word: w, workflow: wf}));
}
(function() {
    var s = document.getElementById('trigger_usage_mode_select');
    if (s) { s.onchange = function() { var lim = document.getElementById('trigger_usage_limit_val'); lim.style.display = this.value === 'limited' ? 'inline-block' : 'none'; }; }
})();

var modalNodes = [];
var typeLabels = { positive: '提示词', latent: 'Latent', image_input: '图片', video_width: '宽度', video_height: '高度', duration: '时长' };

function _nodeTypeChanged(sel) {
    var row = sel.parentNode; var idx = parseInt(row.getAttribute('data-idx'));
    if (isNaN(idx) || idx >= modalNodes.length) return;
    modalNodes[idx].type = sel.value;
    var tag = row.querySelector('.node-type-tag');
    if (tag) { tag.className = 'node-type-tag ' + sel.value; tag.textContent = typeLabels[sel.value] || sel.value; }
}
function _nodeTitleChanged(inp) {
    var row = inp.parentNode; var idx = parseInt(row.getAttribute('data-idx'));
    if (isNaN(idx) || idx >= modalNodes.length) return;
    modalNodes[idx].title = inp.value;
}
function _deleteNodeRow(btn) {
    var row = btn.parentNode; var container = row.parentNode; var idx = parseInt(row.getAttribute('data-idx'));
    if (!isNaN(idx) && idx < modalNodes.length) { modalNodes.splice(idx, 1); }
    row.remove();
    var rows = container.querySelectorAll('.node-row'); var newNodes = [];
    for (var i = 0; i < rows.length; i++) {
        var r = rows[i]; r.setAttribute('data-idx', i);
        var s = r.querySelector('select'); var t = r.querySelector('input');
        newNodes.push({type: s ? s.value : 'positive', title: t ? t.value : ''});
    }
    modalNodes = newNodes;
}
function addNodeRow() {
    var i = modalNodes.length; modalNodes.push({type: 'positive', title: ''});
    var container = document.getElementById('modal_nodes_container');
    var h = '<div class="node-row" data-idx="' + i + '">';
    h += '<span class="node-type-tag positive">提示词</span>';
    h += '<select onchange="_nodeTypeChanged(this)">';
    h += '<option value="positive" selected>提示词节点</option><option value="latent">Latent节点</option><option value="image_input">图片节点</option><option value="video_width">宽度(视频)</option><option value="video_height">高度(视频)</option><option value="duration">时长(视频)</option>';
    h += '</select><input type="text" placeholder="节点编号或标题" value="" oninput="_nodeTitleChanged(this)"><button class="btn danger small" onclick="_deleteNodeRow(this)">×</button></div>';
    container.insertAdjacentHTML('beforeend', h);
}
function openWorkflowModal() {
    saveActiveTab();
    document.getElementById('modal_edit_mode').value = '0'; document.getElementById('modal_edit_old_name').value = '';
    document.getElementById('wfModalTitle').innerHTML = '➕ 添加工作流'; document.getElementById('wfModalSubmitBtn').textContent = '添加';
    modalNodes = [];
    document.getElementById('modal_wf_name').value = ''; document.getElementById('modal_wf_file').value = '';
    document.getElementById('modal_wf_file_select').value = ''; document.getElementById('modal_wf_output_type').value = 'image';
    document.getElementById('modal_wf_note').value = ''; document.getElementById('modal_wf_example').value = '';
    document.getElementById('modal_nodes_container').innerHTML = '';
    loadWorkflowFilesIntoModal();
    document.getElementById('wfModal').classList.add('active');
}
function editWorkflow(name) {
    saveActiveTab();
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/workflow/get?name=' + encodeURIComponent(name), true);
    xhr.onreadystatechange = function() {
        if (xhr.readyState == 4 && xhr.status == 200) {
            var d = JSON.parse(xhr.responseText);
            if (d.status !== 'ok') { alert(d.message || '获取工作流信息失败'); return; }
            var wf = d.workflow;
            document.getElementById('modal_edit_mode').value = '1'; document.getElementById('modal_edit_old_name').value = wf.name;
            document.getElementById('wfModalTitle').innerHTML = '✏️ 编辑工作流'; document.getElementById('wfModalSubmitBtn').textContent = '保存';
            document.getElementById('modal_wf_name').value = wf.name; document.getElementById('modal_wf_file').value = '';
            document.getElementById('modal_wf_file_select').value = wf.file_path;
            document.getElementById('modal_wf_output_type').value = wf.output_type || 'image';
            document.getElementById('modal_wf_note').value = wf.note || ''; document.getElementById('modal_wf_example').value = wf.example || '';
            modalNodes = []; document.getElementById('modal_nodes_container').innerHTML = '';
            if (wf.nodes && wf.nodes.length > 0) {
                modalNodes = wf.nodes;
                for (var i = 0; i < modalNodes.length; i++) {
                    var n = modalNodes[i]; var container = document.getElementById('modal_nodes_container');
                    var tagClass = n.type || 'positive';
                    var h = '<div class="node-row" data-idx="' + i + '">';
                    h += '<span class="node-type-tag ' + tagClass + '">' + (typeLabels[tagClass] || tagClass) + '</span>';
                    h += '<select onchange="_nodeTypeChanged(this)">';
                    h += '<option value="positive" ' + (tagClass==='positive'?'selected':'') + '>提示词节点</option>';
                    h += '<option value="latent" ' + (tagClass==='latent'?'selected':'') + '>Latent节点</option>';
                    h += '<option value="image_input" ' + (tagClass==='image_input'?'selected':'') + '>图片节点</option>';
                    h += '<option value="video_width" ' + (tagClass==='video_width'?'selected':'') + '>宽度(视频)</option>';
                    h += '<option value="video_height" ' + (tagClass==='video_height'?'selected':'') + '>高度(视频)</option>';
                    h += '<option value="duration" ' + (tagClass==='duration'?'selected':'') + '>时长(视频)</option>';
                    h += '</select><input type="text" placeholder="节点编号或标题" value="' + (n.title||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;') + '" oninput="_nodeTitleChanged(this)"><button class="btn danger small" onclick="_deleteNodeRow(this)">×</button></div>';
                    container.insertAdjacentHTML('beforeend', h);
                }
            }
            loadWorkflowFilesIntoModal(); document.getElementById('wfModal').classList.add('active');
        }
    }; xhr.send();
}
function closeWorkflowModal() { document.getElementById('wfModal').classList.remove('active'); modalNodes = []; }
function loadWorkflowFilesIntoModal() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/workflow/files', true);
    xhr.onreadystatechange = function() {
        if (xhr.readyState == 4 && xhr.status == 200) {
            var d = JSON.parse(xhr.responseText);
            var s = document.getElementById('modal_wf_file_select');
            s.innerHTML = '<option value="">--选择JSON文件--</option>';
            for (var i = 0; i < d.files.length; i++) { s.innerHTML += '<option value="'+d.files[i]+'">'+d.files[i]+'</option>'; }
        }
    }; xhr.send();
}
function confirmAddWorkflow() {
    var isEdit = document.getElementById('modal_edit_mode').value === '1';
    var oldName = document.getElementById('modal_edit_old_name').value;
    var name = document.getElementById('modal_wf_name').value.trim();
    var fp = document.getElementById('modal_wf_file_select').value || document.getElementById('modal_wf_file').value.trim();
    if (!name || !fp) { alert('名称和文件路径必填'); return; }
    var container = document.getElementById('modal_nodes_container');
    var rows = container.querySelectorAll('.node-row'); var nodes = [];
    for (var r = 0; r < rows.length; r++) {
        var sel = rows[r].querySelector('select'); var inp = rows[r].querySelector('input');
        nodes.push({type: sel ? sel.value : 'positive', title: inp ? inp.value : ''});
    }
    var outputType = document.getElementById('modal_wf_output_type').value;
    var note = document.getElementById('modal_wf_note').value.trim();
    var example = document.getElementById('modal_wf_example').value.trim();
    var data = { name: name, file_path: fp, output_type: outputType, note: note, example: example, nodes: nodes };
    if (isEdit) { data.old_name = oldName; postApi('/api/workflow/update', data); }
    else { postApi('/api/workflow/add', data); }
}
function delWorkflow(n){if(confirm('确认删除工作流 '+n+'?')){postApi('/api/workflow/delete',{name:n})}}
function addPreset(){var n=document.getElementById('preset_name').value.trim(),w=document.getElementById('preset_words').value.trim();if(!n||!w){alert('名称和过滤词不能为空');return}postApi('/api/filter_preset/add',{name:n,words:w})}
function delPreset(id){if(confirm('确认删除此预设？')){postApi('/api/filter_preset/delete',{id:id})}}
function applyPreset(id){var xhr=new XMLHttpRequest();xhr.open('POST','/api/filter_preset/apply',true);xhr.setRequestHeader('Content-Type','application/json');xhr.onreadystatechange=function(){if(xhr.readyState==4&&xhr.status==200){var d=JSON.parse(xhr.responseText);if(d.status=='ok'){document.getElementById('filter_words').value=d.new_words;showToast('✅ 已追加进过滤池，别忘了点保存');}else{alert(d.message||'失败')}}};xhr.send(JSON.stringify({id:id}))}
function loadMembers(){var g=document.getElementById('member_group_select').value;if(!g){document.getElementById('member_table').innerHTML='<tr><th>QQ号</th><th>备注</th><th>今日已用</th><th>操作</th></tr>';return}var xhr=new XMLHttpRequest();xhr.open('GET','/api/group_member/list_with_usage?group_id='+g,true);xhr.onreadystatechange=function(){if(xhr.readyState==4&&xhr.status==200){var d=JSON.parse(xhr.responseText),t=document.getElementById('member_table');t.innerHTML='<tr><th>QQ号</th><th>备注</th><th>今日已用</th><th>操作</th></tr>';for(var i=0;i<d.members.length;i++){var m=d.members[i];t.innerHTML+='<tr><td>'+m.qq+'</td><td><span class=remark>'+(m.remark||'')+'</span></td><td>'+m.used+'/'+m.max+'</td><td><button class=\"btn success small\" onclick=\"editMemberRemark(\x27'+g+'\x27,\x27'+m.qq+'\x27)\">备注</button><button class=\"btn warning small\" onclick=\"resetMemberUsage(\x27'+g+'\x27,\x27'+m.qq+'\x27)\">重置</button><button class=\"btn dark small\" onclick=\"banMember(\x27'+g+'\x27,\x27'+m.qq+'\x27)\">拉黑</button><button class=\"btn danger small\" onclick=\"delMember(\x27'+g+'\x27,\x27'+m.qq+'\x27)\">删除</button></td></tr>'}}};xhr.send()}
function loadBlacklist(){var g=document.getElementById('blacklist_group_select').value;if(!g){document.getElementById('blacklist_table').innerHTML='<tr><th>QQ号</th><th>备注</th><th>操作</th></tr>';return}var xhr=new XMLHttpRequest();xhr.open('GET','/api/blacklist/list?group_id='+g,true);xhr.onreadystatechange=function(){if(xhr.readyState==4&&xhr.status==200){var d=JSON.parse(xhr.responseText),t=document.getElementById('blacklist_table');t.innerHTML='<tr><th>QQ号</th><th>备注</th><th>操作</th></tr>';for(var i=0;i<d.blacklist.length;i++){var m=d.blacklist[i];t.innerHTML+='<tr class=blacklist-row><td>'+m.qq+'</td><td><span class=remark>'+(m.remark||'')+'</span></td><td><button class=\"btn success small\" onclick=\"unbanMember(\x27'+g+'\x27,\x27'+m.qq+'\x27)\">取消拉黑</button></td></tr>'}}};xhr.send()}
function loadWorkflowFiles(){}
var currentPage=1;
function searchHistory(page){if(!page)page=1;currentPage=page;var kw=document.getElementById('history_search').value.trim(),status=document.getElementById('history_status').value;var xhr=new XMLHttpRequest();xhr.open('GET','/api/history/search?keyword='+encodeURIComponent(kw)+'&status='+encodeURIComponent(status)+'&page='+page,true);xhr.onreadystatechange=function(){if(xhr.readyState==4&&xhr.status==200){var d=JSON.parse(xhr.responseText),t=document.getElementById('history_table');t.innerHTML='<tr><th>时间</th><th>QQ</th><th>群</th><th>类型</th><th>工作流</th><th>尺寸</th><th>耗时</th><th>状态</th></tr>';for(var i=0;i<d.data.length;i++){var h=d.data[i],remarkHtml=h.remark?' <span class=remark>('+h.remark+')</span>':'',isPrivate=(!h.group_id||h.group_id===''),typeHtml=isPrivate?'私聊':'群聊';t.innerHTML+='<tr><td>'+h.time+'</td><td>'+h.qq+remarkHtml+'</td><td>'+(h.group_id||'-')+'</td><td>'+typeHtml+'</td><td>'+h.workflow+'</td><td>'+(h.size||'-')+'</td><td>'+(h.duration?h.duration.toFixed(1)+'秒':'-')+'</td><td>'+h.status+'</td></tr>'}var pager=document.getElementById('history_pager');pager.innerHTML='';if(d.total_pages>1){if(page>1){pager.innerHTML+='<button class=\"btn small\" onclick=\"searchHistory('+(page-1)+')\">上一页</button>'}pager.innerHTML+='<span>第 '+page+' / '+d.total_pages+' 页</span>';if(page<d.total_pages){pager.innerHTML+='<button class=\"btn small\" onclick=\"searchHistory('+(page+1)+')\">下一页</button>'}}}};xhr.send()}
function confirmClearHistory(){var a=prompt('警告：清空所有历史！请输入 DELETE 确认：');if(a==='DELETE'){var xhr=new XMLHttpRequest();xhr.open('GET','/api/history/clear',true);xhr.onreadystatechange=function(){if(xhr.readyState==4&&xhr.status==200){var d=JSON.parse(xhr.responseText);if(d.status==='ok'){location.reload()}}};xhr.send()}else if(a!==null){alert('输入错误，已取消')}}
function searchWordbankWord() {
    var kw=document.getElementById('wb_search_keyword').value.trim();
    if(!kw){document.getElementById('wb_search_result').textContent='';return}
    fetch('/api/random/wordbank/search?keyword='+encodeURIComponent(kw)).then(function(r){return r.json()}).then(function(d){
        var el=document.getElementById('wb_search_result');
        if(d.results.length===0)el.innerHTML='✅ 未找到重复词条';
        else el.innerHTML='⚠️ 找到 '+d.results.length+' 条：'+d.results.map(function(r){return r.text+'('+r.category_name+'-'+r.type+')'}).join('、');
    });
}
function reloadWordbank() {
    saveActiveTab();
    fetch('/api/random/wordbank/reload',{method:'POST',headers:{'Content-Type':'application/json'}}).then(function(r){return r.json()}).then(function(d){
        if(d.status==='ok'){showToast('✅ 词库已刷新，共 '+d.total+' 条词');setTimeout(function(){location.reload()},1000)}
        else alert('❌ 刷新失败');
    });
}
window.onload=function(){
    restoreActiveTab();
    var s=document.getElementById('member_group_select');
    if(s&&s.value){loadMembers()}
    initTheme();
}
</script></body></html>"""

# =====================================================================
# 拼接完整的 ADMIN_HTML
# =====================================================================
ADMIN_HTML = ADMIN_HTML_CSS + ADMIN_HTML_BODY + ADMIN_HTML_JS
# =====================================================================
# Flask 路由
# =====================================================================
@app.route('/')
def index():
    """管理后台首页，渲染所有数据"""
    db = get_db()
    today = datetime.date.today().isoformat()
    cfg = {
        "comfyui_input_dir": get_config("comfyui_input_dir", str(COMFYUI_INPUT)),
        "napcat_http": get_config("napcat_http", NAPCAT_HTTP),
        "napcat_ws": get_config("napcat_ws", NAPCAT_WS),
        "comfyui_server": get_config("comfyui_server", COMFYUI_SERVER),
        "max_daily_count": get_config("max_daily_count", "20"),
        "public_help": get_config("public_help", ""),
        "admin_help": get_config("admin_help", ""),
        "help_triggers": get_config("help_triggers", "说明,帮助,help,？,?,帮助文档,使用帮助,指令,菜单,功能,教程,怎么用,使用说明"),
        "filter_enabled": get_config("filter_enabled", "false"),
        "filter_words": get_config("filter_words", ""),
        "regen_enabled": get_config("regen_enabled", "true"),
        "regen_triggers": get_config("regen_triggers", "再来,继续,加图,再来一次"),
        "drawing_mode_trigger": get_config("drawing_mode_trigger", "绘图模式"),
        "drawing_mode_duration": get_config("drawing_mode_duration", "5"),
        "private_display_flags": get_config("private_display_flags", "prompt,size,duration,workflow"),
        "regen_private_enabled": get_config("regen_private_enabled", "true"),
        "regen_group_enabled": get_config("regen_group_enabled", "true"),
        "group_display_flags": get_config("group_display_flags", "duration"),
        # ── 随便来点配置 ──
        "random_enabled": get_config("random_enabled", "false"),
        "random_trigger": get_config("random_trigger", "随便来点"),
        "random_enabled_private": get_config("random_enabled_private", "true"),
        "random_enabled_group": get_config("random_enabled_group", "true"),
        "random_sfw": get_config("random_sfw", "true"),
        "random_nsfw": get_config("random_nsfw", "false"),
        "random_usage_mode": get_config("random_usage_mode", "global"),
        "random_usage_limit": get_config("random_usage_limit", "0"),
        "random_wordbank_count": get_random_wordbank_count(),
        # ── 随便来点独立显示设置 ──
        "random_private_display_flags": get_config("random_private_display_flags", "prompt,size,duration,workflow"),
        "random_group_display_flags": get_config("random_group_display_flags", "prompt,duration"),
        # ── 白名单开关 ──
        "private_whitelist_enabled": get_config("private_whitelist_enabled", "true"),
        "group_whitelist_enabled": get_config("group_whitelist_enabled", "true"),
        # ── 停止任务配置 ──
        "stop_triggers": get_config("stop_triggers", "停止,终止,取消任务"),
        "stop_private_enabled": get_config("stop_private_enabled", "true"),
        "stop_group_enabled": get_config("stop_group_enabled", "true"),
    }
    private_list = [dict(r) for r in db.execute("SELECT qq, remark FROM private_whitelist").fetchall()]
    group_list = [dict(r) for r in db.execute("SELECT group_id, remark, enabled FROM group_whitelist").fetchall()]
    triggers = [{"word": r["word"], "workflow": r["workflow"]} for r in db.execute("SELECT * FROM trigger_words").fetchall()]
    workflows_raw = [dict(r) for r in db.execute("SELECT * FROM workflows").fetchall()]
    size_presets = load_size_presets()
    video_size_presets = load_video_size_presets()
    blacklist_count = db.execute("SELECT COUNT(*) as cnt FROM group_blacklist").fetchone()["cnt"]
    history_count = db.execute("SELECT COUNT(*) as cnt FROM history").fetchone()["cnt"]
    admin_list = [dict(r) for r in db.execute("SELECT qq, remark FROM admin_list").fetchall()]

    # 过滤词数量
    fw = get_config("filter_words", "")
    filter_word_count = len([w.strip() for w in fw.split(",") if w.strip()])

    # 过滤词预设
    filter_presets = [dict(r) for r in db.execute("SELECT id, name, words FROM filter_presets").fetchall()]

    # 节点类型标签（完整中文，6种）
    type_labels_full = {
        "positive": "提示词", "latent": "Latent", "image_input": "图片",
        "video_width": "宽度", "video_height": "高度", "duration": "时长"
    }

    # 工作流：解析 nodes_config 生成 nodes_tags（卡片用）和 nodes_display（兼容旧表格）
    workflows = []
    for wf in workflows_raw:
        wf = dict(wf)
        nodes_tags = []
        nodes_display = ""
        nc = wf.get("nodes_config", "")
        if nc:
            try:
                nodes = json.loads(nc)
                for n in nodes:
                    ntype = n.get("type", "?")
                    ntitle = n.get("title", "-")
                    nodes_tags.append({"type": ntype, "label": f"{type_labels_full.get(ntype, ntype)}:{ntitle}"})
                nodes_display = ", ".join(nt["label"] for nt in nodes_tags)
            except:
                pass
        if not nodes_display:
            parts = []
            if wf.get("positive_node_title", ""):
                t = wf["positive_node_title"]
                parts.append(f"提示词:{t}")
                nodes_tags.append({"type":"positive","label":f"提示词:{t}"})
            if wf.get("latent_node_title", ""):
                t = wf["latent_node_title"]
                parts.append(f"Latent:{t}")
                nodes_tags.append({"type":"latent","label":f"Latent:{t}"})
            if wf.get("image_node_title", ""):
                t = wf["image_node_title"]
                parts.append(f"图片:{t}")
                nodes_tags.append({"type":"image_input","label":f"图片:{t}"})
            nodes_display = ", ".join(parts)
        wf["nodes_display"] = nodes_display
        wf["nodes_tags"] = nodes_tags
        workflows.append(wf)

    # 触发词+用量合并数据（用于新卡片布局）
    triggers_map = {}
    for tw in triggers:
        triggers_map[tw["workflow"]] = tw["word"]
    trigger_data = []
    for wf in workflows:
        if wf["name"] in triggers_map:
            usage_mode = str(wf.get("usage_mode", "global") or "global").strip()
            usage_limit = int(wf.get("usage_limit", 0) or 0)
            trigger_data.append({
                "word": triggers_map[wf["name"]],
                "workflow": wf["name"],
                "usage_mode": usage_mode,
                "usage_limit": usage_limit,
            })

    # 随机词库词条详情（用于后台展示）
    random_wordbank = []
    categories = load_random_wordbank()
    if categories:
        random_wordbank = categories

    stats = {
        "today_images": db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='image' AND status='成功'", (today,)).fetchone()["c"],
        "today_texts": db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='text' AND status='成功'", (today,)).fetchone()["c"],
        "today_users": db.execute("SELECT COUNT(DISTINCT qq) as c FROM history WHERE date(time)=?", (today,)).fetchone()["c"],
        "total_images": db.execute("SELECT COUNT(*) as c FROM history WHERE output_type='image' AND status='成功'").fetchone()["c"],
        "comfyui_online": comfyui_online,
        "napcat_online": napcat_online,
    }
    db.close()
    return render_template_string(ADMIN_HTML, config=cfg, private_list=private_list, private_count=len(private_list),
                                   group_list=group_list, group_count=len(group_list), triggers=triggers,
                                   workflows=workflows, size_presets=size_presets, video_size_presets=video_size_presets,
                                   blacklist_count=blacklist_count, history_count=history_count, stats=stats,
                                   admin_list=admin_list, admin_count=len(admin_list),
                                   filter_word_count=filter_word_count, filter_presets=filter_presets,
                                   trigger_data=trigger_data, random_wordbank=random_wordbank)

# ---- API 路由 ----
@app.route('/api/admin/add', methods=['POST'])
def api_add_admin():
    d = request.json; qq = d.get('qq','').strip(); remark = d.get('remark','').strip()
    if not qq: return jsonify({"status":"error"})
    db = get_db(); db.execute("INSERT OR REPLACE INTO admin_list VALUES (?,?)",(qq,remark)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/admin/delete', methods=['POST'])
def api_del_admin():
    qq = request.json.get('qq','').strip()
    db = get_db(); db.execute("DELETE FROM admin_list WHERE qq=?",(qq,)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/private_whitelist/add', methods=['POST'])
def api_add_private():
    d = request.json; qq = d.get('qq','').strip(); remark = d.get('remark','').strip()
    if not qq: return jsonify({"status":"error"})
    db = get_db(); db.execute("INSERT OR REPLACE INTO private_whitelist VALUES (?,?)",(qq,remark)); db.commit(); db.close()
    _access_cache.clear()
    return jsonify({"status":"ok"})

@app.route('/api/private_whitelist/delete', methods=['POST'])
def api_del_private():
    qq = request.json.get('qq','').strip()
    db = get_db(); db.execute("DELETE FROM private_whitelist WHERE qq=?",(qq,)); db.commit(); db.close()
    _access_cache.clear()
    return jsonify({"status":"ok"})

@app.route('/api/private_whitelist/remark', methods=['POST'])
def api_edit_private_remark():
    d = request.json; db = get_db()
    db.execute("UPDATE private_whitelist SET remark=? WHERE qq=?",(d.get('remark',''), d.get('qq',''))); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/group_whitelist/add', methods=['POST'])
def api_add_group():
    d = request.json; gid = d.get('group_id','').strip(); remark = d.get('remark','').strip()
    if not gid: return jsonify({"status":"error"})
    db = get_db(); db.execute("INSERT OR REPLACE INTO group_whitelist VALUES (?,?,1)",(gid,remark)); db.commit(); db.close()
    _access_cache.clear()
    return jsonify({"status":"ok"})

@app.route('/api/group_whitelist/delete', methods=['POST'])
def api_del_group():
    gid = request.json.get('group_id','').strip()
    db = get_db()
    db.execute("DELETE FROM group_whitelist WHERE group_id=?",(gid,))
    db.execute("DELETE FROM group_member_whitelist WHERE group_id=?",(gid,))
    db.execute("DELETE FROM group_blacklist WHERE group_id=?",(gid,))
    db.commit(); db.close()
    _access_cache.clear()
    return jsonify({"status":"ok"})

@app.route('/api/group_whitelist/remark', methods=['POST'])
def api_edit_group_remark():
    d = request.json; db = get_db()
    db.execute("UPDATE group_whitelist SET remark=? WHERE group_id=?",(d.get('remark',''), d.get('group_id',''))); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/group_whitelist/toggle', methods=['POST'])
def api_toggle_group():
    d = request.json; gid = d.get('group_id','').strip(); en = int(d.get('enabled',1))
    db = get_db(); db.execute("UPDATE group_whitelist SET enabled=? WHERE group_id=?",(en,gid)); db.commit(); db.close()
    _access_cache.clear()
    return jsonify({"status":"ok"})

@app.route('/api/group_member/add', methods=['POST'])
def api_add_member():
    d = request.json; gid = d.get('group_id','').strip(); qq = d.get('qq','').strip(); remark = d.get('remark','').strip()
    if not gid or not qq: return jsonify({"status":"error"})
    db = get_db(); db.execute("INSERT OR REPLACE INTO group_member_whitelist VALUES (?,?,?)",(gid,qq,remark)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/group_member/delete', methods=['POST'])
def api_del_member():
    d = request.json; gid = d.get('group_id','').strip(); qq = d.get('qq','').strip()
    db = get_db(); db.execute("DELETE FROM group_member_whitelist WHERE group_id=? AND qq=?",(gid,qq)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/group_member/remark', methods=['POST'])
def api_edit_member_remark():
    d = request.json; db = get_db()
    db.execute("UPDATE group_member_whitelist SET remark=? WHERE group_id=? AND qq=?",(d.get('remark',''), d.get('group_id',''), d.get('qq',''))); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/group_member/list')
def api_list_member():
    gid = request.args.get('group_id','').strip()
    if not gid: return jsonify({"members":[]})
    db = get_db(); rows = db.execute("SELECT qq, remark FROM group_member_whitelist WHERE group_id=?",(gid,)).fetchall(); db.close()
    return jsonify({"members":[{"qq":r["qq"],"remark":r["remark"]} for r in rows]})

@app.route('/api/group_member/list_with_usage')
def api_list_member_with_usage():
    gid = request.args.get('group_id','').strip()
    if not gid: return jsonify({"members":[]})
    today = datetime.date.today().isoformat(); max_count = int(get_config("max_daily_count","20"))
    db = get_db()
    members = db.execute("SELECT qq, remark FROM group_member_whitelist WHERE group_id=?",(gid,)).fetchall()
    result = []
    for m in members:
        row = db.execute("SELECT count FROM daily_usage WHERE qq=? AND group_id=? AND date=?",(m["qq"],gid,today)).fetchone()
        result.append({"qq":m["qq"],"remark":m["remark"] or "","used":row["count"] if row else 0,"max":max_count})
    db.close()
    return jsonify({"members":result})

@app.route('/api/blacklist/add', methods=['POST'])
def api_blacklist_add():
    d = request.json; gid = d.get('group_id','').strip(); qq = d.get('qq','').strip()
    if not gid or not qq: return jsonify({"status":"error"})
    db = get_db()
    row = db.execute("SELECT remark FROM group_member_whitelist WHERE group_id=? AND qq=?",(gid,qq)).fetchone()
    remark = row["remark"] if row else ""
    db.execute("DELETE FROM group_member_whitelist WHERE group_id=? AND qq=?",(gid,qq))
    db.execute("INSERT OR REPLACE INTO group_blacklist VALUES (?,?,?)",(gid,qq,remark))
    db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/blacklist/remove', methods=['POST'])
def api_blacklist_remove():
    d = request.json; gid = d.get('group_id','').strip(); qq = d.get('qq','').strip()
    if not gid or not qq: return jsonify({"status":"error"})
    db = get_db()
    row = db.execute("SELECT remark FROM group_blacklist WHERE group_id=? AND qq=?",(gid,qq)).fetchone()
    remark = row["remark"] if row else ""
    db.execute("DELETE FROM group_blacklist WHERE group_id=? AND qq=?",(gid,qq))
    db.execute("INSERT OR REPLACE INTO group_member_whitelist VALUES (?,?,?)",(gid,qq,remark))
    db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/blacklist/list')
def api_blacklist_list():
    gid = request.args.get('group_id','').strip()
    if not gid: return jsonify({"blacklist":[]})
    db = get_db(); rows = db.execute("SELECT qq, remark FROM group_blacklist WHERE group_id=?",(gid,)).fetchall(); db.close()
    return jsonify({"blacklist":[{"qq":r["qq"],"remark":r["remark"]} for r in rows]})

def is_blacklisted(gid, qq):
    db = get_db(); row = db.execute("SELECT 1 FROM group_blacklist WHERE group_id=? AND qq=?",(gid,qq)).fetchone(); db.close()
    return row is not None

@app.route('/api/usage/reset_member', methods=['POST'])
def api_reset_member_usage():
    d = request.json; gid = d.get('group_id','').strip(); qq = d.get('qq','').strip()
    today = datetime.date.today().isoformat()
    db = get_db(); db.execute("DELETE FROM daily_usage WHERE group_id=? AND qq=? AND date=?",(gid,qq,today)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/usage/reset_group', methods=['POST'])
def api_reset_group_usage():
    gid = request.json.get('group_id','').strip()
    today = datetime.date.today().isoformat()
    db = get_db(); db.execute("DELETE FROM daily_usage WHERE group_id=? AND date=?",(gid,today)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/history/search')
def api_history_search():
    kw = request.args.get('keyword','').strip(); status = request.args.get('status','').strip()
    page = int(request.args.get('page',1)); per_page = 20; offset = (page-1)*per_page
    db = get_db()
    where_clauses = []; params = []
    if kw:
        where_clauses.append("(qq LIKE ? OR group_id LIKE ?)")
        params.extend([f"%{kw}%", f"%{kw}%"])
    if status:
        where_clauses.append("status=?"); params.append(status)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    total = db.execute(f"SELECT COUNT(*) as cnt FROM history WHERE {where_sql}", params).fetchone()["cnt"]
    rows = db.execute(f"SELECT * FROM history WHERE {where_sql} ORDER BY id DESC LIMIT {per_page} OFFSET {offset}", params).fetchall()
    db.close()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    return jsonify({"data":[dict(r) for r in rows], "total":total, "total_pages":total_pages, "page":page})

@app.route('/api/history/clear')
def api_history_clear():
    db = get_db(); db.execute("DELETE FROM history"); db.commit(); db.close()
    return jsonify({"status":"ok"})

def add_history(qq, gid, wf, ot, pt, sz, status, dur=0, msg_type=""):
    """写入历史记录，返回新记录ID（用于后续更新状态）"""
    try:
        db = get_db()
        remark = ""
        if gid:
            r = db.execute("SELECT remark FROM group_member_whitelist WHERE group_id=? AND qq=?",(gid,qq)).fetchone()
            if r: remark = r["remark"] or ""
        else:
            r = db.execute("SELECT remark FROM private_whitelist WHERE qq=?",(qq,)).fetchone()
            if r: remark = r["remark"] or ""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = db.execute("INSERT INTO history (time,qq,group_id,workflow,output_type,prompt_text,size,status,duration,remark) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (now_str, qq, gid or "", wf, ot, pt or "", sz or "", status, dur, remark))
        new_id = cursor.lastrowid
        db.commit()
        db.close()
        return new_id
    except Exception as e:
        logger.error(f"写入历史异常: {e}")
        return None

def update_history(history_id, status, duration):
    """更新历史记录的状态和耗时"""
    if not history_id:
        return
    try:
        db = get_db()
        db.execute("UPDATE history SET status=?, duration=? WHERE id=?", (status, duration, history_id))
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"更新历史异常: {e}")

@app.route('/api/trigger/add', methods=['POST'])
def api_add_trigger():
    d = request.json; word = d.get('word','').strip(); wf = d.get('workflow','').strip()
    if not word or not wf: return jsonify({"status":"error"})
    db = get_db(); db.execute("INSERT OR REPLACE INTO trigger_words VALUES (?,?)",(word,wf)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/trigger/delete', methods=['POST'])
def api_del_trigger():
    word = request.json.get('word','').strip()
    db = get_db(); db.execute("DELETE FROM trigger_words WHERE word=?",(word,)); db.commit(); db.close()
    return jsonify({"status":"ok"})

# ── 图片尺寸 API ──
@app.route('/api/size/update_trigger', methods=['POST'])
def api_update_size_trigger():
    d = request.json; name = d.get('name','').strip(); trigger_word = d.get('trigger_word','').strip()
    if not name or not trigger_word: return jsonify({"status":"error","message":"参数不完整"})
    set_config(f"size_{name}_trigger", trigger_word)
    return jsonify({"status":"ok"})

@app.route('/api/size/add', methods=['POST'])
def api_add_size():
    d = request.json; name = d.get('name','').strip(); width = int(d.get('width',0)); height = int(d.get('height',0)); trigger_word = d.get('trigger_word','').strip()
    if not name or width <= 0 or height <= 0 or not trigger_word: return jsonify({"status":"error","message":"参数不完整"})
    save_size_preset(name, width, height, trigger_word)
    return jsonify({"status":"ok"})

@app.route('/api/size/delete', methods=['POST'])
def api_del_size():
    d = request.json; name = d.get('name','').strip()
    if not name: return jsonify({"status":"error","message":"名称不能为空"})
    size_list_str = get_config("size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if len(names) <= 1:
        return jsonify({"status":"error","message":"至少保留一个尺寸"})
    delete_size_preset(name)
    return jsonify({"status":"ok"})

# ── 视频尺寸 API ──
@app.route('/api/video_size/update_trigger', methods=['POST'])
def api_update_video_size_trigger():
    d = request.json; name = d.get('name','').strip(); trigger_word = d.get('trigger_word','').strip()
    if not name or not trigger_word: return jsonify({"status":"error","message":"参数不完整"})
    set_config(f"video_size_{name}_trigger", trigger_word)
    return jsonify({"status":"ok"})

@app.route('/api/video_size/add', methods=['POST'])
def api_add_video_size():
    d = request.json; name = d.get('name','').strip(); width = int(d.get('width',0)); height = int(d.get('height',0))
    if not name or width <= 0 or height <= 0: return jsonify({"status":"error","message":"参数不完整"})
    save_video_size_preset(name, width, height, "")
    return jsonify({"status":"ok"})

@app.route('/api/video_size/delete', methods=['POST'])
def api_del_video_size():
    d = request.json; name = d.get('name','').strip()
    if not name: return jsonify({"status":"error","message":"名称不能为空"})
    size_list_str = get_config("video_size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if len(names) <= 1:
        return jsonify({"status":"error","message":"至少保留一个视频尺寸"})
    delete_video_size_preset(name)
    return jsonify({"status":"ok"})

# ---- 工作流 JSON 文件列表 API ----
@app.route('/api/workflow/files')
def api_workflow_files():
    files = scan_workflow_files()
    return jsonify({"files": files})

@app.route('/api/workflow/get')
def api_workflow_get():
    """获取单个工作流详情（用于编辑弹窗预填）"""
    name = request.args.get('name', '').strip()
    if not name: return jsonify({"status":"error","message":"名称不能为空"})
    db = get_db()
    wf = db.execute("SELECT * FROM workflows WHERE name=?", (name,)).fetchone()
    db.close()
    if not wf: return jsonify({"status":"error","message":"工作流不存在"})
    wf = dict(wf)
    # 解析 nodes_config 为数组
    nodes = []
    nc = wf.get("nodes_config", "")
    if nc:
        try:
            nodes = json.loads(nc)
        except:
            pass
    if not nodes:
        if wf.get("positive_node_title", ""): nodes.append({"type":"positive","title":wf["positive_node_title"]})
        if wf.get("latent_node_title", ""): nodes.append({"type":"latent","title":wf["latent_node_title"]})
        if wf.get("image_node_title", ""): nodes.append({"type":"image_input","title":wf["image_node_title"]})
    wf["nodes"] = nodes
    return jsonify({"status":"ok","workflow":wf})

@app.route('/api/workflow/add', methods=['POST'])
def api_add_workflow():
    d = request.json; name = d.get('name','').strip(); fp = d.get('file_path','').strip()
    if not name or not fp: return jsonify({"status":"error"})
    nodes = d.get('nodes', [])
    nodes_config = json.dumps(nodes, ensure_ascii=False) if nodes else ""
    # 兼容旧字段：如果 nodes 为空且提供了旧字段，自动转为新格式
    if not nodes:
        old_nodes = []
        if d.get('positive_node_title', '').strip(): old_nodes.append({"type": "positive", "title": d['positive_node_title'].strip()})
        if d.get('latent_node_title', '').strip(): old_nodes.append({"type": "latent", "title": d['latent_node_title'].strip()})
        if d.get('image_node_title', '').strip(): old_nodes.append({"type": "image_input", "title": d['image_node_title'].strip()})
        if old_nodes:
            nodes_config = json.dumps(old_nodes, ensure_ascii=False)
    db = get_db()
    db.execute("INSERT OR REPLACE INTO workflows (name, file_path, positive_node_title, latent_node_title, image_node_title, note, example, output_type, usage_mode, usage_limit, nodes_config) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (name, fp, d.get('positive_node_title',''), d.get('latent_node_title',''),
                d.get('image_node_title',''), d.get('note',''), d.get('example',''), d.get('output_type','image'), 'global', 0, nodes_config))
    db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/workflow/update', methods=['POST'])
def api_update_workflow():
    """编辑已有工作流"""
    d = request.json
    old_name = d.get('old_name', '').strip()
    name = d.get('name', '').strip()
    fp = d.get('file_path', '').strip()
    if not old_name or not name or not fp: return jsonify({"status":"error","message":"参数不完整"})
    nodes = d.get('nodes', [])
    nodes_config = json.dumps(nodes, ensure_ascii=False) if nodes else ""
    output_type = d.get('output_type', 'image').strip()
    note = d.get('note', '').strip()
    example = d.get('example', '').strip()
    db = get_db()
    db.execute("UPDATE workflows SET name=?, file_path=?, nodes_config=?, output_type=?, note=?, example=? WHERE name=?",
               (name, fp, nodes_config, output_type, note, example, old_name))
    db.commit(); db.close()
    # 如果名称变了，更新触发词中的引用
    if name != old_name:
        db = get_db()
        db.execute("UPDATE trigger_words SET workflow=? WHERE workflow=?", (name, old_name))
        db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/workflow/delete', methods=['POST'])
def api_del_workflow():
    name = request.json.get('name','').strip()
    db = get_db(); db.execute("DELETE FROM workflows WHERE name=?",(name,)); db.commit(); db.close()
    return jsonify({"status":"ok"})

# ---- 工作流使用限制 API ----
@app.route('/api/workflow/usage/update', methods=['POST'])
def api_update_workflow_usage():
    d = request.json; name = d.get('workflow_name','').strip(); mode = d.get('usage_mode','global').strip(); limit = int(d.get('usage_limit',0))
    if not name: return jsonify({"status":"error"})
    db = get_db()
    db.execute("UPDATE workflows SET usage_mode=?, usage_limit=? WHERE name=?", (mode, limit, name))
    db.commit(); db.close()
    return jsonify({"status":"ok"})

# ---- 过滤词预设 API ----
@app.route('/api/filter_preset/add', methods=['POST'])
def api_add_filter_preset():
    d = request.json; name = d.get('name','').strip(); words = d.get('words','').strip()
    if not name or not words: return jsonify({"status":"error","message":"参数不完整"})
    db = get_db(); db.execute("INSERT INTO filter_presets (name, words) VALUES (?,?)", (name, words)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/filter_preset/delete', methods=['POST'])
def api_del_filter_preset():
    pid = request.json.get('id', 0)
    db = get_db(); db.execute("DELETE FROM filter_presets WHERE id=?", (pid,)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/filter_preset/apply', methods=['POST'])
def api_apply_filter_preset():
    pid = request.json.get('id', 0)
    db = get_db()
    row = db.execute("SELECT words FROM filter_presets WHERE id=?", (pid,)).fetchone()
    if not row:
        db.close()
        return jsonify({"status":"error","message":"预设不存在"})
    new_words_text = row["words"]
    current = get_config("filter_words", "")
    current_list = [w.strip() for w in current.split(",") if w.strip()]
    new_list = [w.strip() for w in new_words_text.split(",") if w.strip()]
    for w in new_list:
        if w and w not in current_list:
            current_list.append(w)
    result = ",".join(current_list)
    set_config("filter_words", result)
    db.close()
    return jsonify({"status":"ok", "new_words": result})

@app.route('/api/config/update', methods=['POST'])
def api_update_config():
    d = request.json; key = d.get('key','').strip(); value = d.get('value','').strip()
    if not key: return jsonify({"status":"error"})
    set_config(key, value)
    _access_cache.clear()
    return jsonify({"status":"ok"})


# =====================================================================
# ⭐ 健康检测 API
# =====================================================================
@app.route('/api/check/napcat', methods=['POST'])
def api_check_napcat():
    """手动检测 NapCat 在线状态"""
    check_napcat_online()
    return jsonify({"status": "ok", "online": napcat_online})

@app.route('/api/check/comfyui', methods=['POST'])
def api_check_comfyui():
    """手动检测 ComfyUI 在线状态"""
    check_comfyui_online()
    return jsonify({"status": "ok", "online": comfyui_online})


# =====================================================================
# ⭐ 随便来点 - 词库管理 API
# =====================================================================

def get_random_wordbank_count():
    """返回随机词库的总词条数"""
    categories = load_random_wordbank()
    if not categories:
        return 0
    return sum(len(c.get("words", [])) for c in categories)

@app.route('/api/random/wordbank/list')
def api_random_wordbank_list():
    """获取随机词库完整数据（分类+词条）"""
    categories = load_random_wordbank(force_reload=False)
    if not categories:
        return jsonify({"categories": [], "total": 0})
    total = sum(len(c.get("words", [])) for c in categories)
    return jsonify({"categories": categories, "total": total})

@app.route('/api/random/wordbank/reload', methods=['POST'])
def api_random_wordbank_reload():
    """强制重新加载词库文件"""
    categories = load_random_wordbank(force_reload=True)
    if not categories:
        return jsonify({"status": "error", "message": "词库加载失败"})
    total = sum(len(c.get("words", [])) for c in categories)
    return jsonify({"status": "ok", "total": total, "categories_count": len(categories)})

@app.route('/api/random/wordbank/add', methods=['POST'])
def api_random_wordbank_add():
    """
    向词库中添加一条词条。
    请求格式: { "category_id": 1, "text": "long hair", "tags": ["sfw"], "zh": "长发" }
    兼容旧格式：也接受 "type": "sfw"，会自动转为 tags
    """
    d = request.json
    category_id = d.get("category_id")
    text = d.get("text", "").strip()
    wzh = d.get("zh", "").strip()

    # 兼容新旧格式：优先读取 tags，没有则从 type 自动转换
    wtags = d.get("tags", None)
    if wtags is None:
        wtype = d.get("type", "sfw").strip()
        wtags = [wtype] if wtype in ("sfw", "nsfw") else ["sfw"]
    if not isinstance(wtags, list) or len(wtags) == 0:
        wtags = ["sfw"]
    if not any(t in ("sfw", "nsfw") for t in wtags):
        wtags.append("sfw")

    if not category_id or not text:
        return jsonify({"status": "error", "message": "参数不完整"})

    wordbank_path = BASE_DIR / "wordbank.json"
    if not wordbank_path.exists():
        return jsonify({"status": "error", "message": "词库文件不存在"})

    try:
        with open(wordbank_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        categories = data.get("categories", [])
        target_cat = None
        for cat in categories:
            if cat.get("id") == category_id:
                target_cat = cat
                break

        if not target_cat:
            return jsonify({"status": "error", "message": f"分类 {category_id} 不存在"})

        # 检查是否已存在相同词条
        words = target_cat.get("words", [])
        for w in words:
            if w.get("text") == text:
                return jsonify({"status": "exists", "message": f"词条「{text}」已存在于该分类中"})

        # 添加新词条（用 tags 替代 type）
        new_word = {"text": text, "tags": wtags}
        if wzh:
            new_word["zh"] = wzh
        words.append(new_word)
        # 排序：带 nsfw 标签的排后面
        words.sort(key=lambda x: (1 if "nsfw" in x.get("tags", x.get("type", ["sfw"])) else 0, x.get("text", "")))

        with open(wordbank_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 刷新缓存
        load_random_wordbank(force_reload=True)

        return jsonify({"status": "ok", "message": f"已添加词条「{text}」到分类 {target_cat.get('name', category_id)}"})

    except Exception as e:
        logger.error(f"添加词条失败: {e}")
        return jsonify({"status": "error", "message": f"添加失败: {str(e)}"})

@app.route('/api/random/wordbank/delete', methods=['POST'])
def api_random_wordbank_delete():
    """
    从词库中删除一条词条。
    请求格式: { "category_id": 1, "text": "long hair" }
    """
    d = request.json
    category_id = d.get("category_id")
    text = d.get("text", "").strip()

    if not category_id or not text:
        return jsonify({"status": "error", "message": "参数不完整"})

    wordbank_path = BASE_DIR / "wordbank.json"
    if not wordbank_path.exists():
        return jsonify({"status": "error", "message": "词库文件不存在"})

    try:
        with open(wordbank_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        categories = data.get("categories", [])
        target_cat = None
        for cat in categories:
            if cat.get("id") == category_id:
                target_cat = cat
                break

        if not target_cat:
            return jsonify({"status": "error", "message": f"分类 {category_id} 不存在"})

        words = target_cat.get("words", [])
        new_words = [w for w in words if w.get("text") != text]

        if len(new_words) == len(words):
            return jsonify({"status": "error", "message": f"未找到词条「{text}」"})

        target_cat["words"] = new_words

        with open(wordbank_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 刷新缓存
        load_random_wordbank(force_reload=True)

        return jsonify({"status": "ok", "message": f"已删除词条「{text}」"})

    except Exception as e:
        logger.error(f"删除词条失败: {e}")
        return jsonify({"status": "error", "message": f"删除失败: {str(e)}"})

@app.route('/api/random/wordbank/search', methods=['GET'])
def api_random_wordbank_search():
    """搜索词库中的词条（用于去重检查）"""
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"results": []})

    categories = load_random_wordbank()
    if not categories:
        return jsonify({"results": []})

    results = []
    lower_kw = keyword.lower()
    for cat in categories:
        for w in cat.get("words", []):
            if lower_kw in w.get("text", "").lower():
                results.append({
                    "category_id": cat.get("id"),
                    "category_name": cat.get("name", ""),
                    "text": w.get("text"),
                    "tags": w.get("tags", [w.get("type", "sfw")])
                })

    return jsonify({"results": results})
# =====================================================================
# QQ 图片下载（用于反推，支持重试）
# =====================================================================
def download_qq_image(event):
    """从 QQ 消息事件中提取第一张图片并下载到 ComfyUI 输入目录（支持重试）"""
    try:
        message_list = event.get("message", [])
        image_url = None
        for segment in message_list:
            if segment.get("type") == "image":
                data = segment.get("data", {})
                image_url = data.get("url", "") or data.get("file", "")
                if image_url and not image_url.startswith("http"):
                    image_url = f"{get_config('napcat_http', NAPCAT_HTTP)}{image_url}"
                break
        if not image_url:
            cq_match = re.search(r'\[CQ:image,file=([^\]]+)\]', event.get("raw_message", ""))
            if cq_match:
                image_url = cq_match.group(1)
                if not image_url.startswith("http"):
                    image_url = f"{get_config('napcat_http', NAPCAT_HTTP)}{image_url}"
        if not image_url:
            return None

        filename = _download_single_file(image_url, "image")
        return filename
    except Exception as e:
        logger.error(f"图片下载异常: {e}")
        return None


def download_qq_images(event):
    """从 QQ 消息事件中按顺序提取所有图片并下载，返回文件名列表"""
    try:
        message_list = event.get("message", [])
        image_urls = []
        for segment in message_list:
            if segment.get("type") == "image":
                data = segment.get("data", {})
                url = data.get("url", "") or data.get("file", "")
                if url:
                    if not url.startswith("http"):
                        url = f"{get_config('napcat_http', NAPCAT_HTTP)}{url}"
                    image_urls.append(url)

        # 如果 segment 方式没提取到，尝试从 CQ 码中提取
        if not image_urls:
            cq_matches = re.findall(r'\[CQ:image,file=([^\]]+)\]', event.get("raw_message", ""))
            for m in cq_matches:
                url = m.strip()
                if not url.startswith("http"):
                    url = f"{get_config('napcat_http', NAPCAT_HTTP)}{url}"
                image_urls.append(url)

        if not image_urls:
            return []

        filenames = []
        for url in image_urls:
            fname = _download_single_file(url, "image")
            if fname:
                filenames.append(fname)
        return filenames
    except Exception as e:
        logger.error(f"多图片下载异常: {e}")
        return []


def _download_single_file(url, kind="image"):
    """下载单个文件（图片/视频），返回文件名。最多重试 2 次"""
    resp = None
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=60, stream=True)
            if resp.status_code == 200:
                break
            else:
                logger.error(f"{kind}下载失败(尝试{attempt+1}/2): HTTP {resp.status_code}")
        except requests.Timeout:
            logger.error(f"{kind}下载超时(尝试{attempt+1}/2): {url}")
            if attempt < 1:
                time.sleep(2)
        except Exception as e:
            logger.error(f"{kind}下载异常(尝试{attempt+1}/2): {e}")
            if attempt < 1:
                time.sleep(2)

    if not resp or resp.status_code != 200:
        logger.error(f"{kind}下载彻底失败: {url}")
        return None

    content_type = resp.headers.get("content-type", "")
    if "png" in content_type:
        ext = ".png"
    elif "gif" in content_type:
        ext = ".gif"
    elif "webp" in content_type:
        ext = ".webp"
    elif "mp4" in content_type or "video" in content_type:
        ext = ".mp4"
    else:
        ext = ".jpg"

    filename = f"artlink_{hashlib.md5(str(time.time()).encode()).hexdigest()[:12]}{ext}"
    input_dir = Path(get_config("comfyui_input_dir", str(COMFYUI_INPUT)))
    input_dir.mkdir(parents=True, exist_ok=True)
    with open(input_dir / filename, 'wb') as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    return filename


# =====================================================================
# ComfyUI 交互层
# =====================================================================
def load_workflow_json(workflow_name):
    """加载指定的工作流 JSON 文件"""
    db = get_db()
    wf = db.execute("SELECT * FROM workflows WHERE name=?", (workflow_name,)).fetchone()
    db.close()
    if not wf:
        return None, "工作流未找到"
    wf_config = dict(wf)
    path = BASE_DIR / wf_config["file_path"]
    if not path.exists():
        return None, f"文件不存在: {path}"
    with open(path, 'r', encoding='utf-8') as f:
        workflow = json.load(f)
    return workflow, wf_config


def _parse_nodes_config(wf_config):
    """
    解析工作流的节点配置，返回分类好的节点列表。
    兼容新旧格式：
    - 新格式: nodes_config 是 JSON [{"type":"positive","title":"..."}, ...]
    - 旧格式: positive_node_title / latent_node_title / image_node_title 单字段
    """
    nc = wf_config.get("nodes_config", "")
    if nc:
        try:
            return json.loads(nc)
        except:
            pass

    # 旧格式回退
    nodes = []
    if wf_config.get("positive_node_title", "").strip():
        nodes.append({"type": "positive", "title": wf_config["positive_node_title"].strip()})
    if wf_config.get("latent_node_title", "").strip():
        nodes.append({"type": "latent", "title": wf_config["latent_node_title"].strip()})
    if wf_config.get("image_node_title", "").strip():
        nodes.append({"type": "image_input", "title": wf_config["image_node_title"].strip()})
    return nodes


def _find_int_key(inputs_dict):
    """在整数节点的 inputs 中查找数字字段名：优先 'value'，否则 'int' 'number'"""
    for k in ("value", "int", "number", "integer"):
        if k in inputs_dict:
            return k
    return None


def modify_workflow(workflow, wf_config, positive_prompts=None, size_width=None, size_height=None,
                    input_image_paths=None, video_duration=None):
    """
    动态修改工作流 JSON 中的节点值。
    支持多提示词（按顺序填入）、多图片（按顺序填入）、视频时长。

    参数:
        positive_prompts: 提示词列表 [str, ...]，按顺序填到 positive 节点
        size_width / size_height: latent 节点尺寸 或 视频宽度/高度节点
        input_image_paths: 图片路径列表 [str, ...]，按顺序填到 image_input 节点
        video_duration: 视频时长（秒），填入 duration 节点
    """
    nodes_config = _parse_nodes_config(wf_config)

    # ── 提示词节点（按顺序） ──
    positive_nodes = [n for n in nodes_config if n["type"] == "positive"]
    if positive_prompts and positive_nodes:
        for i, pn in enumerate(positive_nodes):
            if i < len(positive_prompts) and positive_prompts[i]:
                target = str(pn.get("title", "")).strip()
                if target:
                    for node_id, node in workflow.items():
                        matched = (target.isdigit() and node_id == target) or (node.get("_meta", {}).get("title") == target)
                        if matched and "inputs" in node and "text" in node["inputs"]:
                            node["inputs"]["text"] = positive_prompts[i]
                            break

    # ── Latent 节点（图片/文生视频工作流） ──
    latent_nodes = [n for n in nodes_config if n["type"] == "latent"]
    if latent_nodes and size_width and size_height:
        for ln in latent_nodes:
            target = str(ln.get("title", "")).strip()
            if target:
                for node_id, node in workflow.items():
                    matched = (target.isdigit() and node_id == target) or (node.get("_meta", {}).get("title") == target)
                    if matched and "inputs" in node:
                        node["inputs"]["width"] = size_width
                        node["inputs"]["height"] = size_height
                        break

    # ── 视频宽度节点（整数节点） ──
    video_width_nodes = [n for n in nodes_config if n["type"] == "video_width"]
    if video_width_nodes and size_width:
        for vn in video_width_nodes:
            target = str(vn.get("title", "")).strip()
            if target:
                for node_id, node in workflow.items():
                    matched = (target.isdigit() and node_id == target) or (node.get("_meta", {}).get("title") == target)
                    if matched and "inputs" in node:
                        key = _find_int_key(node["inputs"])
                        if key:
                            node["inputs"][key] = size_width
                        break

    # ── 视频高度节点（整数节点） ──
    video_height_nodes = [n for n in nodes_config if n["type"] == "video_height"]
    if video_height_nodes and size_height:
        for vn in video_height_nodes:
            target = str(vn.get("title", "")).strip()
            if target:
                for node_id, node in workflow.items():
                    matched = (target.isdigit() and node_id == target) or (node.get("_meta", {}).get("title") == target)
                    if matched and "inputs" in node:
                        key = _find_int_key(node["inputs"])
                        if key:
                            node["inputs"][key] = size_height
                        break

    # ── 时长节点（整数节点） ──
    duration_nodes = [n for n in nodes_config if n["type"] == "duration"]
    if duration_nodes and video_duration is not None:
        for dn in duration_nodes:
            target = str(dn.get("title", "")).strip()
            if target:
                for node_id, node in workflow.items():
                    matched = (target.isdigit() and node_id == target) or (node.get("_meta", {}).get("title") == target)
                    if matched and "inputs" in node:
                        key = _find_int_key(node["inputs"])
                        if key:
                            node["inputs"][key] = video_duration
                        break

    # ── 图片输入节点（按顺序） ──
    image_nodes = [n for n in nodes_config if n["type"] == "image_input"]
    if input_image_paths and image_nodes:
        for i, imn in enumerate(image_nodes):
            if i < len(input_image_paths) and input_image_paths[i]:
                target = str(imn.get("title", "")).strip()
                if target:
                    for node_id, node in workflow.items():
                        matched = (target.isdigit() and node_id == target) or (node.get("_meta", {}).get("title") == target)
                        if matched and "inputs" in node:
                            key = next((k for k in ["image", "choose file to upload"] if k in node["inputs"]), None)
                            if key:
                                node["inputs"][key] = input_image_paths[i]
                                break

    return workflow


def check_comfyui_online():
    """检查 ComfyUI 是否在线，连续失败 3 次判定为离线"""
    global comfyui_online, comfyui_fail_count
    try:
        resp = requests.get(f"{get_config('comfyui_server', COMFYUI_SERVER)}/system_stats", timeout=5)
        if resp.status_code == 200:
            with comfyui_lock:
                if not comfyui_online:
                    comfyui_online = True
                    comfyui_fail_count = 0
                    logger.info("ComfyUI 已恢复连接")
                    notify_admin("✅ ComfyUI 已恢复连接")
                comfyui_fail_count = 0
            return True
    except Exception:
        pass

    with comfyui_lock:
        comfyui_fail_count += 1
        if comfyui_online and comfyui_fail_count >= 3:
            comfyui_online = False
            logger.warning("ComfyUI 失去连接")
            notify_admin("⚠️ ComfyUI 失去连接，请检查！")
    return False

def submit_to_comfyui(workflow):
    """向 ComfyUI 提交工作流任务"""
    if not check_comfyui_online():
        return None
    try:
        resp = requests.post(f"{get_config('comfyui_server', COMFYUI_SERVER)}/prompt", json={"prompt": workflow}, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("prompt_id")
    except Exception as e:
        logger.error(f"提交任务异常: {e}")
    return None

def get_history_result(prompt_id, output_type="image", max_wait=300):
    """轮询 ComfyUI 的历史接口，等待任务完成并获取结果"""
    server = get_config("comfyui_server", COMFYUI_SERVER)
    url = f"{server}/history/{prompt_id}"
    start_time = time.time()
    while time.time() - start_time < max_wait:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if prompt_id in data and "outputs" in data[prompt_id]:
                    outputs = data[prompt_id]["outputs"]
                    if output_type in ("image", "video"):
                        media_files = []
                        for node_output in outputs.values():
                            # 图片
                            if "images" in node_output:
                                for img in node_output["images"]:
                                    filename = img.get("filename", "")
                                    subfolder = img.get("subfolder", "")
                                    img_type = img.get("type", "output")
                                    view_url = f"{server}/view"
                                    params = {"filename": filename, "type": img_type}
                                    if subfolder:
                                        params["subfolder"] = subfolder
                                    img_resp = requests.get(view_url, params=params, timeout=30)
                                    if img_resp.status_code == 200:
                                        temp_dir = BASE_DIR / ("temp_video" if output_type == "video" else "temp_images")
                                        temp_dir.mkdir(exist_ok=True)
                                        local_path = temp_dir / filename
                                        with open(local_path, 'wb') as f:
                                            f.write(img_resp.content)
                                        media_files.append(str(local_path))
                            # GIF（视频工作流可能输出GIF）
                            if "gifs" in node_output:
                                for gif in node_output["gifs"]:
                                    filename = gif.get("filename", "")
                                    subfolder = gif.get("subfolder", "")
                                    gif_type = gif.get("type", "output")
                                    view_url = f"{server}/view"
                                    params = {"filename": filename, "type": gif_type}
                                    if subfolder:
                                        params["subfolder"] = subfolder
                                    gif_resp = requests.get(view_url, params=params, timeout=30)
                                    if gif_resp.status_code == 200:
                                        temp_dir = BASE_DIR / "temp_video"
                                        temp_dir.mkdir(exist_ok=True)
                                        local_path = temp_dir / filename
                                        with open(local_path, 'wb') as f:
                                            f.write(gif_resp.content)
                                        media_files.append(str(local_path))
                        return media_files
                    elif output_type == "text":
                        texts = []
                        for node_output in outputs.values():
                            for key in ("text", "string", "output"):
                                if key in node_output:
                                    for t in node_output[key]:
                                        if t and str(t).strip():
                                            texts.append(str(t).strip())
                        return texts
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return []

# =====================================================================
# 消息撤回与发送
# =====================================================================
def delete_msg(message_id):
    """调用 NapCat API 撤回消息"""
    try:
        requests.post(f"{get_config('napcat_http', NAPCAT_HTTP)}/delete_msg", json={"message_id": message_id}, timeout=5)
        return True
    except Exception as e:
        logger.error(f"撤回消息异常: {e}")
        return False

def schedule_delete(message_id, delay=15):
    """延迟撤回消息（在后台线程中执行）"""
    def _del():
        time.sleep(delay)
        delete_msg(message_id)
    threading.Thread(target=_del, daemon=True).start()

def send_private_message(qq, message):
    """发送私聊消息"""
    try:
        requests.post(f"{get_config('napcat_http', NAPCAT_HTTP)}/send_private_msg",
                      json={"user_id": int(qq), "message": message}, timeout=10)
    except Exception as e:
        logger.error(f"发送私聊消息异常: {e}")

def send_msg(qq, gid, msg_type, text):
    """通用消息发送，返回 message_id"""
    try:
        if msg_type == "private":
            url = f"{get_config('napcat_http', NAPCAT_HTTP)}/send_private_msg"
            payload = {"user_id": int(qq), "message": text}
        else:
            url = f"{get_config('napcat_http', NAPCAT_HTTP)}/send_group_msg"
            payload = {"group_id": int(gid), "message": text}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("message_id")
    except Exception as e:
        logger.error(f"发送消息异常: {e}")
    return None

def get_group_nickname(gid, qq):
    """获取群昵称，用于自助注册时的备注"""
    try:
        resp = requests.get(f"{get_config('napcat_http', NAPCAT_HTTP)}/get_group_member_info",
                            params={"group_id": int(gid), "user_id": int(qq)}, timeout=5)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            return data.get("card", "") or data.get("nickname", "") or qq
    except Exception as e:
        logger.error(f"获取群昵称异常: {e}")
    return qq

def interrupt_comfyui():
    """调用 ComfyUI 的 /interrupt 接口中断当前任务"""
    try:
        resp = requests.post(f"{get_config('comfyui_server', COMFYUI_SERVER)}/interrupt", json={}, timeout=5)
        if resp.status_code == 200:
            logger.info("ComfyUI 任务中断成功")
            return True
        else:
            logger.error(f"ComfyUI 中断失败: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"ComfyUI 中断异常: {e}")
        return False

# =====================================================================
# 帮助信息
# =====================================================================
ADMIN_HELP = r"""🔐 管理员说明
【管理菜单】(私聊发送 管理)
  管理 → 进入管理菜单
    1.群聊管理 → 成员增删/拉黑/重置/黑名单
    2.私聊管理 → 私聊白名单增删
    3.其他设置 → 开关控制+配置调整
        开关控制（回复 序号+开/关）：
          1 开/关 → 提示词过滤
          2 开/关 → 群聊白名单
          3 开/关 → 随便来点
          4 开/关 → 随便来点 私聊
          5 开/关 → 随便来点 群聊
          6 开/关 → 随便来点 SFW
          7 开/关 → 随便来点 NSFW
        其他设置：8过滤词 9上限 10说明 11再来一张
【快捷指令】(私聊直接发送)
  状态 → 查看系统运行状态
  过滤开/过滤关 → 控制提示词过滤
  群聊开/群聊关 → 群聊白名单总开关
  重置群 群号 → 重置整群次数
  拉黑 群号 QQ号 → 拉黑群成员
  移除 群号 QQ号 → 移除群成员
  重置 群号 QQ号 → 重置成员次数
  添加群 群号 → 添加群白名单
  添加私聊 QQ号 备注 → 添加私聊白名单
  移除私聊 QQ号 → 移除私聊白名单
【系统功能】
  自助注册 → 群内 @机器人 注册
  说明(帮助/help等) → 查看帮助
  绘图 → 触发生图（消耗次数）
  反推 → 触发图片反推（不消耗次数）
  撤回 → 群内直接发送"撤回"
  停止 → 停止自己当前任务
  尺寸触发词 → 切换尺寸
  私聊「再来」→ 用上次提示词再生成（支持1-5张）
  群聊「再来」→ 用上次提示词再生成1张（无需@）
  私聊「绘图模式」→ 免触发词生图
  私聊「随便来点」→ 随机提示词生图（需后台开启）
  群聊「随便来点」→ 随机提示词生图（需@机器人）
【后台管理】http://127.0.0.1:5000
【当前状态】ComfyUI: {comfyui_status} 排队: {queue_count} 任务"""

def send_help(qq, msg_type, gid=None):
    """发送帮助信息（管理员私聊显示管理版，普通用户显示公共说明）"""
    if msg_type == "private" and is_admin(qq):
        custom = get_config("admin_help", "").strip()
        if custom:
            txt = custom
        else:
            txt = ADMIN_HELP.format(comfyui_status="在线" if comfyui_online else "离线", queue_count=queue_counter)
    else:
        txt = get_config("public_help", "").strip() or "📋 帮助\n发送 说明 查看帮助"
    mid = send_msg(qq, gid, msg_type, txt)
    if mid and msg_type == "group":
        schedule_delete(mid, 15)

# =====================================================================
# 全局任务管理
# =====================================================================
task_queue = queue.Queue()
prompt_context = {}
start_time_global = time.time()

# 私聊「再来一张」记忆
last_private_prompt = {}

# 群聊「再来一张」记忆
last_group_prompt = {}
last_group_prompt_lock = threading.Lock()

# 私聊「绘图模式」状态
drawing_mode_states = {}
drawing_mode_lock = threading.Lock()

# 随机词库缓存
_wordbank_cache = None
_wordbank_cache_time = 0
_wordbank_cache_lock = threading.Lock()
WORD_BANK_CACHE_TTL = 600  # 缓存有效期600秒（10分钟）

# 白名单权限缓存（本次启动有效，重启/配置变更后清空）
_access_cache = {}

# 确认状态机（多提示词/多图片确认）key: "private_QQ" 或 "group_GID_QQ"
pending_confirm_states = {}
pending_confirm_lock = threading.Lock()

# 视频尺寸询问状态 key: "private_QQ" 或 "group_GID_QQ"
video_size_states = {}
video_size_lock = threading.Lock()

# 视频时长询问状态 key: "private_QQ" 或 "group_GID_QQ"
video_duration_states = {}
video_duration_lock = threading.Lock()

# ⭐ 群聊工作流记忆缓存 key: "群号_QQ号" -> 工作流名，重启清空
group_last_workflow = {}

# =====================================================================
# 导入外挂过滤规则
# =====================================================================
import sys
sys.path.insert(0, str(BASE_DIR))
from wordbank_filter import filter_by_rules, load_relations

def load_random_wordbank(force_reload=False):
    """
    从 wordbank.json 加载随机词库，返回分类列表。
    缓存机制：10分钟最多重新读取一次文件，避免频繁IO。
    """
    global _wordbank_cache, _wordbank_cache_time

    with _wordbank_cache_lock:
        now = time.time()
        if not force_reload and _wordbank_cache and (now - _wordbank_cache_time) < WORD_BANK_CACHE_TTL:
            return _wordbank_cache

        wordbank_path = BASE_DIR / "wordbank.json"
        if not wordbank_path.exists():
            logger.warning("wordbank.json 文件不存在")
            _wordbank_cache = None
            return None

        try:
            with open(wordbank_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            categories = data.get("categories", [])
            # 按 id 排序，确保槽位顺序正确
            categories.sort(key=lambda c: c.get("id", 0))
            _wordbank_cache = categories
            _wordbank_cache_time = now
            logger.info(f"已加载随机词库: {sum(len(c.get('words', [])) for c in categories)} 条词, {len(categories)} 个分类")
            return categories
        except Exception as e:
            logger.error(f"加载随机词库失败: {e}")
            _wordbank_cache = None
            return None

def _get_word_tags(word):
    """获取词条的标签码列表（兼容新旧格式）"""
    tags = word.get("tags", None)
    if tags is None:
        # 旧格式回退
        return [word.get("type", "sfw")]
    return tags if isinstance(tags, list) else [tags]

def _collect_selected_tags(picked_by_category):
    """从已挑选的词中收集所有标签码集合"""
    all_tags = set()
    for words in picked_by_category.values():
        for w_text in words:
            all_tags.add(w_text.lower())
    return all_tags

def generate_random_prompt(sfw_enabled=True, nsfw_enabled=False, retry_count=0):
    """
    从 wordbank.json 中随机组合一条提示词。
    支持外挂过滤规则 + 吸引权重 + 对立检测。
    兼容新旧词库格式。
    """
    categories = load_random_wordbank()
    if not categories:
        return None

    if not sfw_enabled and not nsfw_enabled:
        return None

    MAX_RETRY = 5

    # ── 加载吸引关系 ──
    attract_relations = []
    try:
        all_relations = load_relations()
        attract_relations = [r for r in all_relations if r.get("relation") == "吸引"]
    except:
        pass

    selected_tags = []
    picked_by_category = {}

    for cat in categories:
        words = cat.get("words", [])
        if not words:
            continue

        cat_id = cat.get("id", 0)

        # 筛选符合条件的词
        valid_words = []
        for w in words:
            w_tags = _get_word_tags(w)
            if (sfw_enabled and "sfw" in w_tags) or (nsfw_enabled and "nsfw" in w_tags):
                valid_words.append(w)

        if not valid_words:
            continue

        pick_min = cat.get("pick_min", 0)
        pick_max = cat.get("pick_max", 1)
        pick_count = random.randint(pick_min, pick_max)

        if pick_count <= 0:
            continue

        # ── 吸引权重计算 ──
        if attract_relations and pick_count > 0:
            selected_so_far = _collect_selected_tags(picked_by_category)

            # 构建加权列表
            weighted_pool = []
            for w in valid_words:
                w_tags = _get_word_tags(w)
                w_tags_lower = [t.lower() for t in w_tags]

                # 基础权重
                weight = 1.0

                # 检查吸引关系：该词的标签是否被前面已选标签吸引
                for rel in attract_relations:
                    tag_a = rel["tag_a"].lower()
                    tag_b = rel["tag_b"].lower()
                    strength = rel.get("strength", "中")

                    # 如果前面选了 tag_a，且当前词包含 tag_b
                    if tag_a in selected_so_far and tag_b in w_tags_lower:
                        if strength == "强":
                            weight *= 3.0
                        elif strength == "中":
                            weight *= 2.0
                        else:
                            weight *= 1.5

                    # 反过来：如果前面选了 tag_b，且当前词包含 tag_a
                    if tag_b in selected_so_far and tag_a in w_tags_lower:
                        if strength == "强":
                            weight *= 3.0
                        elif strength == "中":
                            weight *= 2.0
                        else:
                            weight *= 1.5

                weighted_pool.append((w, weight))

            # 按权重随机选取
            picked = []
            remaining = weighted_pool[:]
            for _ in range(min(pick_count, len(remaining))):
                if not remaining:
                    break
                total_weight = sum(wt for _, wt in remaining)
                if total_weight <= 0:
                    break
                r = random.uniform(0, total_weight)
                cumulative = 0
                chosen = remaining[0]
                for item, wt in remaining:
                    cumulative += wt
                    if r <= cumulative:
                        chosen = item
                        break
                picked.append(chosen)
                remaining = [(item, wt) for item, wt in remaining if item != chosen]
        else:
            picked = random.sample(valid_words, min(pick_count, len(valid_words)))

        picked_texts = [p["text"] for p in picked]
        selected_tags.extend(picked_texts)
        picked_by_category[cat_id] = picked_texts

    if not selected_tags:
        return None

    full_prompt = ", ".join(selected_tags)

    # ── 外挂过滤规则（含对立检测）──
    if retry_count <= MAX_RETRY:
        try:
            picked_by_category, full_prompt, needs_retry = filter_by_rules(
                picked_by_category, sfw_enabled, nsfw_enabled, full_prompt
            )
            if needs_retry:
                logger.info(f"过滤规则建议重抽，第{retry_count+1}次")
                return generate_random_prompt(sfw_enabled, nsfw_enabled, retry_count + 1)
        except Exception as e:
            logger.warning(f"外挂过滤规则异常: {e}，跳过")

    return full_prompt

def get_user_size(user_key):
    """获取用户当前的尺寸记忆"""
    db = get_db()
    row = db.execute("SELECT width, height FROM user_size WHERE user_key=?", (user_key,)).fetchone()
    db.close()
    if row:
        return (row["width"], row["height"])
    return get_default_size()[1:]

def save_user_size(user_key, width, height):
    """保存用户的尺寸记忆"""
    db = get_db()
    db.execute("INSERT OR REPLACE INTO user_size VALUES (?,?,?)", (user_key, width, height))
    db.commit()
    db.close()

def check_group_permission(gid, qq):
    """检查群内用户的使用权限，支持 -1 无限制"""
    db = get_db()
    row = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, qq)).fetchone()
    if not row:
        db.close()
        return False, "您没有使用权限"
    today = datetime.date.today().isoformat()
    row = db.execute("SELECT count FROM daily_usage WHERE qq=? AND group_id=? AND date=?", (qq, gid, today)).fetchone()
    used = row["count"] if row else 0
    max_count = int(get_config("max_daily_count", "20"))
    db.close()
    # ── 支持 -1 无限制 ──
    if max_count == -1:
        return True, None
    if used >= max_count:
        return False, f"今日次数已用完 ({max_count}/天)"
    return True, None

def check_workflow_limit(gid, qq, workflow_name):
    """检查工作流单独限制，返回 (allowed, reason, used_global)"""
    db = get_db()
    wf = db.execute("SELECT usage_mode, usage_limit FROM workflows WHERE name=?", (workflow_name,)).fetchone()
    db.close()
    if not wf:
        return True, None, True

    mode = str(wf["usage_mode"] or "global").strip()
    limit = int(wf["usage_limit"] or 0)

    if mode == "unlimited":
        return True, None, False

    today = datetime.date.today().isoformat()

    if mode == "limited":
        db = get_db()
        row = db.execute("SELECT count FROM workflow_daily_usage WHERE qq=? AND group_id=? AND workflow=? AND date=?", (qq, gid, workflow_name, today)).fetchone()
        used = row["count"] if row else 0
        db.close()
        if used >= limit:
            return False, f"该工作流今日次数已用完 ({limit}/天)", False
        return True, None, False

    return True, None, True

def increment_usage(gid, qq, workflow_name=None, use_global=True):
    """增加使用次数计数"""
    today = datetime.date.today().isoformat()
    db = get_db()
    if use_global:
        db.execute("INSERT INTO daily_usage VALUES (?,?,?,1) ON CONFLICT(qq, group_id, date) DO UPDATE SET count=count+1", (qq, gid, today))
    if workflow_name:
        db.execute("INSERT INTO workflow_daily_usage VALUES (?,?,?,?,1) ON CONFLICT(qq, group_id, workflow, date) DO UPDATE SET count=count+1", (qq, gid, workflow_name, today))
    db.commit()
    db.close()

def has_image(event):
    """检查消息中是否包含图片"""
    for segment in event.get("message", []):
        if segment.get("type") == "image":
            return True
    return "[CQ:image" in event.get("raw_message", "")

def count_images(event):
    """统计消息中包含的图片数量"""
    count = 0
    for segment in event.get("message", []):
        if segment.get("type") == "image":
            count += 1
    if count == 0:
        cq_matches = re.findall(r'\[CQ:image', event.get("raw_message", ""))
        count = len(cq_matches)
    return count

def handle_register(sqq, gid, mt, at_bot_prefix=None):
    """处理用户注册请求"""
    if is_blacklisted(gid, sqq):
        return True
    db = get_db()
    existing = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, sqq)).fetchone()
    if existing:
        db.close()
        msg_id = send_msg(None, gid, mt, f"[CQ:at,qq={sqq}] 已在白名单")
        if msg_id:
            schedule_delete(msg_id, 15)
        return True
    nickname = get_group_nickname(gid, sqq)
    db.execute("INSERT OR REPLACE INTO group_member_whitelist VALUES (?,?,?)", (gid, sqq, nickname))
    db.commit()
    db.close()
    logger.info(f"自助注册: 群{gid} QQ{sqq} ({nickname})")
    send_msg(None, gid, mt, f"[CQ:at,qq={sqq}] ✅ 注册成功，剩余{get_config('max_daily_count','20')}次（每日0点刷新）")
    return True

# =====================================================================
# 中文数字转换（增加"两"）
# =====================================================================
CN_NUMS = {"一":1, "二":2, "三":3, "四":4, "五":5, "两":2}

def parse_regen_count(msg):
    """解析"再来x张"中的数量，支持数字1-5和中文一二三四五两，默认返回1"""
    m = re.search(r'[再来继续加图再来一次]+(\d+)[张次遍]?$', msg)
    if m and m.group(1):
        n = int(m.group(1))
        if 1 <= n <= 5:
            return n
    for cn, num in CN_NUMS.items():
        if cn in msg:
            return num
    return 1

# =====================================================================
# 再来一张功能开关检查
# =====================================================================
def is_regen_enabled_for_private():
    return get_config("regen_private_enabled", "true") == "true"

def is_regen_enabled_for_group():
    return get_config("regen_group_enabled", "true") == "true"

# =====================================================================
# 随便来点功能开关检查
# =====================================================================
def is_random_enabled():
    """检查随便来点功能总开关"""
    return get_config("random_enabled", "false") == "true"

def is_random_enabled_for_private():
    """检查私聊是否开启随便来点"""
    return get_config("random_enabled_private", "true") == "true"

def is_random_enabled_for_group():
    """检查群聊是否开启随便来点"""
    return get_config("random_enabled_group", "true") == "true"

def get_random_trigger():
    """获取随便来点触发词"""
    return get_config("random_trigger", "随便来点").strip()

def is_random_sfw_enabled():
    """检查 SFW 是否勾选"""
    return get_config("random_sfw", "true") == "true"

def is_random_nsfw_enabled():
    """检查 NSFW 是否勾选"""
    return get_config("random_nsfw", "false") == "true"

# =====================================================================
# 日志记录增强（控制台更详细，不显示提示词）
# =====================================================================
def log_task_submit(sender_qq, group_id, workflow_name, output_type, msg_type_label):
    if group_id:
        logger.info(f"[提交] QQ:{sender_qq} 群:{group_id} 工作流:{workflow_name} 类型:{msg_type_label}")
    else:
        logger.info(f"[提交] QQ:{sender_qq} 私聊 工作流:{workflow_name} 类型:{msg_type_label}")

def log_task_result(sender_qq, group_id, workflow_name, status, duration, has_image=False, has_text=False):
    result_str = "成功"
    if has_image:
        result_str += " [图片]"
    if has_text:
        result_str += " [文字]"
    if not status:
        result_str = "失败"
    if group_id:
        logger.info(f"[结果] QQ:{sender_qq} 群:{group_id} 工作流:{workflow_name} {result_str} ({duration:.0f}秒)")
    else:
        logger.info(f"[结果] QQ:{sender_qq} 私聊 工作流:{workflow_name} {result_str} ({duration:.0f}秒)")
# =====================================================================
# 管理员菜单系统
# =====================================================================
admin_menu = {}  # 存储每个管理员的菜单状态

def menu_send(sqq, text, menu_state):
    """发送菜单消息并更新状态"""
    last_id = menu_state.get("last_msg_id")
    if last_id:
        delete_msg(last_id)
    msg_id = send_msg(sqq, None, "private", text)
    menu_state["last_msg_id"] = msg_id
    menu_state["last_active"] = time.time()
    return msg_id

def handle_admin_menu(sqq, msg):
    """管理员菜单状态机，根据 mode/step 路由到不同的处理逻辑"""
    if not is_admin(sqq):
        return False

    now = time.time()
    if sqq not in admin_menu:
        if msg == "管理":
            menu = {"mode": "main", "last_active": now, "last_msg_id": None, "confirm": None}
            admin_menu[sqq] = menu
            menu_send(sqq, "📋 管理菜单\n1. 群聊管理\n2. 私聊管理\n3. 其他设置\n回复序号或 退出", menu)
            return True
        return False

    menu = admin_menu[sqq]
    menu["last_active"] = now

    # 退出/返回主菜单
    if msg in ("退出", "返回主菜单"):
        last = menu.get("last_msg_id")
        if last:
            delete_msg(last)
        del admin_menu[sqq]
        mid = send_msg(sqq, None, "private", "已退出管理菜单")
        if mid:
            schedule_delete(mid, 15)
        return True

    # 主菜单路由
    if menu.get("mode") == "main":
        if msg == "1":
            menu["mode"] = "group"; menu["step"] = "list_groups"; menu["group_id"] = None
            return show_group_list(sqq)
        elif msg == "2":
            menu["mode"] = "private"; menu["step"] = "list_private"
            return show_private_list(sqq)
        elif msg == "3":
            menu["mode"] = "other"; menu["step"] = "main"
            return show_other_menu(sqq)
        else:
            menu_send(sqq, "请输入 1、2、3 或 退出", menu)
            return True

    # 群聊管理子菜单
    if menu.get("mode") == "group":
        return handle_group_menu(sqq, msg, menu)

    # 私聊管理子菜单
    if menu.get("mode") == "private":
        return handle_private_menu(sqq, msg, menu)

    # 其他设置子菜单
    if menu.get("mode") == "other":
        return handle_other_menu(sqq, msg, menu)

    menu_send(sqq, "未知指令，回复 退出 退出菜单", menu)
    return True

def handle_group_menu(sqq, msg, menu):
    """群聊管理子菜单"""
    if menu.get("step") == "list_groups":
        if msg == "返回":
            menu["mode"] = "main"; menu["step"] = None
            menu.pop("group_id", None); menu.pop("confirm", None)
            return menu_send(sqq, "📋 管理菜单\n1. 群聊管理\n2. 私聊管理\n3. 其他设置\n回复序号或 退出", menu)
        if msg.startswith("添加 "):
            gid = msg.split(" ", 1)[1].strip()
            if gid.isdigit() and len(gid) >= 5:
                db = get_db()
                db.execute("INSERT OR IGNORE INTO group_whitelist (group_id) VALUES (?)", (gid,))
                db.commit(); db.close()
                menu_send(sqq, f"✅ 群 {gid} 已添加", menu); return show_group_list(sqq)
            else:
                menu_send(sqq, "群号格式错误", menu); return True
        if msg.startswith("移除 "):
            gid = msg.split(" ", 1)[1].strip()
            if gid.isdigit():
                if menu.get("confirm") != f"remove_group_{gid}":
                    menu["confirm"] = f"remove_group_{gid}"
                    menu_send(sqq, f"⚠️ 确认移除群 {gid} 及其所有成员？回复 确认移除 {gid}", menu)
                    return True
                else:
                    del menu["confirm"]
                    db = get_db()
                    db.execute("DELETE FROM group_whitelist WHERE group_id=?", (gid,))
                    db.execute("DELETE FROM group_member_whitelist WHERE group_id=?", (gid,))
                    db.execute("DELETE FROM group_blacklist WHERE group_id=?", (gid,))
                    db.commit(); db.close()
                    menu_send(sqq, f"✅ 群 {gid} 已移除", menu); return show_group_list(sqq)
            else:
                menu_send(sqq, "群号格式错误", menu); return True
        if msg.isdigit():
            db = get_db()
            groups = db.execute("SELECT group_id, remark FROM group_whitelist ORDER BY rowid").fetchall()
            db.close()
            idx = int(msg) - 1
            if 0 <= idx < len(groups):
                gid = groups[idx]["group_id"]
                menu["group_id"] = gid; menu["step"] = "manage_members"
                return show_member_list(sqq, gid)
            else:
                menu_send(sqq, "序号无效", menu); return True
        menu_send(sqq, "未知指令\n回复：数字选择 / 添加 群号 / 移除 群号 / 返回", menu); return True

    if menu.get("step") == "manage_members":
        gid = menu.get("group_id")
        if not gid:
            del admin_menu[sqq]; return handle_admin_menu(sqq, "管理")

        # 移除成员确认
        if msg == "确认" and menu.get("confirm", "").startswith("remove_"):
            target_qq = menu["confirm"].split("_", 1)[1]
            db = get_db()
            db.execute("DELETE FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, target_qq))
            db.commit(); db.close()
            del menu["confirm"]
            menu_send(sqq, f"✅ 已移除 {target_qq}", menu); return show_member_list(sqq, gid)

        if menu.get("confirm"):
            menu.pop("confirm", None)

        # 黑名单查看
        if msg == "黑名单":
            return show_group_blacklist(sqq, gid)

        if msg == "返回":
            menu["step"] = "list_groups"; menu.pop("group_id", None); menu.pop("confirm", None)
            return show_group_list(sqq)
        if msg == "全部重置":
            today = datetime.date.today().isoformat()
            db = get_db()
            db.execute("DELETE FROM daily_usage WHERE group_id=? AND date=?", (gid, today))
            db.commit(); db.close()
            menu_send(sqq, "✅ 已重置该群所有成员今日次数", menu); return show_member_list(sqq, gid)
        if msg.startswith("拉黑 "):
            parts = msg.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                db = get_db()
                members = db.execute("SELECT qq, remark FROM group_member_whitelist WHERE group_id=?", (gid,)).fetchall()
                db.close()
                if 0 <= idx < len(members):
                    target_qq = members[idx]["qq"]
                    if menu.get("confirm") != f"ban_{target_qq}":
                        menu["confirm"] = f"ban_{target_qq}"
                        menu_send(sqq, f"⚠️ 确认拉黑 {target_qq}（{members[idx]['remark']}）？回复 确认拉黑 {target_qq}", menu)
                        return True
                    else:
                        del menu["confirm"]
                        db = get_db()
                        row = db.execute("SELECT remark FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, target_qq)).fetchone()
                        remark = row["remark"] if row else ""
                        db.execute("DELETE FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, target_qq))
                        db.execute("INSERT OR REPLACE INTO group_blacklist VALUES (?,?,?)", (gid, target_qq, remark))
                        db.commit(); db.close()
                        menu_send(sqq, f"✅ 已拉黑 {target_qq}", menu); return show_member_list(sqq, gid)
                else:
                    menu_send(sqq, "序号无效", menu)
            else:
                menu_send(sqq, "格式：拉黑 序号", menu)
            return True
        if msg.startswith("移除 "):
            parts = msg.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                db = get_db()
                members = db.execute("SELECT qq, remark FROM group_member_whitelist WHERE group_id=?", (gid,)).fetchall()
                db.close()
                if 0 <= idx < len(members):
                    target_qq = members[idx]["qq"]
                    menu["confirm"] = f"remove_{target_qq}"
                    menu_send(sqq, f"⚠️ 确认移除 {target_qq}（{members[idx]['remark']}）？回复 确认", menu)
                    return True
                else:
                    menu_send(sqq, "序号无效", menu)
            else:
                menu_send(sqq, "格式：移除 序号", menu)
            return True
        if msg.startswith("重置 "):
            parts = msg.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                db = get_db()
                members = db.execute("SELECT qq FROM group_member_whitelist WHERE group_id=?", (gid,)).fetchall()
                db.close()
                if 0 <= idx < len(members):
                    target_qq = members[idx]["qq"]
                    today = datetime.date.today().isoformat()
                    db = get_db()
                    db.execute("DELETE FROM daily_usage WHERE group_id=? AND qq=? AND date=?", (gid, target_qq, today))
                    db.commit(); db.close()
                    menu_send(sqq, f"✅ 已重置 {target_qq} 今日次数", menu); return show_member_list(sqq, gid)
                else:
                    menu_send(sqq, "序号无效", menu)
            else:
                menu_send(sqq, "格式：重置 序号", menu)
            return True
        if msg.startswith("添加成员 "):
            rest = msg[len("添加成员 "):].strip()
            parts = rest.split()
            qq = parts[0] if parts else ""
            remark = " ".join(parts[1:]) if len(parts) > 1 else ""
            if qq.isdigit():
                db = get_db()
                db.execute("INSERT OR REPLACE INTO group_member_whitelist VALUES (?,?,?)", (gid, qq, remark))
                db.commit(); db.close()
                menu_send(sqq, f"✅ 已添加 {qq}", menu); return show_member_list(sqq, gid)
            else:
                menu_send(sqq, "格式：添加成员 QQ号 备注", menu); return True
        menu_send(sqq, "未知指令", menu); return True

    return True

def handle_private_menu(sqq, msg, menu):
    """私聊管理子菜单"""
    if menu.get("step") == "list_private":
        if msg == "返回":
            menu["mode"] = "main"; menu["step"] = None
            return menu_send(sqq, "📋 管理菜单\n1. 群聊管理\n2. 私聊管理\n3. 其他设置\n回复序号或 退出", menu)
        if msg.startswith("添加 "):
            rest = msg[len("添加 "):].strip()
            parts = rest.split()
            qq = parts[0] if parts else ""
            remark = " ".join(parts[1:]) if len(parts) > 1 else ""
            if qq.isdigit():
                db = get_db()
                db.execute("INSERT OR REPLACE INTO private_whitelist VALUES (?,?)", (qq, remark))
                db.commit(); db.close()
                menu_send(sqq, f"✅ 已添加 {qq}", menu); return show_private_list(sqq)
            else:
                menu_send(sqq, "格式：添加 QQ号 备注", menu); return True
        if msg.startswith("移除 "):
            parts = msg.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                db = get_db()
                members = db.execute("SELECT qq FROM private_whitelist").fetchall()
                db.close()
                if 0 <= idx < len(members):
                    target_qq = members[idx]["qq"]
                    db = get_db()
                    db.execute("DELETE FROM private_whitelist WHERE qq=?", (target_qq,))
                    db.commit(); db.close()
                    menu_send(sqq, f"✅ 已移除 {target_qq}", menu); return show_private_list(sqq)
                else:
                    menu_send(sqq, "序号无效", menu)
            else:
                menu_send(sqq, "格式：移除 序号", menu)
            return True
        menu_send(sqq, "未知指令\n回复：添加 QQ号 备注 / 移除 序号 / 返回", menu); return True

    return show_private_list(sqq)

def handle_other_menu(sqq, msg, menu):
    """其他设置子菜单（开关控制 + 过滤词、每日上限、公共说明、再来一张设置）"""
    step = menu.get("step", "main")
    if step == "main":
        if msg == "返回":
            menu["mode"] = "main"; menu["step"] = None; menu.pop("other_confirm", None)
            return menu_send(sqq, "📋 管理菜单\n1. 群聊管理\n2. 私聊管理\n3. 其他设置\n回复序号或 退出", menu)

        # ── 解析 序号+开/关 开关指令（1-7）──
        parts = msg.strip().split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1] in ("开", "关"):
            idx = int(parts[0])
            is_on = (parts[1] == "开")
            switch_map = {
                1: ("filter_enabled", "提示词过滤"),
                2: ("group_whitelist_enabled", "群聊白名单"),
                3: ("random_enabled", "随便来点"),
                4: ("random_enabled_private", "随便来点 私聊"),
                5: ("random_enabled_group", "随便来点 群聊"),
                6: ("random_sfw", "随便来点 SFW"),
                7: ("random_nsfw", "随便来点 NSFW"),
            }
            if idx in switch_map:
                key, name = switch_map[idx]
                set_config(key, "true" if is_on else "false")
                _access_cache.clear()
                menu_send(sqq, f"✅ {name} 已{'开启' if is_on else '关闭'}", menu)
                return show_other_menu(sqq)
            else:
                menu_send(sqq, "序号无效，请输入 1-7", menu)
                return True

        # ── 子菜单导航（8-11）──
        if msg == "8":
            words = get_config("filter_words", "")
            word_list = [w.strip() for w in words.split(",") if w.strip()]
            menu["step"] = "filter_words"
            menu_send(sqq, f"📋 当前过滤词（{len(word_list)}个）: {', '.join(word_list) if word_list else '无'}\n回复：添加 xxx / 删除 xxx / 返回", menu)
            return True
        if msg == "9":
            current_limit = get_config("max_daily_count", "20")
            menu["step"] = "daily_limit"
            menu_send(sqq, f"⚠️ 当前每日上限: {current_limit} 次\n回复 数字 修改，回复 返回 取消", menu)
            return True
        if msg == "10":
            current_help = get_config("public_help", "")
            menu["step"] = "public_help"
            preview = current_help[:200] + ("..." if len(current_help) > 200 else "")
            menu_send(sqq, f"📋 当前公共说明:\n{preview}\n\n回复 新文本 替换，回复 返回 取消", menu)
            return True
        if msg == "11":
            menu["step"] = "regen_settings"
            return show_regen_settings(sqq, menu)

        # ── 纯数字提示（如果是1-7但没有开/关）──
        if msg.isdigit() and 1 <= int(msg) <= 7:
            menu_send(sqq, "请用 序号+开/关 格式，如「1 关」或「3 开」", menu)
            return True

        return show_other_menu(sqq)

    # 过滤词管理子步骤
    if step == "filter_words":
        if msg == "返回":
            menu["step"] = "main"; return show_other_menu(sqq)
        if msg.startswith("添加 "):
            new_word = msg.split(" ", 1)[1].strip()
            words = get_config("filter_words", "")
            word_list = [w.strip() for w in words.split(",") if w.strip()]
            if new_word in word_list:
                menu_send(sqq, f"⚠️ '{new_word}' 已存在", menu)
            else:
                word_list.append(new_word)
                set_config("filter_words", ",".join(word_list))
                menu_send(sqq, f"✅ 已添加 '{new_word}'，当前: {', '.join(word_list)}", menu)
            return True
        if msg.startswith("删除 "):
            del_word = msg.split(" ", 1)[1].strip()
            words = get_config("filter_words", "")
            word_list = [w.strip() for w in words.split(",") if w.strip()]
            if del_word in word_list:
                word_list.remove(del_word)
                set_config("filter_words", ",".join(word_list))
                menu_send(sqq, f"✅ 已删除 '{del_word}'，当前: {', '.join(word_list) if word_list else '无'}", menu)
            else:
                menu_send(sqq, f"⚠️ 未找到 '{del_word}'", menu)
            return True
        menu_send(sqq, "格式：添加 xxx / 删除 xxx / 返回", menu); return True

    # 每日上限子步骤
    if step == "daily_limit":
        if msg == "返回":
            menu["step"] = "main"; return show_other_menu(sqq)
        if msg.isdigit():
            new_limit = int(msg)
            if new_limit < 1:
                menu_send(sqq, "上限不能小于1", menu); return True
            set_config("max_daily_count", str(new_limit))
            menu_send(sqq, f"✅ 每日上限已改为 {new_limit} 次", menu)
            menu["step"] = "main"; return show_other_menu(sqq)
        menu_send(sqq, "请输入数字，或 返回", menu); return True

    # 公共说明子步骤
    if step == "public_help":
        if msg == "返回":
            menu["step"] = "main"; return show_other_menu(sqq)
        set_config("public_help", msg)
        menu_send(sqq, "✅ 公共说明已更新", menu)
        menu["step"] = "main"; return show_other_menu(sqq)

    # 再来一张设置子步骤
    if step == "regen_settings":
        if msg == "返回":
            menu["step"] = "main"; return show_other_menu(sqq)
        if msg == "1":
            current = get_config("regen_private_enabled", "true") == "true"
            if current:
                set_config("regen_private_enabled", "false")
                menu_send(sqq, "✅ 私聊「再来一张」已关闭", menu)
            else:
                set_config("regen_private_enabled", "true")
                menu_send(sqq, "✅ 私聊「再来一张」已开启", menu)
            return show_regen_settings(sqq, menu)
        if msg == "2":
            current = get_config("regen_group_enabled", "true") == "true"
            if current:
                set_config("regen_group_enabled", "false")
                menu_send(sqq, "✅ 群聊「再来一张」已关闭", menu)
            else:
                set_config("regen_group_enabled", "true")
                menu_send(sqq, "✅ 群聊「再来一张」已开启", menu)
            return show_regen_settings(sqq, menu)
        menu_send(sqq, "请输入 1、2 或 返回", menu); return True

    return show_other_menu(sqq)

def show_other_menu(sqq):
    """显示其他设置菜单（含序号开关状态）"""
    filter_status = "开" if get_config("filter_enabled") == "true" else "关"
    group_status = "开" if get_config("group_whitelist_enabled", "true") == "true" else "关"
    random_status = "开" if get_config("random_enabled", "false") == "true" else "关"
    random_private_status = "开" if get_config("random_enabled_private", "true") == "true" else "关"
    random_group_status = "开" if get_config("random_enabled_group", "true") == "true" else "关"
    random_sfw_status = "开" if get_config("random_sfw", "true") == "true" else "关"
    random_nsfw_status = "开" if get_config("random_nsfw", "false") == "true" else "关"

    words = get_config("filter_words", "")
    word_count = len([w for w in words.split(",") if w.strip()])
    limit = get_config("max_daily_count", "20")
    menu = admin_menu.get(sqq)
    lines = ["📋 其他设置",
             f"当前: 过滤{filter_status} | 上限{limit}次",
             "",
             "开关控制（回复 序号+开/关，如 1 关）：",
             f"1. 提示词过滤          {filter_status}",
             f"2. 群聊白名单          {group_status}",
             f"3. 随便来点            {random_status}",
             f"4. 随便来点 私聊       {random_private_status}",
             f"5. 随便来点 群聊       {random_group_status}",
             f"6. 随便来点 SFW       {random_sfw_status}",
             f"7. 随便来点 NSFW      {random_nsfw_status}",
             "",
             "其他设置：",
             f"8. 过滤词管理 (共{word_count}个)",
             "9. 每日上限",
             "10. 公共说明",
             "11. 再来一张设置",
             "回复: 序号+开/关 / 序号 / 返回"]
    return menu_send(sqq, "\n".join(lines), menu)

def show_regen_settings(sqq, menu):
    """显示再来一张设置菜单"""
    private_status = "开启" if get_config("regen_private_enabled", "true") == "true" else "关闭"
    group_status = "开启" if get_config("regen_group_enabled", "true") == "true" else "关闭"
    lines = ["📋 再来一张设置",
             f"1. 私聊开关 [{private_status}]",
             f"2. 群聊开关 [{group_status}]",
             "回复序号切换状态，返回"]
    return menu_send(sqq, "\n".join(lines), menu)

def show_group_list(sqq):
    """显示群聊白名单列表"""
    db = get_db()
    groups = db.execute("SELECT group_id, remark FROM group_whitelist ORDER BY rowid").fetchall()
    db.close()
    menu = admin_menu.get(sqq)
    if not groups:
        return menu_send(sqq, "📋 暂无群聊\n回复：添加 群号 / 返回", menu)
    lines = ["📋 群聊列表:"]
    for i, g in enumerate(groups, 1):
        remark = f" ({g['remark']})" if g['remark'] else ""
        lines.append(f"{i}. {g['group_id']}{remark}")
    lines.append("回复：序号选择 / 添加 群号 / 移除 群号 / 返回")
    return menu_send(sqq, "\n".join(lines), menu)

def show_member_list(sqq, gid):
    """显示群成员列表（含今日使用次数）"""
    today = datetime.date.today().isoformat()
    max_count = int(get_config("max_daily_count", "20"))
    db = get_db()
    ginfo = db.execute("SELECT remark FROM group_whitelist WHERE group_id=?", (gid,)).fetchone()
    gremark = f" ({ginfo['remark']})" if ginfo and ginfo['remark'] else ""
    members = db.execute("SELECT qq, remark FROM group_member_whitelist WHERE group_id=?", (gid,)).fetchall()
    db.close()
    lines = [f"📋 群 {gid}{gremark} 成员:"]
    for i, m in enumerate(members, 1):
        db2 = get_db()
        row = db2.execute("SELECT count FROM daily_usage WHERE qq=? AND group_id=? AND date=?", (m["qq"], gid, today)).fetchone()
        db2.close()
        used = row["count"] if row else 0
        remark = f" ({m['remark']})" if m['remark'] else ""
        lines.append(f"{i}. {m['qq']}{remark} · {used}/{max_count}")
    if not members:
        lines.append("  (暂无成员)")
    lines.append("指令：拉黑 序号 / 移除 序号 / 重置 序号 / 全部重置 / 添加成员 QQ号 备注 / 黑名单 / 返回")
    return menu_send(sqq, "\n".join(lines), admin_menu.get(sqq))

def show_group_blacklist(sqq, gid):
    """显示群黑名单"""
    db = get_db()
    ginfo = db.execute("SELECT remark FROM group_whitelist WHERE group_id=?", (gid,)).fetchone()
    gremark = f" ({ginfo['remark']})" if ginfo and ginfo['remark'] else ""
    members = db.execute("SELECT qq, remark FROM group_blacklist WHERE group_id=?", (gid,)).fetchall()
    db.close()
    lines = [f"🚫 群 {gid}{gremark} 黑名单:"]
    if not members:
        lines.append("  (暂无)")
    else:
        for m in members:
            remark = f" ({m['remark']})" if m['remark'] else ""
            lines.append(f"  {m['qq']}{remark}")
    lines.append("回复 返回 回到成员列表")
    menu = admin_menu.get(sqq)
    menu["step"] = "manage_members"
    return menu_send(sqq, "\n".join(lines), menu)

def show_private_list(sqq):
    """显示私聊白名单"""
    db = get_db()
    members = db.execute("SELECT qq, remark FROM private_whitelist").fetchall()
    db.close()
    menu = admin_menu.get(sqq)
    if not members:
        return menu_send(sqq, "📋 私聊白名单为空\n回复：添加 QQ号 备注 / 返回", menu)
    lines = ["📋 私聊白名单:"]
    for i, m in enumerate(members, 1):
        remark = f" ({m['remark']})" if m['remark'] else ""
        lines.append(f"{i}. {m['qq']}{remark}")
    lines.append("指令：添加 QQ号 备注 / 移除 序号 / 返回")
    return menu_send(sqq, "\n".join(lines), menu)
# =====================================================================
# 管理员快捷指令
# =====================================================================
def handle_quick_commands(sqq, msg):
    """处理管理员快捷指令（无需进入菜单）"""
    if not is_admin(sqq):
        return False

    # 过滤开关快捷指令
    if msg == "过滤开":
        set_config("filter_enabled", "true")
        send_msg(sqq, None, "private", "✅ 提示词过滤已开启")
        return True
    if msg == "过滤关":
        set_config("filter_enabled", "false")
        send_msg(sqq, None, "private", "✅ 提示词过滤已关闭")
        return True

    # 重置整群次数
    if msg.startswith("重置群 "):
        gid = msg.split(" ", 1)[1].strip()
        if gid.isdigit():
            today = datetime.date.today().isoformat()
            db = get_db()
            db.execute("DELETE FROM daily_usage WHERE group_id=? AND date=?", (gid, today))
            db.commit(); db.close()
            send_msg(sqq, None, "private", f"✅ 已重置群 {gid} 全部成员次数")
            return True

    # 拉黑成员
    if msg.startswith("拉黑 "):
        parts = msg.split()
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            gid, target_qq = parts[1], parts[2]
            db = get_db()
            row = db.execute("SELECT remark FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, target_qq)).fetchone()
            remark = row["remark"] if row else ""
            db.execute("DELETE FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, target_qq))
            db.execute("INSERT OR REPLACE INTO group_blacklist VALUES (?,?,?)", (gid, target_qq, remark))
            db.commit(); db.close()
            send_msg(sqq, None, "private", f"✅ 已拉黑 {target_qq}")
            return True

    # 移除成员
    if msg.startswith("移除 "):
        parts = msg.split()
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            gid, target_qq = parts[1], parts[2]
            db = get_db()
            db.execute("DELETE FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, target_qq))
            db.commit(); db.close()
            send_msg(sqq, None, "private", f"✅ 已移除 {target_qq}")
            return True

    # 重置成员次数
    if msg.startswith("重置 "):
        parts = msg.split()
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            gid, target_qq = parts[1], parts[2]
            today = datetime.date.today().isoformat()
            db = get_db()
            db.execute("DELETE FROM daily_usage WHERE group_id=? AND qq=? AND date=?", (gid, target_qq, today))
            db.commit(); db.close()
            send_msg(sqq, None, "private", f"✅ 已重置 {target_qq} 次数")
            return True

    # 添加群白名单
    if msg.startswith("添加群 "):
        gid = msg.split(" ", 1)[1].strip()
        if gid.isdigit():
            db = get_db()
            db.execute("INSERT OR IGNORE INTO group_whitelist (group_id) VALUES (?)", (gid,))
            db.commit(); db.close()
            send_msg(sqq, None, "private", f"✅ 群 {gid} 已添加")
            return True

    # 添加私聊白名单
    if msg.startswith("添加私聊 "):
        parts = msg.split()
        qq = parts[1] if len(parts) > 1 else ""
        remark = " ".join(parts[2:]) if len(parts) > 2 else ""
        if qq.isdigit():
            db = get_db()
            db.execute("INSERT OR REPLACE INTO private_whitelist VALUES (?,?)", (qq, remark))
            db.commit(); db.close()
            send_msg(sqq, None, "private", f"✅ 已添加 {qq}")
            return True

    # 移除私聊白名单
    if msg.startswith("移除私聊 "):
        parts = msg.split()
        if len(parts) == 2 and parts[1].isdigit():
            qq = parts[1]
            db = get_db()
            db.execute("DELETE FROM private_whitelist WHERE qq=?", (qq,))
            db.commit(); db.close()
            send_msg(sqq, None, "private", f"✅ 已移除 {qq}")
            return True

    return False

# =====================================================================
# 核心消息处理（包含所有新功能）
# =====================================================================
def handle_message(event):
    """
    处理来自 NapCat WebSocket 的消息事件。
    这是整个机器人的核心调度函数。
    """
    global queue_counter

    # 只处理消息事件
    if event.get("post_type") not in ("message", "message_sent"):
        return

    # 提取消息基本信息
    msg_type = event.get("message_type")  # "private" 或 "group"
    user_id = str(event.get("user_id", ""))
    group_id = str(event.get("group_id", "")) if msg_type == "group" else None
    raw_message = event.get("raw_message", "").strip()
    sender_qq = str(event.get("sender", {}).get("user_id", user_id))
    self_id = str(event.get("self_id", ""))  # 机器人自己的 QQ 号

    # 忽略机器人自己的消息和空消息（且无图片）
    if sender_qq == self_id or (not raw_message and not has_image(event)):
        return

    # 解析消息：移除 @机器人 标记
    at_bot = f"[CQ:at,qq={self_id}]"
    is_at_bot = at_bot in raw_message
    msg = raw_message.replace(at_bot, "").strip()

    # ── 构建用户 key ──
    if msg_type == "private":
        user_key = f"private_{sender_qq}"
    else:
        user_key = f"group_{group_id}_{sender_qq}"

    # ── 检查确认状态机（多提示词/多图片确认）──
    with pending_confirm_lock:
        confirm_state = pending_confirm_states.get(user_key)
    if confirm_state:
        msg_lower = msg.lower().strip()
        if msg_lower in ("是", "继续", "y", "yes", "确认"):
            # 撤回所有询问消息
            for mid in confirm_state.get("sent_msg_ids", []):
                delete_msg(mid)
            with pending_confirm_lock:
                pending_confirm_states.pop(user_key, None)
            # 继续执行之前暂存的任务
            params = confirm_state["params"]
            if params.get("next_step") == "video_size":
                return _ask_video_size(user_key, msg_type, sender_qq, group_id, params, event)
            else:
                return _do_submit_task(sender_qq, group_id, msg_type, params, event)
        elif msg_lower in ("否", "取消", "n", "no", "算了"):
            for mid in confirm_state.get("sent_msg_ids", []):
                delete_msg(mid)
            with pending_confirm_lock:
                pending_confirm_states.pop(user_key, None)
            send_msg(sender_qq, group_id, msg_type, "任务已取消" if msg_type == "private" else f"[CQ:at,qq={sender_qq}] 任务已取消")
            return
        else:
            return  # 等待确认中，忽略其他消息

    # ── 检查视频尺寸询问状态 ──
    with video_size_lock:
        vs_state = video_size_states.get(user_key)
    if vs_state:
        if msg.strip().isdigit():
            choice = int(msg.strip())
            video_sizes = load_video_size_presets()
            if 1 <= choice <= len(video_sizes):
                chosen = video_sizes[choice - 1]
                # 撤回询问消息
                for mid in vs_state.get("sent_msg_ids", []):
                    delete_msg(mid)
                with video_size_lock:
                    video_size_states.pop(user_key, None)
                # 如果工作流有 duration 节点 → 继续询问时长
                params = vs_state["params"]
                nodes_config = _parse_nodes_config(params.get("wf_config", {}))
                has_duration = any(n["type"] == "duration" for n in nodes_config)
                if has_duration:
                    return _ask_video_duration(user_key, msg_type, sender_qq, group_id, params, event,
                                               width=chosen["width"], height=chosen["height"],
                                               size_name=chosen["name"])
                else:
                    return _do_submit_task(sender_qq, group_id, msg_type, params, event,
                                          width=chosen["width"], height=chosen["height"],
                                          size_name=chosen["name"])
            else:
                send_msg(sender_qq, group_id, msg_type, "编号无效，请重新输入")
                return
        elif msg.strip().lower() in ("取消", "否", "n"):
            for mid in vs_state.get("sent_msg_ids", []):
                delete_msg(mid)
            with video_size_lock:
                video_size_states.pop(user_key, None)
            send_msg(sender_qq, group_id, msg_type, "任务已取消" if msg_type == "private" else f"[CQ:at,qq={sender_qq}] 任务已取消")
            return
        else:
            send_msg(sender_qq, group_id, msg_type, "请回复编号选择分辨率（如 1），或回复「取消」")
            return

    # ── 检查视频时长询问状态 ──
    with video_duration_lock:
        vd_state = video_duration_states.get(user_key)
    if vd_state:
        if msg.strip().isdigit():
            duration_val = int(msg.strip())
            if duration_val <= 0:
                send_msg(sender_qq, group_id, msg_type, "请输入大于0的秒数")
                return
            # 撤回询问消息
            for mid in vd_state.get("sent_msg_ids", []):
                delete_msg(mid)
            with video_duration_lock:
                video_duration_states.pop(user_key, None)
            # 提交任务
            params = vd_state["params"]
            width = vd_state.get("width")
            height = vd_state.get("height")
            size_name = vd_state.get("size_name", "")
            return _do_submit_task(sender_qq, group_id, msg_type, params, event,
                                  width=width, height=height,
                                  size_name=size_name, video_duration=duration_val)
        elif msg.strip().lower() in ("取消", "否", "n"):
            for mid in vd_state.get("sent_msg_ids", []):
                delete_msg(mid)
            with video_duration_lock:
                video_duration_states.pop(user_key, None)
            send_msg(sender_qq, group_id, msg_type, "任务已取消" if msg_type == "private" else f"[CQ:at,qq={sender_qq}] 任务已取消")
            return
        else:
            send_msg(sender_qq, group_id, msg_type, "请输入视频时长（秒），如 5，或回复「取消」")
            return

    # ── 群聊撤回指令 ──
    if msg_type == "group" and msg.strip() == "撤回":
        if is_blacklisted(group_id, sender_qq):
            return
        db = get_db()
        in_whitelist = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (group_id, sender_qq)).fetchone()
        db.close()
        if not in_whitelist:
            return
        idx, task = get_user_recent_task(group_id, sender_qq)
        if idx is None:
            return
        sent_ids = task["sent_msg_ids"]
        success = sum(1 for mid in sent_ids if delete_msg(mid))
        with task_lock:
            if idx < len(group_recent_tasks.get(group_id, [])):
                del group_recent_tasks[group_id][idx]
        logger.info(f"撤回操作: 群{group_id} 用户{sender_qq} 成功{success}/{len(sent_ids)}")
        if success < len(sent_ids):
            tip_id = send_msg(None, group_id, "group", "❌ 已超过2分钟，无法撤回")
            if tip_id:
                schedule_delete(tip_id, 15)
        return

    # ⭐ ── 停止任务指令（群聊无需@机器人，私聊和群聊都支持）──
    stop_triggers_str = get_config("stop_triggers", "停止,终止,取消任务")
    stop_words = [w.strip() for w in stop_triggers_str.split(",") if w.strip()]
    is_stop_cmd = msg.strip() in stop_words
    if is_stop_cmd:
        if msg_type == "group":
            stop_group_enabled = get_config("stop_group_enabled", "true") == "true"
            if not stop_group_enabled:
                return
            if is_blacklisted(group_id, sender_qq):
                return
            db = get_db()
            in_whitelist = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (group_id, sender_qq)).fetchone()
            db.close()
            if not in_whitelist:
                return
        elif msg_type == "private":
            stop_private_enabled = get_config("stop_private_enabled", "true") == "true"
            if not stop_private_enabled:
                return
        found_prompt_id = None
        for pid, ctx in prompt_context.items():
            if ctx.get("sender_qq") == sender_qq and ctx.get("message_type") == msg_type:
                if msg_type == "group" and ctx.get("group_id") != group_id:
                    continue
                found_prompt_id = pid
                break
        if not found_prompt_id:
            tip = "⚠️ 没有正在进行的任务"
            mid = send_msg(sender_qq, group_id, msg_type, tip if msg_type == "private" else f"[CQ:at,qq={sender_qq}] {tip}")
            if mid:
                schedule_delete(mid, 15)
            return
        success = interrupt_comfyui()
        if success:
            if found_prompt_id in prompt_context:
                prompt_context[found_prompt_id]["interrupted"] = True
            ctx = prompt_context.get(found_prompt_id)
            if ctx and ctx.get("history_id"):
                update_history(ctx["history_id"], "已取消", time.time() - ctx.get("start_time", time.time()))
            prompt_context.pop(found_prompt_id, None)
            tip = "✅ 已停止当前任务"
        else:
            tip = "❌ 停止失败，请稍后重试"
        mid = send_msg(sender_qq, group_id, msg_type, tip if msg_type == "private" else f"[CQ:at,qq={sender_qq}] {tip}")
        if mid:
            schedule_delete(mid, 15)
        return

    # ── 群聊「随便来点」检测（需@机器人）──
    if msg_type == "group" and is_random_enabled() and is_random_enabled_for_group():
        random_trigger = get_random_trigger()
        if msg == random_trigger:
            if is_blacklisted(group_id, sender_qq):
                return
            db = get_db()
            in_whitelist = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (group_id, sender_qq)).fetchone()
            db.close()
            if not in_whitelist:
                return
            random_usage_mode = get_config("random_usage_mode", "global").strip()
            random_usage_limit = int(get_config("random_usage_limit", "0"))
            if random_usage_mode == "global":
                allowed, reason = check_group_permission(group_id, sender_qq)
                if not allowed:
                    send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason}")
                    return
            elif random_usage_mode == "limited":
                today = datetime.date.today().isoformat()
                db = get_db()
                used_row = db.execute("SELECT count FROM daily_usage WHERE qq=? AND group_id=? AND date=?", (sender_qq, group_id, today)).fetchone()
                used = used_row["count"] if used_row else 0
                db.close()
                if used >= random_usage_limit:
                    send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] ⚠️ 今天随便来点次数已用完 ({random_usage_limit}/天)")
                    return
            sfw_enabled = is_random_sfw_enabled()
            nsfw_enabled = is_random_nsfw_enabled()
            if not sfw_enabled and not nsfw_enabled:
                send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] ⚠️ 后台未勾选任何模式（SFW/NSFW）")
                return
            random_prompt = generate_random_prompt(sfw_enabled=sfw_enabled, nsfw_enabled=nsfw_enabled)
            if not random_prompt:
                send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] ⚠️ 随机提示词生成失败，请检查词库文件")
                return
            group_key = f"{group_id}_{sender_qq}"
            with last_group_prompt_lock:
                last = last_group_prompt.get(group_key)
            chosen_wf = None
            if last:
                chosen_wf = last["workflow_name"]
            else:
                db = get_db()
                row = db.execute(
                    "SELECT workflow FROM history WHERE qq=? AND group_id=? AND output_type='image' AND status='成功' ORDER BY id DESC LIMIT 1",
                    (sender_qq, group_id)
                ).fetchone()
                db.close()
                if row:
                    chosen_wf = row["workflow"]
            if not chosen_wf:
                db = get_db()
                triggers = db.execute("SELECT word, workflow FROM trigger_words").fetchall()
                db.close()
                for tw in triggers:
                    wf_data, wf_cfg = load_workflow_json(tw["workflow"])
                    if wf_data and str(wf_cfg.get("output_type", "image")).strip() == "image":
                        has_pos = bool(str(wf_cfg.get("positive_node_title", "")).strip())
                        if has_pos:
                            chosen_wf = tw["workflow"]
                            break
            if not chosen_wf:
                send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] ❌ 未找到可用的绘图工作流")
                return
            wf, wf_config = load_workflow_json(chosen_wf)
            if not wf:
                return
            submit_generate_task(
                sender_qq=sender_qq, group_id=group_id, msg_type="group",
                msg=random_prompt, matched_word="", matched_workflow=chosen_wf,
                workflow=wf, wf_config=wf_config, event=event,
                size_name="随机", width=None, height=None,
                has_positive=True, has_latent=True,
                total_in_batch=1, batch_index=0, is_random=True,
            )
            return

    # ── 群聊「再来一张」检测（无需@）──
    if msg_type == "group" and is_regen_enabled_for_group():
        regen_triggers_str = get_config("regen_triggers", "再来,继续,加图,再来一次")
        regen_words = [w.strip() for w in regen_triggers_str.split(",") if w.strip()]
        is_regen = False
        for rw in regen_words:
            if msg.startswith(rw):
                is_regen = True
                break
        if is_regen:
            if is_blacklisted(group_id, sender_qq):
                return
            db = get_db()
            in_whitelist = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (group_id, sender_qq)).fetchone()
            db.close()
            if not in_whitelist:
                return
            group_key = f"{group_id}_{sender_qq}"
            with last_group_prompt_lock:
                last = last_group_prompt.get(group_key)
            if last and last.get("output_type", "image") != "image":
                last = None
            if not last:
                send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] ⚠️ 没有找到上一次生图记录")
                return
            allowed_wf, reason_wf, use_global = check_workflow_limit(group_id, sender_qq, last["workflow_name"])
            if not allowed_wf:
                send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason_wf}")
                return
            if use_global:
                allowed, reason = check_group_permission(group_id, sender_qq)
                if not allowed:
                    send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason}")
                    return
            increment_usage(group_id, sender_qq, last["workflow_name"], use_global)
            wf, wf_config = load_workflow_json(last["workflow_name"])
            if not wf:
                return
            submit_generate_task(
                sender_qq=sender_qq, group_id=group_id, msg_type="group",
                msg=last["prompt_text"], matched_word="", matched_workflow=last["workflow_name"],
                workflow=wf, wf_config=wf_config, event=event,
                size_name="", width=None, height=None,
                has_positive=last.get("has_positive", True),
                has_latent=last.get("has_latent", False),
                total_in_batch=1, batch_index=0,
            )
            return

    # 群聊中未 @机器人 则忽略
    if msg_type == "group" and not is_at_bot:
        return

    # ── 管理员私聊指令 ──
    if msg_type == "private" and is_admin(sender_qq):
        if handle_quick_commands(sender_qq, msg):
            return
        if msg == "状态":
            uptime_seconds = int(time.time() - start_time_global)
            days, rem = divmod(uptime_seconds, 86400)
            hours, rem2 = divmod(rem, 3600)
            mins, _ = divmod(rem2, 60)
            uptime_str = f"{days}天{hours}小时{mins}分" if days > 0 else f"{hours}小时{mins}分"
            db = get_db()
            today = datetime.date.today().isoformat()
            today_imgs = db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='image' AND status='成功'", (today,)).fetchone()["c"]
            today_texts = db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='text' AND status='成功'", (today,)).fetchone()["c"]
            today_users = db.execute("SELECT COUNT(DISTINCT qq) as c FROM history WHERE date(time)=?", (today,)).fetchone()["c"]
            total_registered = db.execute("SELECT COUNT(*) as c FROM group_member_whitelist").fetchone()["c"]
            total_blacklisted = db.execute("SELECT COUNT(*) as c FROM group_blacklist").fetchone()["c"]
            total_groups = db.execute("SELECT COUNT(*) as c FROM group_whitelist").fetchone()["c"]
            db.close()
            max_count = int(get_config("max_daily_count", "20"))
            filter_status = "开启" if get_config("filter_enabled") == "true" else "关闭"
            regen_private = "开启" if get_config("regen_private_enabled", "true") == "true" else "关闭"
            regen_group = "开启" if get_config("regen_group_enabled", "true") == "true" else "关闭"
            random_status = "开启" if get_config("random_enabled", "false") == "true" else "关闭"
            status_text = (
                f"📊 ArtLink v1.2.0 状态\n"
                f"ComfyUI: {'在线 ✅' if comfyui_online else '离线 ❌'}\n"
                f"NapCat: {'在线 ✅' if napcat_online else '离线 ❌'}\n"
                f"排队任务: {queue_counter}\n"
                f"今日生图: {today_imgs}\n"
                f"今日反推: {today_texts}\n"
                f"活跃用户: {today_users}\n"
                f"每日上限: {max_count}次（0点刷新）\n"
                f"过滤状态: {filter_status}\n"
                f"再来一张(私聊): {regen_private}\n"
                f"再来一张(群聊): {regen_group}\n"
                f"随便来点: {random_status}\n"
                f"已注册用户: {total_registered}\n"
                f"黑名单: {total_blacklisted}\n"
                f"已授权群: {total_groups}\n"
                f"运行时长: {uptime_str}"
            )
            send_msg(sender_qq, None, "private", status_text)
            return
        if handle_admin_menu(sender_qq, msg):
            return

    # ── 权限检查 ──
    if msg_type == "private":
        if get_config("private_whitelist_enabled", "true") != "false":
            cache_key = f"private_{sender_qq}"
            if cache_key not in _access_cache:
                db = get_db()
                row = db.execute("SELECT 1 FROM private_whitelist WHERE qq=?", (sender_qq,)).fetchone()
                db.close()
                _access_cache[cache_key] = row is not None
            if not _access_cache[cache_key]:
                return

        # ── 私聊：随便来点（无需进入绘图模式）──
        if is_random_enabled() and is_random_enabled_for_private():
            random_trigger = get_random_trigger()
            if msg == random_trigger:
                sfw_enabled = is_random_sfw_enabled()
                nsfw_enabled = is_random_nsfw_enabled()
                if not sfw_enabled and not nsfw_enabled:
                    send_msg(sender_qq, None, "private", "⚠️ 后台未勾选任何模式（SFW/NSFW）")
                    return
                random_prompt = generate_random_prompt(sfw_enabled=sfw_enabled, nsfw_enabled=nsfw_enabled)
                if not random_prompt:
                    send_msg(sender_qq, None, "private", "⚠️ 随机提示词生成失败，请检查词库文件")
                    return
                last = last_private_prompt.get(sender_qq)
                chosen_wf = None
                if last:
                    chosen_wf = last["workflow_name"]
                else:
                    db = get_db()
                    row = db.execute(
                        "SELECT workflow FROM history WHERE qq=? AND output_type='image' AND status='成功' ORDER BY id DESC LIMIT 1",
                        (sender_qq,)
                    ).fetchone()
                    db.close()
                    if row:
                        chosen_wf = row["workflow"]
                if not chosen_wf:
                    db = get_db()
                    triggers = db.execute("SELECT word, workflow FROM trigger_words").fetchall()
                    db.close()
                    for tw in triggers:
                        wf_data, wf_cfg = load_workflow_json(tw["workflow"])
                        if wf_data and str(wf_cfg.get("output_type", "image")).strip() == "image":
                            has_pos = bool(str(wf_cfg.get("positive_node_title", "")).strip())
                            if has_pos:
                                chosen_wf = tw["workflow"]
                                break
                    if not chosen_wf:
                        send_msg(sender_qq, None, "private", "❌ 未找到可用的绘图工作流")
                        return
                wf, wf_config = load_workflow_json(chosen_wf)
                if not wf:
                    return
                submit_generate_task(
                    sender_qq=sender_qq, group_id=None, msg_type="private",
                    msg=random_prompt, matched_word="", matched_workflow=chosen_wf,
                    workflow=wf, wf_config=wf_config, event=event,
                    size_name="随机", width=None, height=None,
                    has_positive=True, has_latent=True,
                    total_in_batch=1, batch_index=0, is_random=True,
                )
                return

        # ⭐ ── 私聊：绘图模式检测（优先使用最近一次绘图的工作流）──
        drawing_mode_trigger = get_config("drawing_mode_trigger", "绘图模式")
        duration_minutes = int(get_config("drawing_mode_duration", "5"))
        if msg == "退出":
            with drawing_mode_lock:
                if sender_qq in drawing_mode_states:
                    del drawing_mode_states[sender_qq]
                    send_msg(sender_qq, None, "private", "✅ 已退出绘图模式")
                    return
        if msg == drawing_mode_trigger:
            # ⭐ 选用最近一次的绘图工作流
            db = get_db()
            chosen_wf = None
            # ① 优先从缓存取
            last = last_private_prompt.get(sender_qq)
            if last:
                chosen_wf = last["workflow_name"]
            # ② 从历史记录取
            if not chosen_wf:
                row = db.execute(
                    "SELECT workflow FROM history WHERE qq=? AND output_type='image' AND status='成功' ORDER BY id DESC LIMIT 1",
                    (sender_qq,)
                ).fetchone()
                if row:
                    chosen_wf = row["workflow"]
            # ③ 遍历触发词取第一个图片工作流
            if not chosen_wf:
                triggers = db.execute("SELECT word, workflow FROM trigger_words").fetchall()
                for tw in triggers:
                    wf_data, wf_cfg = load_workflow_json(tw["workflow"])
                    if wf_data and str(wf_cfg.get("output_type", "image")).strip() == "image":
                        has_pos = bool(str(wf_cfg.get("positive_node_title", "")).strip())
                        if has_pos:
                            chosen_wf = tw["workflow"]
                            break
            db.close()
            if not chosen_wf:
                send_msg(sender_qq, None, "private", "❌ 未找到可用的绘图工作流")
                return
            with drawing_mode_lock:
                drawing_mode_states[sender_qq] = {
                    "workflow_name": chosen_wf,
                    "last_active": time.time()
                }
            send_msg(sender_qq, None, "private", f"🎨 已进入绘图模式（{duration_minutes}分钟超时）\n直接发送提示词即可生图\n发送「退出」手动退出")
            return
        with drawing_mode_lock:
            dm_state = drawing_mode_states.get(sender_qq)
        if dm_state:
            if time.time() - dm_state["last_active"] > duration_minutes * 60:
                with drawing_mode_lock:
                    drawing_mode_states.pop(sender_qq, None)
            else:
                if not msg:
                    return
                size_presets = load_size_presets()
                is_size_cmd = False
                for sp in size_presets:
                    trigger_word = sp["trigger_word"]
                    if trigger_word and msg == trigger_word:
                        user_key2 = f"private_{sender_qq}"
                        save_user_size(user_key2, sp["width"], sp["height"])
                        send_msg(sender_qq, None, "private", f"已切换为{sp['name']}({sp['width']}x{sp['height']})")
                        with drawing_mode_lock:
                            if sender_qq in drawing_mode_states:
                                drawing_mode_states[sender_qq]["last_active"] = time.time()
                        is_size_cmd = True
                        break
                if is_size_cmd:
                    return
                with drawing_mode_lock:
                    if sender_qq in drawing_mode_states:
                        drawing_mode_states[sender_qq]["last_active"] = time.time()
                wf_name = dm_state["workflow_name"]
                return handle_generate_task(sender_qq, None, "private", msg, wf_name, event)

    else:
        # 群聊：检查群白名单（缓存+总开关+小开关）
        if get_config("group_whitelist_enabled", "true") != "false":
            cache_key = f"group_{group_id}"
            if cache_key not in _access_cache:
                db = get_db()
                row = db.execute("SELECT enabled FROM group_whitelist WHERE group_id=?", (group_id,)).fetchone()
                db.close()
                _access_cache[cache_key] = (row is not None and row["enabled"] != 0)
            if not _access_cache[cache_key]:
                return

        # 黑名单静默忽略
        if is_blacklisted(group_id, sender_qq):
            return

        # 未注册用户处理
        db = get_db()
        in_whitelist = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (group_id, sender_qq)).fetchone()
        db.close()
        if not in_whitelist:
            if msg == "注册":
                return handle_register(sender_qq, group_id, msg_type, at_bot)
            tip = "⚠️ 请先注册后再使用\n发送 @机器人 注册"
            mid = send_msg(None, group_id, "group", tip)
            if mid:
                schedule_delete(mid, 15)
            return

    # ── 帮助触发词 ──
    help_triggers_str = get_config("help_triggers", "说明,帮助,help,？,?,帮助文档,使用帮助,指令,菜单,功能,教程,怎么用,使用说明")
    help_words = [w.strip() for w in help_triggers_str.split(",") if w.strip()]
    if msg in help_words:
        send_help(sender_qq, msg_type, group_id)
        return

    # ── 已注册用户发"注册" ──
    if msg_type == "group" and msg == "注册":
        return handle_register(sender_qq, group_id, msg_type, at_bot)

    # ── 空消息（无图片）静默 ──
    if msg_type == "group" and not msg and not has_image(event):
        return

    # ── 私聊「再来一张」检测 ──
    if msg_type == "private":
        if is_regen_enabled_for_private():
            regen_triggers_str = get_config("regen_triggers", "再来,继续,加图,再来一次")
            regen_words = [w.strip() for w in regen_triggers_str.split(",") if w.strip()]
            is_regen = False
            for rw in regen_words:
                if msg.startswith(rw):
                    is_regen = True
                    break
            if is_regen:
                last = last_private_prompt.get(sender_qq)
                if last and last.get("output_type", "image") != "image":
                    last = None
                if not last:
                    send_msg(sender_qq, None, "private", "⚠️ 没有找到上一次生图记录")
                    return
                count = parse_regen_count(msg)
                for i in range(count):
                    wf_name = last["workflow_name"]
                    prompt_text = last["prompt_text"]
                    wf, wf_config = load_workflow_json(wf_name)
                    if not wf:
                        send_msg(sender_qq, None, "private", f"❌ 工作流 {wf_name} 加载失败")
                        return
                    submit_generate_task(
                        sender_qq=sender_qq, group_id=None, msg_type="private",
                        msg=prompt_text, matched_word="", matched_workflow=wf_name,
                        workflow=wf, wf_config=wf_config, event=event,
                        size_name="", width=None, height=None,
                        has_positive=last.get("has_positive", True),
                        has_latent=last.get("has_latent", False),
                        total_in_batch=count, batch_index=i,
                    )
                    if i < count - 1:
                        time.sleep(1.5)
                if count > 1:
                    send_msg(sender_qq, None, "private", f"✅ 已提交 {count} 张")
                return

    # ── 尺寸触发词 ──
    size_presets = load_size_presets()
    for sp in size_presets:
        trigger_word = sp["trigger_word"]
        if trigger_word and msg.startswith(trigger_word):
            user_key2 = f"group_{group_id}_{sender_qq}" if msg_type == "group" else f"private_{sender_qq}"
            save_user_size(user_key2, sp["width"], sp["height"])
            if msg == trigger_word:
                tip = f"已切换为{sp['name']}({sp['width']}x{sp['height']})"
                mid = send_msg(sender_qq, group_id, msg_type, tip if msg_type == "private" else f"[CQ:at,qq={sender_qq}] {tip}")
                if mid and msg_type == "group":
                    schedule_delete(mid, 15)
                return
            msg = msg[len(trigger_word):].strip()
            break

    # ── 生图/反推触发词匹配 ──
    db = get_db()
    triggers = db.execute("SELECT word, workflow FROM trigger_words").fetchall()
    db.close()
    trigger_list = [{"word": r["word"], "workflow": r["workflow"]} for r in triggers]
    trigger_list.sort(key=lambda x: len(x["word"]), reverse=True)
    matched = None
    for row in trigger_list:
        if msg == row["word"] or msg.startswith(row["word"]):
            matched = (row["word"], row["workflow"])
            break

    if not matched:
        # ⭐ 群聊：无触发词匹配 → 检查工作流记忆缓存 ──
        if msg_type == "group" and msg:
            group_wf_key = f"{group_id}_{sender_qq}"
            cached_wf = group_last_workflow.get(group_wf_key)
            if cached_wf and msg:
                # 有缓存 → 用缓存的工作流 + 当前消息当提示词
                workflow, wf_config = load_workflow_json(cached_wf)
                if workflow is None:
                    return
                output_type = str(wf_config.get("output_type", "image")).strip()
                # 只做图片任务
                if output_type != "image":
                    return
                prompt_text = msg
                nodes_config = _parse_nodes_config(wf_config)
                positive_count = len([n for n in nodes_config if n["type"] == "positive"])
                image_count = len([n for n in nodes_config if n["type"] == "image_input"])
                if positive_count > 0 and prompt_text:
                    prompt_segments = [s.strip() for s in prompt_text.split("\n") if s.strip()]
                    if not prompt_segments:
                        prompt_segments = []
                else:
                    prompt_segments = [prompt_text] if prompt_text else []
                img_filenames = []
                if image_count > 0 and has_image(event):
                    ack_id = send_msg(sender_qq, group_id, msg_type, "📥 正在接收图片，请稍候...")
                    img_filenames = download_qq_images(event)
                    if ack_id: delete_msg(ack_id)
                if positive_count > 1 and len(prompt_segments) < positive_count:
                    sent_ids = []
                    confirm_text = (
                        f"⚠️ 提示词数量提示\n\n"
                        f"该工作流配置了 {positive_count} 个提示词输入节点，"
                        f"您当前只输入了 {len(prompt_segments)} 段提示词。\n\n"
                        f"💡 多段提示词请用回车换行分隔\n\n"
                        f"• 回复「是」→ 立即生成（未匹配的节点将保持默认值）\n"
                        f"• 回复「否」→ 取消任务\n\n"
                        f"⏳ 60秒超时自动取消"
                    )
                    mid = send_msg(sender_qq, group_id, msg_type, confirm_text)
                    if mid: sent_ids.append(mid)
                    params = {
                        "matched_word": "", "matched_workflow": cached_wf,
                        "prompt_segments": prompt_segments, "img_filenames": img_filenames,
                        "output_type": output_type, "wf_config": wf_config, "workflow": workflow,
                        "positive_count": positive_count, "image_count": image_count,
                        "has_latent": True,
                    }
                    with pending_confirm_lock:
                        pending_confirm_states[user_key] = {
                            "params": params, "sent_msg_ids": sent_ids, "expires_at": time.time() + 60
                        }
                    return
                if image_count > 0 and len(img_filenames) < image_count:
                    sent_ids = []
                    confirm_text = (
                        f"⚠️ 图片数量提示\n\n"
                        f"该工作流配置了 {image_count} 个图片输入节点，"
                        f"您当前只发送了 {len(img_filenames)} 张图片。\n\n"
                        f"• 回复「是」→ 继续生成（未匹配节点保持默认值）\n"
                        f"• 回复「否」→ 取消任务\n\n"
                        f"⏳ 60秒超时自动取消"
                    )
                    mid = send_msg(sender_qq, group_id, msg_type, confirm_text)
                    if mid: sent_ids.append(mid)
                    params = {
                        "matched_word": "", "matched_workflow": cached_wf,
                        "prompt_segments": prompt_segments, "img_filenames": img_filenames,
                        "output_type": output_type, "wf_config": wf_config, "workflow": workflow,
                        "positive_count": positive_count, "image_count": image_count,
                        "has_latent": True,
                    }
                    with pending_confirm_lock:
                        pending_confirm_states[user_key] = {
                            "params": params, "sent_msg_ids": sent_ids, "expires_at": time.time() + 60
                        }
                    return
                return _do_submit_task(sender_qq, group_id, msg_type, {
                    "matched_word": "", "matched_workflow": cached_wf,
                    "prompt_segments": prompt_segments, "img_filenames": img_filenames,
                    "output_type": output_type, "wf_config": wf_config, "workflow": workflow,
                    "positive_count": positive_count, "image_count": image_count,
                    "has_latent": True,
                }, event)
            else:
                # 无缓存 → 提示
                tip = "⚠️ 暂无使用记录，请先完整触发一次 绘图任务"
                mid = send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {tip}")
                if mid:
                    schedule_delete(mid, 15)
                return
        return

    # ⭐ 触发词匹配成功 → 记录工作流记忆（仅群聊图片任务）──
    if msg_type == "group":
        workflow, wf_config = load_workflow_json(matched[1])
        if workflow and str(wf_config.get("output_type", "image")).strip() == "image":
            group_wf_key = f"{group_id}_{sender_qq}"
            group_last_workflow[group_wf_key] = matched[1]

    prompt_text = msg[len(matched[0]):].strip()
    workflow, wf_config = load_workflow_json(matched[1])
    if workflow is None:
        return

    output_type = str(wf_config.get("output_type", "image")).strip()
    nodes_config = _parse_nodes_config(wf_config)
    positive_count = len([n for n in nodes_config if n["type"] == "positive"])
    image_count = len([n for n in nodes_config if n["type"] == "image_input"])

    # ── 分割多段提示词 ──
    if positive_count > 0 and prompt_text:
        prompt_segments = [s.strip() for s in prompt_text.split("\n") if s.strip()]
        if not prompt_segments:
            prompt_segments = []
    else:
        prompt_segments = [prompt_text] if prompt_text else []

    # ── 多图片下载 ──
    img_filenames = []
    if image_count > 0 and has_image(event):
        ack_id = send_msg(sender_qq, group_id, msg_type, "📥 正在接收图片，请稍候...")
        img_filenames = download_qq_images(event)
        if ack_id: delete_msg(ack_id)

    # ── 检查提示词段数是否匹配 ──
    if positive_count > 1 and len(prompt_segments) < positive_count:
        sent_ids = []
        confirm_text = (
            f"⚠️ 提示词数量提示\n\n"
            f"该工作流配置了 {positive_count} 个提示词输入节点，"
            f"您当前只输入了 {len(prompt_segments)} 段提示词。\n\n"
            f"💡 多段提示词请用回车换行分隔\n\n"
            f"• 回复「是」→ 立即生成（未匹配的节点将保持默认值）\n"
            f"• 回复「否」→ 取消任务\n\n"
            f"⏳ 60秒超时自动取消"
        )
        mid = send_msg(sender_qq, group_id, msg_type, confirm_text)
        if mid: sent_ids.append(mid)
        next_step = "video_size" if output_type == "video" else None
        params = {
            "matched_word": matched[0], "matched_workflow": matched[1],
            "prompt_segments": prompt_segments, "img_filenames": img_filenames,
            "output_type": output_type, "wf_config": wf_config, "workflow": workflow,
            "positive_count": positive_count, "image_count": image_count,
            "has_latent": True, "next_step": next_step,
        }
        with pending_confirm_lock:
            pending_confirm_states[user_key] = {
                "params": params, "sent_msg_ids": sent_ids, "expires_at": time.time() + 60
            }
        return

    # ── 检查图片数量是否匹配 ──
    if image_count > 0 and len(img_filenames) < image_count:
        sent_ids = []
        confirm_text = (
            f"⚠️ 图片数量提示\n\n"
            f"该工作流配置了 {image_count} 个图片输入节点，"
            f"您当前只发送了 {len(img_filenames)} 张图片。\n\n"
            f"• 回复「是」→ 继续生成（未匹配节点保持默认值）\n"
            f"• 回复「否」→ 取消任务\n\n"
            f"⏳ 60秒超时自动取消"
        )
        mid = send_msg(sender_qq, group_id, msg_type, confirm_text)
        if mid: sent_ids.append(mid)
        next_step = "video_size" if output_type == "video" else None
        params = {
            "matched_word": matched[0], "matched_workflow": matched[1],
            "prompt_segments": prompt_segments, "img_filenames": img_filenames,
            "output_type": output_type, "wf_config": wf_config, "workflow": workflow,
            "positive_count": positive_count, "image_count": image_count,
            "has_latent": True, "next_step": next_step,
        }
        with pending_confirm_lock:
            pending_confirm_states[user_key] = {
                "params": params, "sent_msg_ids": sent_ids, "expires_at": time.time() + 60
            }
        return

    # ── 视频工作流：询问尺寸 ──
    if output_type == "video":
        return _ask_video_size(user_key, msg_type, sender_qq, group_id, {
            "matched_word": matched[0], "matched_workflow": matched[1],
            "prompt_segments": prompt_segments, "img_filenames": img_filenames,
            "output_type": output_type, "wf_config": wf_config, "workflow": workflow,
            "positive_count": positive_count, "image_count": image_count,
            "has_latent": True,
        }, event)

    # ── 正常提交 ──
    return _do_submit_task(sender_qq, group_id, msg_type, {
        "matched_word": matched[0], "matched_workflow": matched[1],
        "prompt_segments": prompt_segments, "img_filenames": img_filenames,
        "output_type": output_type, "wf_config": wf_config, "workflow": workflow,
        "positive_count": positive_count, "image_count": image_count,
        "has_latent": True,
    }, event)

# =====================================================================
# 任务辅助函数：视频询问、任务提交、绘图模式生图
# =====================================================================
def _ask_video_size(user_key, msg_type, sender_qq, group_id, params, event):
    """询问视频分辨率"""
    video_sizes = load_video_size_presets()
    if not video_sizes:
        send_msg(sender_qq, group_id, msg_type, "⚠️ 未配置视频尺寸，请在后台添加")
        return
    lines = ["🎬 请选择视频分辨率："]
    for i, vs in enumerate(video_sizes, 1):
        lines.append(f"{i}. {vs['name']} {vs['width']}x{vs['height']}")
    lines.append("")
    lines.append("💡 请回复标号（如 1、2、3）| 回复「取消」放弃任务 | 60秒超时自动取消")
    text = "\n".join(lines)
    mid = send_msg(sender_qq, group_id, msg_type, text)
    with video_size_lock:
        video_size_states[user_key] = {
            "params": params, "sent_msg_ids": [mid] if mid else [], "expires_at": time.time() + 60
        }


def _ask_video_duration(user_key, msg_type, sender_qq, group_id, params, event, width=None, height=None, size_name=""):
    """询问视频时长（秒）—— 示例改为5，超时保持60秒"""
    lines = ["⏱️ 请输入视频时长（秒）："]
    lines.append("")
    lines.append("💡 直接回复数字（如 5）| 回复「取消」放弃任务 | 60秒超时自动取消")
    text = "\n".join(lines)
    mid = send_msg(sender_qq, group_id, msg_type, text)
    with video_duration_lock:
        video_duration_states[user_key] = {
            "params": params, "sent_msg_ids": [mid] if mid else [],
            "expires_at": time.time() + 60,
            "width": width, "height": height, "size_name": size_name
        }


def _do_submit_task(sender_qq, group_id, msg_type, params, event, width=None, height=None, size_name="", video_duration=None):
    """执行实际的任务提交"""
    global queue_counter
    matched_word = params["matched_word"]
    matched_workflow = params["matched_workflow"]
    prompt_segments = params.get("prompt_segments", [])
    img_filenames = params.get("img_filenames", [])
    output_type = params.get("output_type", "image")
    workflow = params["workflow"]
    wf_config = params["wf_config"]
    positive_count = params.get("positive_count", 0)
    image_count = params.get("image_count", 0)
    has_latent = params.get("has_latent", True)

    prompt_text = params.get("prompt_text_joined", "\n".join(prompt_segments))

    # ── 提示词过滤 ──
    if get_config("filter_enabled") == "true":
        filter_words = get_config("filter_words", "")
        if filter_words:
            words_to_remove = [w.strip() for w in filter_words.split(",") if w.strip()]
            for word in words_to_remove:
                prompt_text = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r',\s*,', ',', prompt_text).strip(', ')
            # ⭐ 同时过滤每个提示词段（传给 ComfyUI 的是 segments，不是 prompt_text）
            for i in range(len(prompt_segments)):
                for word in words_to_remove:
                    prompt_segments[i] = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_segments[i], flags=re.IGNORECASE)
                prompt_segments[i] = re.sub(r',\s*,', ',', prompt_segments[i]).strip(', ')

    # ── 群聊次数检查 ──
    if msg_type == "group":
        allowed_wf, reason_wf, use_global = check_workflow_limit(group_id, sender_qq, matched_workflow)
        if not allowed_wf:
            send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason_wf}")
            return
        if use_global:
            allowed, reason = check_group_permission(group_id, sender_qq)
            if not allowed:
                send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason}")
                return
        increment_usage(group_id, sender_qq, matched_workflow, use_global)

    if not check_comfyui_online():
        send_msg(sender_qq, group_id, msg_type, "⚠️ ComfyUI 离线")
        return

    # ── 获取尺寸 ──
    if not width and has_latent:
        user_key2 = f"group_{group_id}_{sender_qq}" if msg_type == "group" else f"private_{sender_qq}"
        width, height = get_user_size(user_key2)

    size_presets = load_size_presets()
    if not size_name and has_latent and width and height:
        for sp in size_presets:
            if sp["width"] == width and sp["height"] == height:
                size_name = sp["name"]
                break
        if not size_name:
            size_name = f"{width}x{height}"

    with queue_lock:
        queue_counter += 1
        position = queue_counter

    # ⭐ 单提示词节点时，把多段合并为一个
    if positive_count == 1 and len(prompt_segments) > 1:
        prompt_segments = [", ".join(prompt_segments)]

    start_time = time.time()
    workflow = modify_workflow(workflow, wf_config,
                               positive_prompts=prompt_segments if positive_count > 0 else None,
                               size_width=width, size_height=height,
                               input_image_paths=img_filenames if image_count > 0 else None,
                               video_duration=video_duration)
    prompt_id = submit_to_comfyui(workflow)
    if not prompt_id:
        with queue_lock:
            queue_counter -= 1
        send_msg(sender_qq, group_id, msg_type, "❌ 提交失败")
        return

    prompt_context[prompt_id] = {
        "message_type": msg_type, "group_id": group_id, "sender_qq": sender_qq,
        "prompt_text": prompt_text, "size_name": size_name,
        "output_type": output_type, "workflow_name": matched_workflow,
        "retry": 0, "start_time": start_time
    }

    tag_map = {"image": "🎨", "text": "📝", "video": "🎬"}
    tag = tag_map.get(output_type, "🎨")
    if msg_type == "group":
        # ── 按工作流使用模式显示不同的次数提醒 ──
        db = get_db()
        today = datetime.date.today().isoformat()
        wf_row = db.execute("SELECT usage_mode, usage_limit FROM workflows WHERE name=?", (matched_workflow,)).fetchone()
        wf_mode = str(wf_row["usage_mode"] if wf_row else "global").strip()
        wf_limit = int(wf_row["usage_limit"] if wf_row else 0)
        if wf_mode == "unlimited":
            reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务"
        elif wf_mode == "limited":
            used_row = db.execute("SELECT count FROM workflow_daily_usage WHERE qq=? AND group_id=? AND workflow=? AND date=?", (sender_qq, group_id, matched_workflow, today)).fetchone()
            wf_used = used_row["count"] if used_row else 0
            wf_remaining = wf_limit - wf_used
            reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务 · {matched_workflow}剩余{wf_remaining}/{wf_limit}次（每日0点刷新）"
        else:
            used_row = db.execute("SELECT count FROM daily_usage WHERE qq=? AND group_id=? AND date=?", (sender_qq, group_id, today)).fetchone()
            used = used_row["count"] if used_row else 0
            max_count = int(get_config("max_daily_count", "20"))
            # ── 支持 -1 无限制 ──
            if max_count == -1:
                reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务"
            else:
                remaining = max_count - used
                reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务 · 剩余{remaining}次（每日0点刷新）"
        db.close()
    else:
        reply = f"{tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务"
    msg_id = send_msg(sender_qq, group_id, msg_type, reply)
    if msg_id:
        schedule_delete(msg_id, 15)

    history_id = add_history(sender_qq, group_id, matched_workflow, output_type, prompt_text, size_name, "进行中", msg_type=msg_type)
    prompt_context[prompt_id]["history_id"] = history_id
    task_queue.put(prompt_id)

    log_task_submit(sender_qq, group_id, matched_workflow, output_type, "私聊" if msg_type == "private" else "群聊")

    # 仅图片任务记录「再来一张」上下文
    if output_type == "image":
        if msg_type == "private":
            last_private_prompt[sender_qq] = {
                "prompt_text": prompt_text, "workflow_name": matched_workflow,
                "size_name": size_name, "width": width, "height": height,
                "has_positive": positive_count > 0, "has_latent": has_latent,
                "output_type": output_type,
            }
        elif msg_type == "group":
            group_key = f"{group_id}_{sender_qq}"
            with last_group_prompt_lock:
                last_group_prompt[group_key] = {
                    "prompt_text": prompt_text, "workflow_name": matched_workflow,
                    "size_name": size_name, "width": width, "height": height,
                    "has_positive": positive_count > 0, "has_latent": has_latent,
                    "output_type": output_type,
                }


def handle_generate_task(sender_qq, group_id, msg_type, msg, matched_workflow, event):
    """处理绘图模式中的生图请求"""
    global queue_counter

    workflow, wf_config = load_workflow_json(matched_workflow)
    if workflow is None:
        send_msg(sender_qq, group_id, msg_type, "❌ 工作流加载失败")
        return

    output_type = str(wf_config.get("output_type", "image")).strip()
    nodes_config = _parse_nodes_config(wf_config)
    positive_count = len([n for n in nodes_config if n["type"] == "positive"])

    # 分割多段提示词
    if positive_count > 0 and msg:
        prompt_segments = [s.strip() for s in msg.split("\n") if s.strip()]
        if not prompt_segments:
            prompt_segments = []
    else:
        prompt_segments = [msg] if msg else []

    prompt_text = "\n".join(prompt_segments)

    # ── 提示词过滤 ──
    if get_config("filter_enabled") == "true":
        filter_words = get_config("filter_words", "")
        if filter_words:
            words_to_remove = [w.strip() for w in filter_words.split(",") if w.strip()]
            for word in words_to_remove:
                prompt_text = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r',\s*,', ',', prompt_text).strip(', ')
            # ⭐ 同时过滤每个提示词段（传给 ComfyUI 的是 segments，不是 prompt_text）
            for i in range(len(prompt_segments)):
                for word in words_to_remove:
                    prompt_segments[i] = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_segments[i], flags=re.IGNORECASE)
                prompt_segments[i] = re.sub(r',\s*,', ',', prompt_segments[i]).strip(', ')

    if not prompt_text and positive_count > 0:
        send_msg(sender_qq, group_id, msg_type, "需要提示词")
        return

    if not check_comfyui_online():
        send_msg(sender_qq, group_id, msg_type, "⚠️ ComfyUI 离线")
        return

    has_latent = len([n for n in nodes_config if n["type"] == "latent"]) > 0
    fwidth, fheight = None, None
    if has_latent:
        user_key2 = f"group_{group_id}_{sender_qq}" if msg_type == "group" else f"private_{sender_qq}"
        fwidth, fheight = get_user_size(user_key2)

    size_presets = load_size_presets()
    size_name = ""
    if has_latent and fwidth and fheight:
        for sp in size_presets:
            if sp["width"] == fwidth and sp["height"] == fheight:
                size_name = sp["name"]
                break
        if not size_name:
            size_name = f"{fwidth}x{fheight}"

    with queue_lock:
        queue_counter += 1
        position = queue_counter

    # ⭐ 单提示词节点时，把多段合并为一个
    if positive_count == 1 and len(prompt_segments) > 1:
        prompt_segments = [", ".join(prompt_segments)]

    start_time = time.time()
    workflow = modify_workflow(workflow, wf_config,
                               positive_prompts=prompt_segments if positive_count > 0 else None,
                               size_width=fwidth, size_height=fheight)
    prompt_id = submit_to_comfyui(workflow)
    if not prompt_id:
        with queue_lock:
            queue_counter -= 1
        send_msg(sender_qq, group_id, msg_type, "❌ 提交失败")
        return

    prompt_context[prompt_id] = {
        "message_type": msg_type, "group_id": group_id, "sender_qq": sender_qq,
        "prompt_text": prompt_text, "size_name": size_name,
        "output_type": output_type, "workflow_name": matched_workflow,
        "retry": 0, "start_time": start_time
    }

    reply = f"🎨 已提交 [{matched_workflow}] 前边还有{position-1}个任务"
    msg_id = send_msg(sender_qq, group_id, msg_type, reply)
    if msg_id:
        schedule_delete(msg_id, 15)

    history_id = add_history(sender_qq, group_id, matched_workflow, output_type, prompt_text, size_name, "进行中", msg_type=msg_type)
    prompt_context[prompt_id]["history_id"] = history_id
    task_queue.put(prompt_id)

    log_task_submit(sender_qq, group_id, matched_workflow, output_type, "私聊" if msg_type == "private" else "群聊")

    # 仅图片任务记录「再来一张」上下文
    if output_type == "image":
        if msg_type == "private":
            last_private_prompt[sender_qq] = {
                "prompt_text": prompt_text, "workflow_name": matched_workflow,
                "size_name": size_name, "width": fwidth, "height": fheight,
                "has_positive": positive_count > 0, "has_latent": has_latent,
                "output_type": output_type,
            }
        elif msg_type == "group":
            group_key = f"{group_id}_{sender_qq}"
            with last_group_prompt_lock:
                last_group_prompt[group_key] = {
                    "prompt_text": prompt_text, "workflow_name": matched_workflow,
                    "size_name": size_name, "width": fwidth, "height": fheight,
                    "has_positive": positive_count > 0, "has_latent": has_latent,
                    "output_type": output_type,
                }
# =====================================================================
# 提交任务函数（供绘图模式和再来一张复用）
# =====================================================================
def submit_generate_task(sender_qq, group_id, msg_type, msg, matched_word, matched_workflow, workflow, wf_config, event, size_name="", width=None, height=None, has_positive=True, has_latent=False, total_in_batch=1, batch_index=0, is_random=False):
    """提交生成任务（用于绘图模式和再来一张的复用）"""
    global queue_counter

    output_type = str(wf_config.get("output_type", "image")).strip()
    has_image_node = _parse_nodes_config(wf_config)
    image_count = len([n for n in has_image_node if n["type"] == "image_input"])

    # 提示词过滤
    prompt_text = msg
    if get_config("filter_enabled") == "true":
        filter_words = get_config("filter_words", "")
        if filter_words:
            words_to_remove = [w.strip() for w in filter_words.split(",") if w.strip()]
            for word in words_to_remove:
                prompt_text = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r',\s*,', ',', prompt_text).strip(', ')

    if not prompt_text and has_positive:
        send_msg(sender_qq, group_id, msg_type, "需要提示词")
        return None

    if not check_comfyui_online():
        send_msg(sender_qq, group_id, msg_type, "⚠️ ComfyUI 离线")
        return None

    # 群聊次数检查
    if msg_type == "group":
        if is_random:
            random_usage_mode = get_config("random_usage_mode", "global").strip()
            if random_usage_mode == "unlimited":
                pass
            elif random_usage_mode == "limited":
                increment_usage(group_id, sender_qq, matched_workflow, True)
            else:
                allowed_wf, reason_wf, use_global = check_workflow_limit(group_id, sender_qq, matched_workflow)
                if not allowed_wf:
                    send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason_wf}")
                    return None
                if use_global:
                    allowed, reason = check_group_permission(group_id, sender_qq)
                    if not allowed:
                        send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason}")
                        return None
                increment_usage(group_id, sender_qq, matched_workflow, use_global)
        else:
            allowed_wf, reason_wf, use_global = check_workflow_limit(group_id, sender_qq, matched_workflow)
            if not allowed_wf:
                send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason_wf}")
                return None
            if use_global:
                allowed, reason = check_group_permission(group_id, sender_qq)
                if not allowed:
                    send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason}")
                    return None
            increment_usage(group_id, sender_qq, matched_workflow, use_global)

    # 尺寸
    fwidth, fheight = width, height
    if has_latent and not fwidth:
        user_key = f"group_{group_id}_{sender_qq}" if msg_type == "group" else f"private_{sender_qq}"
        fwidth, fheight = get_user_size(user_key)

    # 排队
    with queue_lock:
        queue_counter += 1
        position = queue_counter

    start_time = time.time()
    workflow = modify_workflow(workflow, wf_config,
                               positive_prompts=[prompt_text] if has_positive and prompt_text else None,
                               size_width=fwidth, size_height=fheight)
    prompt_id = submit_to_comfyui(workflow)
    if not prompt_id:
        with queue_lock:
            queue_counter -= 1
        send_msg(sender_qq, group_id, msg_type, "❌ 提交失败")
        return None

    # 记录上下文（加入 is_random 标记）
    prompt_context[prompt_id] = {
        "message_type": msg_type,
        "group_id": group_id,
        "sender_qq": sender_qq,
        "prompt_text": prompt_text,
        "size_name": size_name,
        "output_type": output_type,
        "workflow_name": matched_workflow,
        "retry": 0,
        "start_time": start_time,
        "is_random": is_random
    }

    if total_in_batch == 1:
        tag_map = {"image": "🎨", "text": "📝", "video": "🎬"}
        tag = tag_map.get(output_type, "🎨")
        if msg_type == "group":
            # ── 按工作流使用模式显示不同的次数提醒 ──
            db = get_db()
            today = datetime.date.today().isoformat()
            wf_row = db.execute("SELECT usage_mode, usage_limit FROM workflows WHERE name=?", (matched_workflow,)).fetchone()
            wf_mode = str(wf_row["usage_mode"] if wf_row else "global").strip()
            wf_limit = int(wf_row["usage_limit"] if wf_row else 0)
            if wf_mode == "unlimited":
                reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务"
            elif wf_mode == "limited":
                used_row = db.execute("SELECT count FROM workflow_daily_usage WHERE qq=? AND group_id=? AND workflow=? AND date=?", (sender_qq, group_id, matched_workflow, today)).fetchone()
                wf_used = used_row["count"] if used_row else 0
                wf_remaining = wf_limit - wf_used
                reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务 · {matched_workflow}剩余{wf_remaining}/{wf_limit}次（每日0点刷新）"
            else:
                used_row = db.execute("SELECT count FROM daily_usage WHERE qq=? AND group_id=? AND date=?", (sender_qq, group_id, today)).fetchone()
                used = used_row["count"] if used_row else 0
                max_count = int(get_config("max_daily_count", "20"))
                # ── 支持 -1 无限制 ──
                if max_count == -1:
                    reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务"
                else:
                    remaining = max_count - used
                    reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务 · 剩余{remaining}次（每日0点刷新）"
            db.close()
        else:
            reply = f"{tag} 已提交 [{matched_workflow}] 前边还有{position-1}个任务"
        msg_id = send_msg(sender_qq, group_id, msg_type, reply)
        if msg_id:
            schedule_delete(msg_id, 15)

    # ── 记录历史，保存返回的ID ──
    history_id = add_history(sender_qq, group_id, matched_workflow, output_type, prompt_text, size_name, "进行中", msg_type=msg_type)
    prompt_context[prompt_id]["history_id"] = history_id
    task_queue.put(prompt_id)

    log_task_submit(sender_qq, group_id, matched_workflow, output_type, "私聊" if msg_type == "private" else "群聊")

    # 记录"再来一张"上下文（仅图片任务）
    if output_type == "image":
        if msg_type == "private":
            last_private_prompt[sender_qq] = {
                "prompt_text": prompt_text,
                "workflow_name": matched_workflow,
                "size_name": size_name,
                "width": fwidth,
                "height": fheight,
                "has_positive": has_positive,
                "has_latent": has_latent,
                "output_type": output_type,
            }
        elif msg_type == "group":
            group_key = f"{group_id}_{sender_qq}"
            with last_group_prompt_lock:
                last_group_prompt[group_key] = {
                    "prompt_text": prompt_text,
                    "workflow_name": matched_workflow,
                    "size_name": size_name,
                    "width": fwidth,
                    "height": fheight,
                    "has_positive": has_positive,
                    "has_latent": has_latent,
                    "output_type": output_type,
                }

    return prompt_id

# =====================================================================
# 结果监控线程
# =====================================================================
class ResultMonitor:
    """监控 ComfyUI 任务完成状态，发送结果"""
    def __init__(self):
        self.running = True

    def run(self):
        logger.info("监控线程启动")
        while self.running:
            try:
                prompt_id = task_queue.get(timeout=2)
                ctx = prompt_context.get(prompt_id)
                if not ctx:
                    continue

                # 等待 ComfyUI 生成完成
                result = get_history_result(prompt_id, ctx.get("output_type", "image"))
                global queue_counter
                with queue_lock:
                    queue_counter = max(0, queue_counter - 1)

                duration = time.time() - ctx.get("start_time", time.time())
                history_id = ctx.get("history_id")

                # ── 生成失败，尝试重试 ──
                if not result:
                    # ⭐ 如果任务已被手动中断，不重试
                    if ctx.get("interrupted"):
                        update_history(ctx.get("history_id"), "已取消", duration)
                        log_task_result(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""),
                                       True, duration, has_image=False)
                        continue
                    retry = ctx.get("retry", 0)
                    if retry < 1:
                        ctx["retry"] = retry + 1
                        with queue_lock:
                            queue_counter += 1
                        wf, _ = load_workflow_json(ctx.get("workflow_name", ""))
                        if wf:
                            new_pid = submit_to_comfyui(wf)
                            if new_pid:
                                prompt_context[new_pid] = ctx
                                task_queue.put(new_pid)
                                send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], "🔄 重试中...")
                                continue
                    # 重试失败 → 更新历史记录为失败
                    send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], "❌ 失败")
                    update_history(history_id, "失败", duration)
                    log_task_result(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""), False, duration)
                    continue

                # ── 文字输出 ──
                if ctx.get("output_type") == "text":
                    text_output = result
                    if text_output:
                        full_text = "\n".join(text_output)
                        mid = send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], f"📝 反推结果 ({duration:.0f}秒)\n{full_text}")
                        if mid and ctx.get("group_id"):
                            schedule_delete(mid, 15)
                    else:
                        send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], "❌ 未提取到文字")
                    update_history(history_id, "成功" if text_output else "失败", duration)
                    log_task_result(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""),
                                   bool(text_output), duration, has_text=bool(text_output))
                    continue

                # ── 视频输出 ──
                if ctx.get("output_type") == "video":
                    sent_ids = []
                    total = len(result)
                    display_flags = get_config("private_display_flags", "prompt,size,duration,workflow") if ctx["message_type"] == "private" else get_config("group_display_flags", "duration")
                    flag_list = [f.strip() for f in display_flags.split(",") if f.strip()]

                    for idx, media_path in enumerate(result):
                        abs_path = str(Path(media_path).resolve()).replace("\\", "/")
                        cap_parts = ["🎬 视频完成"]
                        if "duration" in flag_list:
                            cap_parts.append(f" {duration:.0f}秒")
                        if "workflow" in flag_list:
                            cap_parts.append(f" [{ctx.get('workflow_name','')}]")
                        cap = "".join(cap_parts)

                        # 先发文字，再发视频（分两条消息，QQ不支持文字+视频混合）
                        text_mid = send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], cap)
                        if text_mid:
                            sent_ids.append(text_mid)
                        # 发送视频文件，最多重试2次
                        video_sent = False
                        for attempt in range(2):
                            video_mid = send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], f"[CQ:video,file=file:///{abs_path}]")
                            if video_mid:
                                sent_ids.append(video_mid)
                                video_sent = True
                                break
                            if attempt < 1:
                                time.sleep(2)
                        if not video_sent:
                            send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], "⚠️ 视频上传失败")
                        if idx < total - 1:
                            time.sleep(1)

                    if ctx.get("group_id") and sent_ids:
                        add_recent_task(ctx["group_id"], ctx["sender_qq"], sent_ids)
                    update_history(history_id, "成功", duration)
                    log_task_result(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""),
                                   True, duration, has_image=True)
                    continue

                # ── 图片输出 ──
                sent_ids = []
                total = len(result)

                is_random = ctx.get("is_random", False)

                if ctx["message_type"] == "private":
                    if is_random:
                        display_flags = get_config("random_private_display_flags", "prompt,size,duration,workflow")
                    else:
                        display_flags = get_config("private_display_flags", "prompt,size,duration,workflow")
                    flag_list = [f.strip() for f in display_flags.split(",") if f.strip()]

                    for idx, img_path in enumerate(result):
                        abs_path = str(Path(img_path).resolve()).replace("\\", "/")
                        cap_parts = ["✅ 完成"]
                        if "duration" in flag_list:
                            cap_parts.append(f" {duration:.0f}秒")
                        if "workflow" in flag_list:
                            cap_parts.append(f" [{ctx.get('workflow_name','')}]")
                        if "size" in flag_list and ctx.get("size_name"):
                            cap_parts.append(f" {ctx['size_name']}")
                        if "prompt" in flag_list and ctx.get("prompt_text"):
                            cap_parts.append(f"\n{ctx['prompt_text']}")

                        cap = "".join(cap_parts)
                        if total > 1:
                            cap += f"\n(第{idx+1}/{total}张)"
                        text = f"{cap}\n[CQ:image,file=file:///{abs_path}]"
                        mid = send_msg(ctx["sender_qq"], None, "private", text)
                        if mid:
                            sent_ids.append(mid)
                        if idx < total - 1:
                            time.sleep(1)

                    if total > 1:
                        time.sleep(1)
                        send_msg(ctx["sender_qq"], None, "private", f"✅ {total}张全部生成完毕")

                else:
                    if is_random:
                        display_flags = get_config("random_group_display_flags", "prompt,duration")
                    else:
                        display_flags = get_config("group_display_flags", "duration")
                    flag_list = [f.strip() for f in display_flags.split(",") if f.strip()]

                    for idx, img_path in enumerate(result):
                        abs_path = str(Path(img_path).resolve()).replace("\\", "/")
                        cap_parts = ["✨ 任务完成"]
                        if "duration" in flag_list:
                            cap_parts.append(f" {duration:.0f}秒")
                        if "workflow" in flag_list:
                            cap_parts.append(f" [{ctx.get('workflow_name','')}]")
                        if "size" in flag_list and ctx.get("size_name"):
                            cap_parts.append(f" {ctx['size_name']}")
                        if "prompt" in flag_list and ctx.get("prompt_text"):
                            cap_parts.append(f"\n{ctx['prompt_text']}")

                        cap = "".join(cap_parts)
                        if total > 1:
                            if idx == 0:
                                cap += f"\n(第1/{total}张)"
                            else:
                                cap = f"(第{idx+1}/{total}张)"
                        text = f"{cap}\n[CQ:image,file=file:///{abs_path}]"
                        mid = send_msg(None, ctx["group_id"], "group", text)
                        if mid:
                            sent_ids.append(mid)
                        if idx < total - 1:
                            time.sleep(1)

                if ctx.get("group_id") and sent_ids:
                    add_recent_task(ctx["group_id"], ctx["sender_qq"], sent_ids)

                update_history(history_id, "成功", duration)

                log_task_result(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""),
                               True, duration, has_image=True)

            except queue.Empty:
                # 每 2 秒检查超时
                now = time.time()

                # ── 管理员菜单超时 ──
                expired = []
                for sqq, menu in admin_menu.items():
                    if now - menu.get("last_active", now) > 30:
                        expired.append(sqq)
                for sqq in expired:
                    last_id = admin_menu[sqq].get("last_msg_id")
                    if last_id:
                        delete_msg(last_id)
                    del admin_menu[sqq]
                    send_msg(sqq, None, "private", "⏰ 管理菜单已超时退出")

                # ── 确认状态机超时 ──
                with pending_confirm_lock:
                    expired_keys = []
                    for ukey, state in pending_confirm_states.items():
                        if now > state.get("expires_at", now + 60):
                            expired_keys.append(ukey)
                    for ukey in expired_keys:
                        state = pending_confirm_states.pop(ukey)
                        for mid in state.get("sent_msg_ids", []):
                            delete_msg(mid)
                        if ukey.startswith("private_"):
                            send_msg(ukey.replace("private_", ""), None, "private", "⏰ 已超时，任务已取消")
                        else:
                            parts = ukey.replace("group_", "").rsplit("_", 1)
                            if len(parts) == 2:
                                send_msg(None, parts[0], "group", f"[CQ:at,qq={parts[1]}] ⏰ 已超时，任务已取消")

                # ── 视频尺寸询问超时 ──
                with video_size_lock:
                    expired_keys = []
                    for ukey, state in video_size_states.items():
                        if now > state.get("expires_at", now + 60):
                            expired_keys.append(ukey)
                    for ukey in expired_keys:
                        state = video_size_states.pop(ukey)
                        for mid in state.get("sent_msg_ids", []):
                            delete_msg(mid)
                        if ukey.startswith("private_"):
                            send_msg(ukey.replace("private_", ""), None, "private", "⏰ 已超时，任务已取消")
                        else:
                            parts = ukey.replace("group_", "").rsplit("_", 1)
                            if len(parts) == 2:
                                send_msg(None, parts[0], "group", f"[CQ:at,qq={parts[1]}] ⏰ 已超时，任务已取消")

                # ── 视频时长询问超时 ──
                with video_duration_lock:
                    expired_keys = []
                    for ukey, state in video_duration_states.items():
                        if now > state.get("expires_at", now + 60):
                            expired_keys.append(ukey)
                    for ukey in expired_keys:
                        state = video_duration_states.pop(ukey)
                        for mid in state.get("sent_msg_ids", []):
                            delete_msg(mid)
                        if ukey.startswith("private_"):
                            send_msg(ukey.replace("private_", ""), None, "private", "⏰ 已超时，任务已取消")
                        else:
                            parts = ukey.replace("group_", "").rsplit("_", 1)
                            if len(parts) == 2:
                                send_msg(None, parts[0], "group", f"[CQ:at,qq={parts[1]}] ⏰ 已超时，任务已取消")

            except Exception as e:
                logger.error(f"监控线程异常: {e}")

# =====================================================================
# WebSocket 连接管理
# =====================================================================
def on_message(ws, message):
    """收到 WebSocket 消息时的回调函数"""
    try:
        event = json.loads(message)
        handle_message(event)
    except Exception as e:
        logger.error(f"WebSocket 消息处理异常: {e}")

def on_error(ws, error):
    """WebSocket 错误回调"""
    logger.error(f"WebSocket 错误: {error}")

def on_close(ws, close_status_code, close_msg):
    """WebSocket 关闭回调"""
    logger.warning(f"WebSocket 已关闭 (code={close_status_code})")

def on_open(ws):
    """WebSocket 打开回调"""
    logger.info("WebSocket 已连接")

def start_websocket():
    """启动 WebSocket 连接，支持断线自动重连"""
    while True:
        try:
            ws_url = NAPCAT_WS
            logger.info(f"正在连接 WebSocket: {ws_url}")
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.error(f"WebSocket 连接异常: {e}")
        logger.info("10 秒后尝试重连...")
        time.sleep(10)

# =====================================================================
# ComfyUI & NapCat 健康检查线程
# =====================================================================
def start_health_check():
    """每 30 秒检查一次 ComfyUI 和 NapCat 健康状态"""
    while True:
        check_comfyui_online()
        check_napcat_online()
        time.sleep(30)

# =====================================================================
# 主程序入口
# =====================================================================
def main():
    """启动 ArtLink 的所有服务"""
    logger.info("=" * 50)
    logger.info("ArtLink v1.2.0 正在启动...")
    logger.info(f"工作目录: {BASE_DIR}")
    logger.info(f"Python: {'便携版' if USE_PORTABLE else '系统版'}")
    logger.info(f"数据库: {DB_PATH}")
    logger.info("=" * 50)

    # 初始化数据库与配置
    init_db()
    init_config()

    # 确保必要目录存在
    (BASE_DIR / "workflows").mkdir(exist_ok=True)
    (BASE_DIR / "temp_images").mkdir(exist_ok=True)
    (BASE_DIR / "temp_video").mkdir(exist_ok=True)

    # 启动健康检查线程
    health_thread = threading.Thread(target=start_health_check, daemon=True)
    health_thread.start()

    # 启动结果监控线程
    monitor = ResultMonitor()
    monitor_thread = threading.Thread(target=monitor.run, daemon=True)
    monitor_thread.start()

    # 启动 WebSocket 线程
    ws_thread = threading.Thread(target=start_websocket, daemon=True)
    ws_thread.start()

    # 等待 WebSocket 连接稳定
    time.sleep(2)

    # 启动 Flask Web 管理后台
    logger.info(f"Web 管理后台: http://{APP_HOST}:{APP_PORT}")
    app.run(host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    main()
