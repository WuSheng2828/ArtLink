#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ArtLink - ComfyUI QQ 机器人控制台 (单文件版) v1.0.1
==================================================
通过 QQ（NapCat）远程控制 ComfyUI 进行 AI 绘图和图片反推。
支持图片生成、文字反推、多层白名单、黑名单、自助注册、
管理菜单、快捷指令、消息撤回、排队提示、失败重试、历史记录等。

开源首发版本 v1.0.1，面向社区开放使用和二次开发。

部署要求：
  Windows 10/11, Python 3.12（便携版或系统环境）, NapCat QQ 机器人, ComfyUI

项目地址：https://github.com/yourname/ArtLink
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
NAPCAT_HTTP = "http://127.0.0.1:3000"    # NapCat HTTP API 地址
NAPCAT_WS = "ws://127.0.0.1:3001"        # NapCat WebSocket 地址
COMFYUI_SERVER = "http://127.0.0.1:8188" # ComfyUI 服务地址

# 默认输入目录（反推图片存放位置，首次启动写入数据库，后续可在后台修改）
COMFYUI_INPUT = Path(r"ComfyUI/input")

# 全局状态变量
comfyui_online = True          # ComfyUI 是否在线
comfyui_fail_count = 0         # 连续失败次数
comfyui_lock = threading.Lock()  # 线程锁

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
# 记录最近 2 分钟内生成的图片消息 ID，用于撤回功能
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
    """从 config 表加载所有尺寸预设，按触发词长度降序排列"""
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
    """返回默认尺寸：正方 1024×1024"""
    return ("正方", 1024, 1024)

def save_size_preset(name, width, height, trigger_word):
    """添加或更新一个尺寸预设"""
    size_list_str = get_config("size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if name not in names:
        names.append(name)
        set_config("size_preset_list", ",".join(names))
    set_config(f"size_{name}_width", str(width))
    set_config(f"size_{name}_height", str(height))
    set_config(f"size_{name}_trigger", trigger_word)

def delete_size_preset(name):
    """删除一个尺寸预设"""
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
    """首次启动时，自动写入默认的三个尺寸预设"""
    if not get_config("size_preset_list"):
        save_size_preset("正方", 1024, 1024, "正方")
        save_size_preset("横版", 1344, 768, "横版")
        save_size_preset("竖版", 768, 1344, "竖版")
        logger.info("已初始化默认尺寸预设")

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
        "CREATE TABLE IF NOT EXISTS workflows (name TEXT PRIMARY KEY, file_path TEXT, positive_node_title TEXT DEFAULT '', latent_node_title TEXT DEFAULT '', image_node_title TEXT DEFAULT '', note TEXT DEFAULT '', example TEXT DEFAULT '', output_type TEXT DEFAULT 'image')",
        "CREATE TABLE IF NOT EXISTS user_size (user_key TEXT PRIMARY KEY, width INTEGER, height INTEGER)",
        "CREATE TABLE IF NOT EXISTS daily_usage (qq TEXT, group_id TEXT, date TEXT, count INTEGER, PRIMARY KEY (qq, group_id, date))",
        "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS admin_list (qq TEXT PRIMARY KEY, remark TEXT DEFAULT '')",
    ]
    for sql in tables:
        c.execute(sql)

    # 兼容旧版本：为可能缺失的字段自动补全
    new_columns = [
        ("duration", "history"), ("remark", "private_whitelist"),
        ("remark", "group_whitelist"), ("remark", "group_member_whitelist"),
        ("example", "workflows"), ("output_type", "workflows"),
    ]
    for col, tab in new_columns:
        try:
            c.execute(f"ALTER TABLE {tab} ADD COLUMN {col} TEXT DEFAULT ''")
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
            "次数每日 0 点刷新"
        ),
        "help_triggers": "说明,帮助,help,？,?,帮助文档,使用帮助,指令,菜单,功能,教程,怎么用,使用说明",
        "filter_enabled": "false",    # 过滤功能默认关闭
        "filter_words": "",           # 默认无过滤词
    }
    # 注意：不再写入 comfyui_output_dir，因为 v1.0.1 使用 /view 接口获取图片
    for k, v in defaults.items():
        if get_config(k) is None:
            set_config(k, v)
    init_size_presets()

def notify_admin(text):
    """向所有管理员发送通知消息"""
    db = get_db()
    admins = db.execute("SELECT qq FROM admin_list").fetchall()
    db.close()
    for row in admins:
        send_private_message(row["qq"], f"🔔 [ArtLink]\n{text}")

# =====================================================================
# Flask 应用
# =====================================================================
app = Flask(__name__)

# =====================================================================
# 管理后台 HTML 模板（内嵌，单文件部署）
# =====================================================================
ADMIN_HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>ArtLink v1.0.1</title>
<style>
    body{font-family:Microsoft YaHei,sans-serif;max-width:1100px;margin:20px auto;padding:0 20px;background:#f5f5f5}
    h1{color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px}h2{color:#2c3e50;margin-top:0}
    .section{margin:20px 0;padding:20px;border:1px solid #ddd;border-radius:10px;background:#fff;box-shadow:0 2px 4px rgba(0,0,0,.1)}
    table{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}th,td{padding:8px;border:1px solid #e0e0e0}th{background:#3498db;color:#fff}
    .btn{padding:6px 16px;cursor:pointer;background:#3498db;color:#fff;border:none;border-radius:5px;font-size:13px;margin:2px}.btn:hover{background:#2980b9}
    .btn.danger{background:#e74c3c}.btn.warning{background:#f39c12}.btn.success{background:#27ae60}.btn.dark{background:#2c3e50}.btn.small{padding:3px 8px;font-size:11px}
    input,select,textarea{padding:6px;border:1px solid #ccc;border-radius:5px;font-size:13px}textarea{resize:vertical}
    .inline-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
    .badge{display:inline-block;padding:2px 8px;background:#27ae60;color:#fff;border-radius:10px;font-size:11px;margin-left:5px}
    .badge-red{background:#e74c3c}.badge-blue{background:#3498db}.info{color:#7f8c8d;font-size:12px;margin:5px 0}
    .tag-image{background:#3498db;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px}
    .tag-text{background:#9b59b6;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px}
    .remark{color:#888;font-size:12px}.blacklist-row{background:#fff5f5}
    .stats-grid{display:flex;gap:15px;flex-wrap:wrap}.stat-card{flex:1;min-width:100px;padding:15px;background:#f8f9fa;border-radius:8px;text-align:center}
    .stat-card .num{font-size:26px;font-weight:700;color:#2c3e50}.stat-card .label{font-size:12px;color:#7f8c8d;margin-top:5px}
    .status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
    .status-online{background:#27ae60}.status-offline{background:#e74c3c}
    .pagination{display:flex;gap:10px;align-items:center;justify-content:center;margin:10px 0}
    .history-collapse{cursor:pointer;user-select:none}.history-collapse:hover{opacity:0.8}
</style></head><body>
<h1>🎨 ArtLink v1.0.1</h1>
<div class="section"><h2>📊 今日统计</h2><div class="stats-grid">
<div class="stat-card"><div class="num">{{stats.today_images}}</div><div class="label">今日生图</div></div>
<div class="stat-card"><div class="num">{{stats.today_texts}}</div><div class="label">今日反推</div></div>
<div class="stat-card"><div class="num">{{stats.today_users}}</div><div class="label">活跃用户</div></div>
<div class="stat-card"><div class="num">{{stats.total_images}}</div><div class="label">累计生图</div></div>
<div class="stat-card"><div class="num"><span class="status-dot {% if stats.comfyui_online %}status-online{% else %}status-offline{% endif %}"></span></div><div class="label">ComfyUI {% if stats.comfyui_online %}在线{% else %}离线{% endif %}</div></div>
</div></div>
<div class="section"><h2>⚙️ 系统配置</h2>
<!-- 移除 ComfyUI 输出目录配置，v1.0.1 使用 /view 接口获取图片 -->
<div class="inline-form"><label>ComfyUI输入:</label><input type="text" id="comfyui_input_dir" value="{{config.comfyui_input_dir}}" size="45"><button class="btn" onclick="updateConfig('comfyui_input_dir')">保存</button><span class="info">反推图片存放目录</span></div>
<div class="inline-form"><label>NapCat:</label><input type="text" id="napcat_http" value="{{config.napcat_http}}" size="30"><button class="btn" onclick="updateConfig('napcat_http')">保存</button><label>ComfyUI:</label><input type="text" id="comfyui_server" value="{{config.comfyui_server}}" size="30"><button class="btn" onclick="updateConfig('comfyui_server')">保存</button></div>
<div class="inline-form"><label>每日上限:</label><input type="number" id="max_daily_count" value="{{config.max_daily_count}}" style="width:60px"><button class="btn" onclick="updateConfig('max_daily_count')">保存</button></div>
<div class="inline-form"><label>提示词过滤:</label>
<select id="filter_enabled" onchange="updateConfig('filter_enabled')">
  <option value="false" {% if config.filter_enabled=='false' %}selected{% endif %}>关闭</option>
  <option value="true" {% if config.filter_enabled=='true' %}selected{% endif %}>开启</option>
</select>
<input type="text" id="filter_words" value="{{config.filter_words}}" placeholder="过滤词，逗号分隔" size="40">
<button class="btn" onclick="updateConfig('filter_words')">保存过滤词</button>
<span class="info">开启后自动从提示词中移除这些词</span></div>
<div class="inline-form"><label>公共说明:</label><textarea id="public_help" rows="5" style="width:500px">{{config.public_help}}</textarea><button class="btn" onclick="updateConfig('public_help')">保存</button></div>
<div class="inline-form"><label>说明触发词:</label><input type="text" id="help_triggers" value="{{config.help_triggers}}" size="50" placeholder="逗号分隔"><button class="btn" onclick="updateConfig('help_triggers')">保存</button><span class="info">多个用英文逗号分隔</span></div>
</div>
<div class="section"><h2>👑 管理员 <span class="badge">{{admin_count}}人</span></h2>
<div class="inline-form"><input type="text" id="admin_qq" placeholder="QQ号"><input type="text" id="admin_remark" placeholder="备注" style="width:150px"><button class="btn" onclick="addAdmin()">添加</button></div>
<table><tr><th>QQ号</th><th>备注</th><th>操作</th></tr>{% for a in admin_list %}<tr><td>{{a.qq}}</td><td><span class="remark">{{a.remark or ''}}</span></td><td><button class="btn danger small" onclick="delAdmin('{{a.qq}}')">删除</button></td></tr>{% endfor %}</table></div>
<div class="section"><h2>👤 私聊白名单 <span class="badge">{{private_count}}人</span></h2>
<div class="inline-form"><input type="text" id="private_qq" placeholder="QQ号"><input type="text" id="private_remark" placeholder="备注" style="width:150px"><button class="btn" onclick="addPrivateQQ()">添加</button></div>
<table><tr><th>QQ号</th><th>备注</th><th>操作</th></tr>{% for p in private_list %}<tr><td>{{p.qq}}</td><td><span class="remark">{{p.remark or ''}}</span></td><td><button class="btn success small" onclick="editPrivateRemark('{{p.qq}}')">备注</button><button class="btn danger small" onclick="delPrivateQQ('{{p.qq}}')">删除</button></td></tr>{% endfor %}</table></div>
<div class="section"><h2>👥 群聊白名单 <span class="badge">{{group_count}}个群</span></h2>
<div class="inline-form"><input type="text" id="group_id" placeholder="群号"><input type="text" id="group_remark" placeholder="备注" style="width:150px"><button class="btn" onclick="addGroup()">添加群</button></div>
<table><tr><th>群号</th><th>备注</th><th>操作</th></tr>{% for g in group_list %}<tr><td>{{g.group_id}}</td><td><span class="remark">{{g.remark or ''}}</span></td><td><button class="btn success small" onclick="editGroupRemark('{{g.group_id}}')">备注</button><button class="btn danger small" onclick="delGroup('{{g.group_id}}')">删除</button></td></tr>{% endfor %}</table></div>
<div class="section"><h2>👥 群成员白名单</h2>
<div class="inline-form"><label>选择群:</label><select id="member_group_select" onchange="loadMembers()"><option value="">--选择--</option>{% for g in group_list %}<option value="{{g.group_id}}">{{g.group_id}}{% if g.remark %} ({{g.remark}}){% endif %}</option>{% endfor %}</select><input type="text" id="member_qq" placeholder="QQ号"><input type="text" id="member_remark" placeholder="备注" style="width:150px"><button class="btn" onclick="addMember()">添加</button><button class="btn warning small" onclick="resetGroupUsage()">重置群次数</button></div>
<table id="member_table"><tr><th>QQ号</th><th>备注</th><th>今日已用</th><th>操作</th></tr></table></div>
<div class="section"><h2>🚫 黑名单 <span class="badge badge-red">{{blacklist_count}}人</span></h2>
<div class="inline-form"><label>选择群:</label><select id="blacklist_group_select" onchange="loadBlacklist()"><option value="">--选择--</option>{% for g in group_list %}<option value="{{g.group_id}}">{{g.group_id}}{% if g.remark %} ({{g.remark}}){% endif %}</option>{% endfor %}</select></div>
<table id="blacklist_table"><tr><th>QQ号</th><th>备注</th><th>操作</th></tr></table></div>
<div class="section">
  <h2 class="history-collapse" onclick="toggleHistory()">
    📋 历史 <span class="badge badge-blue">{{history_count}}条</span>
    <span id="history_arrow" style="font-size:14px;margin-left:8px">展开 ▼</span>
  </h2>
  <div id="history_body" style="display:none">
    <div class="inline-form">
        <label>状态:</label>
        <select id="history_status" onchange="searchHistory()">
            <option value="">全部</option>
            <option value="成功">成功</option>
            <option value="失败">失败</option>
            <option value="进行中">进行中</option>
        </select>
        <input type="text" id="history_search" placeholder="搜索" style="width:200px">
        <button class="btn" onclick="searchHistory(1)">搜索</button>
        <button class="btn warning small" onclick="confirmClearHistory()">清空</button>
    </div>
    <table id="history_table"><tr><th>时间</th><th>QQ</th><th>群</th><th>工作流</th><th>类型</th><th>提示词</th><th>尺寸</th><th>耗时</th><th>状态</th></tr></table>
    <div class="pagination" id="history_pager"></div>
  </div>
</div>
<div class="section"><h2>🔤 触发词</h2>
<div class="inline-form"><input type="text" id="trigger_word" placeholder="触发词"><select id="workflow_select">{% for wf in workflows %}<option value="{{wf.name}}">{{wf.name}}</option>{% endfor %}</select><button class="btn" onclick="addTrigger()">添加</button></div>
<table><tr><th>触发词</th><th>工作流</th><th>操作</th></tr>{% for tw in triggers %}<tr><td>{{tw.word}}</td><td>{{tw.workflow}}</td><td><button class="btn danger small" onclick="delTrigger('{{tw.word}}')">删除</button></td></tr>{% endfor %}</table></div>
<div class="section"><h2>📐 自定义尺寸 <span class="badge">{{size_presets|length}}种</span></h2>
<table id="size_table"><tr><th>名称</th><th>宽×高</th><th>触发词</th><th>操作</th></tr>{% for sp in size_presets %}<tr><td>{{sp.name}}</td><td>{{sp.width}}×{{sp.height}}</td><td><input type="text" id="trigger_{{sp.name}}" value="{{sp.trigger_word}}" size="10"><button class="btn success small" onclick="updateSizeTrigger('{{sp.name}}')">改触发词</button></td><td><button class="btn danger small" onclick="delSize('{{sp.name}}')">删除</button></td></tr>{% endfor %}</table>
<h3>添加自定义尺寸</h3>
<div class="inline-form"><input type="text" id="new_size_name" placeholder="名称"><input type="number" id="new_size_width" placeholder="宽" style="width:70px"><input type="number" id="new_size_height" placeholder="高" style="width:70px"><input type="text" id="new_size_trigger" placeholder="触发词"><button class="btn" onclick="addSize()">添加</button></div></div>
<div class="section"><h2>📁 工作流</h2><table><tr><th>名称</th><th>文件</th><th>类型</th><th>提示词</th><th>Latent</th><th>图片</th><th>备注</th><th>示例</th><th>操作</th></tr>{% for wf in workflows %}<tr><td>{{wf.name}}</td><td>{{wf.file_path}}</td><td>{% if wf.output_type=='text' %}<span class="tag-text">文字</span>{% else %}<span class="tag-image">图片</span>{% endif %}</td><td>{{wf.positive_node_title or '-'}}</td><td>{{wf.latent_node_title or '-'}}</td><td>{{wf.image_node_title or '-'}}</td><td>{{wf.note or '-'}}</td><td style="max-width:120px;overflow:hidden;text-overflow:ellipsis">{{wf.example or '-'}}</td><td><button class="btn danger small" onclick="delWorkflow('{{wf.name}}')">删除</button></td></tr>{% endfor %}</table>
<h3>添加</h3><div class="inline-form"><input type="text" id="wf_name" placeholder="名称"><select id="wf_file_select"></select><input type="text" id="wf_file" placeholder="或手动输入json路径" size="25"><select id="wf_output_type"><option value="image">🖼️图片</option><option value="text">📝文字</option></select><input type="text" id="wf_positive" placeholder="提示词节点"><input type="text" id="wf_latent" placeholder="Latent节点"><input type="text" id="wf_image" placeholder="图片节点"><input type="text" id="wf_note" placeholder="备注"><input type="text" id="wf_example" placeholder="示例" style="width:160px"><button class="btn" onclick="addWorkflow()">添加</button></div></div>
<script>
function F(u,m,b){return fetch(u,{method:m,headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null}).then(r=>r.json()).then(d=>{if(d.status==='ok')location.reload();else alert(d.message)})}
function addAdmin(){let q=document.getElementById('admin_qq').value.trim(),r=document.getElementById('admin_remark').value.trim();if(q)F('/api/admin/add','POST',{qq:q,remark:r});else alert('请输入QQ号')}
function delAdmin(q){if(confirm('确认删除管理员？'))F('/api/admin/delete','POST',{qq:q})}
function addPrivateQQ(){let q=document.getElementById('private_qq').value.trim(),r=document.getElementById('private_remark').value.trim();if(q)F('/api/private_whitelist/add','POST',{qq:q,remark:r});else alert('请输入QQ号')}
function delPrivateQQ(q){if(confirm('确认删除？'))F('/api/private_whitelist/delete','POST',{qq:q})}
function editPrivateRemark(q){let r=prompt('新备注:');if(r!==null)F('/api/private_whitelist/remark','POST',{qq:q,remark:r.trim()})}
function addGroup(){let g=document.getElementById('group_id').value.trim(),r=document.getElementById('group_remark').value.trim();if(g)F('/api/group_whitelist/add','POST',{group_id:g,remark:r});else alert('请输入群号')}
function delGroup(g){if(confirm('确认删除该群及所有成员？'))F('/api/group_whitelist/delete','POST',{group_id:g})}
function editGroupRemark(g){let r=prompt('新备注:');if(r!==null)F('/api/group_whitelist/remark','POST',{group_id:g,remark:r.trim()})}
function addMember(){let g=document.getElementById('member_group_select').value,q=document.getElementById('member_qq').value.trim(),r=document.getElementById('member_remark').value.trim();if(!g)alert('请选择群');else if(!q)alert('请输入QQ号');else F('/api/group_member/add','POST',{group_id:g,qq:q,remark:r})}
function delMember(g,q){if(confirm('确认删除？'))F('/api/group_member/delete','POST',{group_id:g,qq:q})}
function editMemberRemark(g,q){let r=prompt('新备注:');if(r!==null)F('/api/group_member/remark','POST',{group_id:g,qq:q,remark:r.trim()})}
function resetMemberUsage(g,q){if(confirm('重置？'))F('/api/usage/reset_member','POST',{group_id:g,qq:q})}
function resetGroupUsage(){let g=document.getElementById('member_group_select').value;if(!g){alert('请选择群');return}if(confirm('确认重置？'))F('/api/usage/reset_group','POST',{group_id:g})}
function banMember(g,q){if(!confirm('拉黑？'))return;F('/api/blacklist/add','POST',{group_id:g,qq:q})}
function unbanMember(g,q){if(!confirm('取消拉黑？'))return;F('/api/blacklist/remove','POST',{group_id:g,qq:q})}
function loadMembers(){let g=document.getElementById('member_group_select').value;if(!g){document.getElementById('member_table').innerHTML='<tr><th>QQ号</th><th>备注</th><th>今日已用</th><th>操作</th></tr>';return}fetch('/api/group_member/list_with_usage?group_id='+g).then(r=>r.json()).then(d=>{let t=document.getElementById('member_table');t.innerHTML='<tr><th>QQ号</th><th>备注</th><th>今日已用</th><th>操作</th></tr>';d.members.forEach(m=>{t.innerHTML+=`<tr><td>${m.qq}</td><td><span class="remark">${m.remark||''}</span></td><td>${m.used}/${m.max}</td><td><button class="btn success small" onclick="editMemberRemark('${g}','${m.qq}')">备注</button><button class="btn warning small" onclick="resetMemberUsage('${g}','${m.qq}')">重置</button><button class="btn dark small" onclick="banMember('${g}','${m.qq}')">拉黑</button><button class="btn danger small" onclick="delMember('${g}','${m.qq}')">删除</button></td></tr>`})})}
function loadBlacklist(){let g=document.getElementById('blacklist_group_select').value;if(!g){document.getElementById('blacklist_table').innerHTML='<tr><th>QQ号</th><th>备注</th><th>操作</th></tr>';return}fetch('/api/blacklist/list?group_id='+g).then(r=>r.json()).then(d=>{let t=document.getElementById('blacklist_table');t.innerHTML='<tr><th>QQ号</th><th>备注</th><th>操作</th></tr>';d.blacklist.forEach(m=>{t.innerHTML+=`<tr class="blacklist-row"><td>${m.qq}</td><td><span class="remark">${m.remark||''}</span></td><td><button class="btn success small" onclick="unbanMember('${g}','${m.qq}')">取消拉黑</button></td></tr>`})})}

// ----- 工作流 JSON 文件下拉列表 -----
function loadWorkflowFiles() {
    fetch('/api/workflow/files').then(r=>r.json()).then(d=>{
        let s = document.getElementById('wf_file_select');
        s.innerHTML = '<option value="">--选择JSON文件--</option>';
        d.files.forEach(f=>{ s.innerHTML += `<option value="${f}">${f}</option>` });
        // 选择后自动填入 file_path 输入框
        s.onchange = function() {
            if (s.value) document.getElementById('wf_file').value = s.value;
        };
    });
}

let currentPage=1;
function searchHistory(page){
    if(!page)page=1;currentPage=page;
    let kw=document.getElementById('history_search').value.trim(),status=document.getElementById('history_status').value;
    let params=new URLSearchParams({keyword:kw,status:status,page:page});
    fetch('/api/history/search?'+params).then(r=>r.json()).then(d=>{
        let t=document.getElementById('history_table');
        t.innerHTML='<tr><th>时间</th><th>QQ</th><th>群</th><th>工作流</th><th>类型</th><th>提示词</th><th>尺寸</th><th>耗时</th><th>状态</th></tr>';
        d.data.forEach(h=>{t.innerHTML+=`<tr><td>${h.time}</td><td>${h.qq}${h.remark?' <span class="remark">('+h.remark+')</span>':''}</td><td>${h.group_id||'-'}</td><td>${h.workflow}</td><td>${h.output_type=='text'?'📝':'🖼️'}</td><td style="max-width:120px;overflow:hidden;text-overflow:ellipsis">${h.prompt_text||'-'}</td><td>${h.size||'-'}</td><td>${h.duration?h.duration.toFixed(1)+'秒':'-'}</td><td>${h.status}</td></tr>`;});
        let pager=document.getElementById('history_pager');pager.innerHTML='';
        if(d.total_pages>1){
            pager.innerHTML+=`<button class="btn small" onclick="searchHistory(${page-1})" ${page<=1?'disabled':''}>上一页</button>`;
            pager.innerHTML+=`<span>第 ${page} / ${d.total_pages} 页</span>`;
            pager.innerHTML+=`<button class="btn small" onclick="searchHistory(${page+1})" ${page>=d.total_pages?'disabled':''}>下一页</button>`;
        }
    });
}
function confirmClearHistory(){let a=prompt("警告：清空所有历史！\n请输入 DELETE 确认：");if(a==="DELETE"){fetch('/api/history/clear').then(r=>r.json()).then(d=>{if(d.status==='ok')location.reload()})}else if(a!==null){alert("输入错误，已取消")}}
function addTrigger(){
    let w=document.getElementById('trigger_word').value.trim(),f=document.getElementById('workflow_select').value;if(!w){alert('请输入触发词');return;}
    let existingWords=[];document.querySelectorAll('#trigger_table tr td:first-child').forEach(td=>existingWords.push(td.innerText.trim()));
    if(existingWords.includes(w)){alert('触发词已存在，请勿重复添加');return;}
    let sizeTriggers=[];document.querySelectorAll('[id^="trigger_"]').forEach(inp=>{if(inp.id.startsWith('trigger_')&&!inp.id.startsWith('trigger_word'))sizeTriggers.push(inp.value.trim());});
    if(sizeTriggers.includes(w)){alert('该触发词已被尺寸占用，请更换');return;}
    F('/api/trigger/add','POST',{word:w,workflow:f})
}
function delTrigger(w){F('/api/trigger/delete','POST',{word:w})}
function updateSizeTrigger(n){let w=document.getElementById('trigger_'+n).value.trim();if(w)F('/api/size/update_trigger','POST',{name:n,trigger_word:w})}
function addSize(){
    let n=document.getElementById('new_size_name').value.trim(),w=parseInt(document.getElementById('new_size_width').value)||0,h=parseInt(document.getElementById('new_size_height').value)||0,t=document.getElementById('new_size_trigger').value.trim();
    if(!n||!w||!h||!t){alert('名称、宽、高、触发词都不能为空');return;}
    let existing=[];document.querySelectorAll('[id^="trigger_"]').forEach(inp=>{if(inp.id.startsWith('trigger_')&&!inp.id.startsWith('trigger_word'))existing.push(inp.value.trim());});
    if(existing.includes(t)){alert('触发词已存在，请更换');return;}
    F('/api/size/add','POST',{name:n,width:w,height:h,trigger_word:t})
}
function delSize(n){
    let rows=document.querySelectorAll('#size_table tr');if(rows.length<=2){alert('至少保留一个尺寸，无法删除');return;}
    if(confirm('确认删除尺寸 "'+n+'"？'))F('/api/size/delete','POST',{name:n})
}
function addWorkflow(){
    let n=document.getElementById('wf_name').value.trim();
    // 优先使用下拉选择，如果输入框有值则使用输入框
    let fp=document.getElementById('wf_file_select').value || document.getElementById('wf_file').value.trim();
    if(!n||!fp) { alert('名称和文件路径必填'); return; }
    let d={
        name:n,
        file_path:fp,
        output_type:document.getElementById('wf_output_type').value,
        positive_node_title:document.getElementById('wf_positive').value.trim(),
        latent_node_title:document.getElementById('wf_latent').value.trim(),
        image_node_title:document.getElementById('wf_image').value.trim(),
        note:document.getElementById('wf_note').value.trim(),
        example:document.getElementById('wf_example').value.trim()
    };
    F('/api/workflow/add','POST',d)
}
function delWorkflow(n){if(confirm('确认删除工作流 "'+n+'"?'))F('/api/workflow/delete','POST',{name:n})}
function updateConfig(k){let v=document.getElementById(k).value.trim();F('/api/config/update','POST',{key:k,value:v})}
function toggleHistory(){let body=document.getElementById('history_body'),arrow=document.getElementById('history_arrow');if(body.style.display==='none'){body.style.display='block';arrow.innerText='收起 ▲';searchHistory(1);}else{body.style.display='none';arrow.innerText='展开 ▼';}}
window.onload=function(){let s=document.getElementById('member_group_select');if(s.value)loadMembers(); loadWorkflowFiles();}
</script></body></html>"""

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
        "comfyui_server": get_config("comfyui_server", COMFYUI_SERVER),
        "max_daily_count": get_config("max_daily_count", "20"),
        "public_help": get_config("public_help", ""),
        "help_triggers": get_config("help_triggers", "说明,帮助,help,？,?,帮助文档,使用帮助,指令,菜单,功能,教程,怎么用,使用说明"),
        "filter_enabled": get_config("filter_enabled", "false"),
        "filter_words": get_config("filter_words", ""),
    }
    private_list = [dict(r) for r in db.execute("SELECT qq, remark FROM private_whitelist").fetchall()]
    group_list = [dict(r) for r in db.execute("SELECT group_id, remark FROM group_whitelist").fetchall()]
    triggers = [{"word": r["word"], "workflow": r["workflow"]} for r in db.execute("SELECT * FROM trigger_words").fetchall()]
    workflows = [dict(r) for r in db.execute("SELECT * FROM workflows").fetchall()]
    size_presets = load_size_presets()
    blacklist_count = db.execute("SELECT COUNT(*) as cnt FROM group_blacklist").fetchone()["cnt"]
    history_count = db.execute("SELECT COUNT(*) as cnt FROM history").fetchone()["cnt"]
    admin_list = [dict(r) for r in db.execute("SELECT qq, remark FROM admin_list").fetchall()]
    stats = {
        "today_images": db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='image' AND status='成功'", (today,)).fetchone()["c"],
        "today_texts": db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='text' AND status='成功'", (today,)).fetchone()["c"],
        "today_users": db.execute("SELECT COUNT(DISTINCT qq) as c FROM history WHERE date(time)=?", (today,)).fetchone()["c"],
        "total_images": db.execute("SELECT COUNT(*) as c FROM history WHERE output_type='image' AND status='成功'").fetchone()["c"],
        "comfyui_online": comfyui_online,
    }
    db.close()
    return render_template_string(ADMIN_HTML, config=cfg, private_list=private_list, private_count=len(private_list),
                                   group_list=group_list, group_count=len(group_list), triggers=triggers,
                                   workflows=workflows, size_presets=size_presets,
                                   blacklist_count=blacklist_count, history_count=history_count, stats=stats,
                                   admin_list=admin_list, admin_count=len(admin_list))

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
    return jsonify({"status":"ok"})

@app.route('/api/private_whitelist/delete', methods=['POST'])
def api_del_private():
    qq = request.json.get('qq','').strip()
    db = get_db(); db.execute("DELETE FROM private_whitelist WHERE qq=?",(qq,)); db.commit(); db.close()
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
    db = get_db(); db.execute("INSERT OR REPLACE INTO group_whitelist VALUES (?,?)",(gid,remark)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/group_whitelist/delete', methods=['POST'])
def api_del_group():
    gid = request.json.get('group_id','').strip()
    db = get_db()
    db.execute("DELETE FROM group_whitelist WHERE group_id=?",(gid,))
    db.execute("DELETE FROM group_member_whitelist WHERE group_id=?",(gid,))
    db.execute("DELETE FROM group_blacklist WHERE group_id=?",(gid,))
    db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/group_whitelist/remark', methods=['POST'])
def api_edit_group_remark():
    d = request.json; db = get_db()
    db.execute("UPDATE group_whitelist SET remark=? WHERE group_id=?",(d.get('remark',''), d.get('group_id',''))); db.commit(); db.close()
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
        where_clauses.append("(qq LIKE ? OR group_id LIKE ? OR prompt_text LIKE ?)")
        params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])
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

def add_history(qq, gid, wf, ot, pt, sz, status, dur=0):
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
        db.execute("INSERT INTO history (time,qq,group_id,workflow,output_type,prompt_text,size,status,duration,remark) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (now_str, qq, gid or "", wf, ot, pt or "", sz or "", status, dur, remark))
        db.commit(); db.close()
    except Exception:
        pass

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
    # 后端校验：至少保留一个尺寸
    size_list_str = get_config("size_preset_list", "")
    names = [n.strip() for n in size_list_str.split(",") if n.strip()]
    if len(names) <= 1:
        return jsonify({"status":"error","message":"至少保留一个尺寸"})
    delete_size_preset(name)
    return jsonify({"status":"ok"})

# ---- 工作流 JSON 文件列表 API ----
@app.route('/api/workflow/files')
def api_workflow_files():
    """返回项目目录下所有 .json 文件的相对路径列表"""
    files = scan_workflow_files()
    return jsonify({"files": files})

@app.route('/api/workflow/add', methods=['POST'])
def api_add_workflow():
    d = request.json; name = d.get('name','').strip(); fp = d.get('file_path','').strip()
    if not name or not fp: return jsonify({"status":"error"})
    db = get_db()
    db.execute("INSERT OR REPLACE INTO workflows VALUES (?,?,?,?,?,?,?,?)",
               (name, fp, d.get('positive_node_title',''), d.get('latent_node_title',''),
                d.get('image_node_title',''), d.get('note',''), d.get('example',''), d.get('output_type','image')))
    db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/workflow/delete', methods=['POST'])
def api_del_workflow():
    name = request.json.get('name','').strip()
    db = get_db(); db.execute("DELETE FROM workflows WHERE name=?",(name,)); db.commit(); db.close()
    return jsonify({"status":"ok"})

@app.route('/api/config/update', methods=['POST'])
def api_update_config():
    d = request.json; key = d.get('key','').strip(); value = d.get('value','').strip()
    if not key: return jsonify({"status":"error"})
    set_config(key, value)
    return jsonify({"status":"ok"})

# =====================================================================
# QQ 图片下载（用于反推，支持重试）
# =====================================================================
def download_qq_image(event):
    """从 QQ 消息事件中提取图片并下载到 ComfyUI 输入目录（支持重试）"""
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

        # 下载请求，超时 60 秒，最多尝试 2 次
        resp = None
        for attempt in range(2):
            try:
                resp = requests.get(image_url, timeout=60, stream=True)
                if resp.status_code == 200:
                    break
                else:
                    logger.error(f"图片下载失败(尝试{attempt+1}/2): HTTP {resp.status_code}")
            except requests.Timeout:
                logger.error(f"图片下载超时(尝试{attempt+1}/2): {image_url}")
                if attempt < 1:
                    time.sleep(2)
            except Exception as e:
                logger.error(f"图片下载异常(尝试{attempt+1}/2): {e}")
                if attempt < 1:
                    time.sleep(2)

        if not resp or resp.status_code != 200:
            logger.error(f"图片下载彻底失败: {image_url}")
            return None

        content_type = resp.headers.get("content-type", "")
        ext = ".png" if "png" in content_type else (".gif" if "gif" in content_type else (".webp" if "webp" in content_type else ".jpg"))
        filename = f"artlink_{hashlib.md5(str(time.time()).encode()).hexdigest()[:12]}{ext}"
        input_dir = Path(get_config("comfyui_input_dir", str(COMFYUI_INPUT)))
        input_dir.mkdir(parents=True, exist_ok=True)
        with open(input_dir / filename, 'wb') as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return filename
    except Exception as e:
        logger.error(f"图片下载异常: {e}")
        return None

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

def modify_workflow(workflow, wf_config, positive_prompt=None, size_width=None, size_height=None, input_image_path=None):
    """动态修改工作流 JSON 中的节点值"""
    positive_target = str(wf_config.get("positive_node_title", "")).strip()
    latent_target = str(wf_config.get("latent_node_title", "")).strip()
    image_target = str(wf_config.get("image_node_title", "")).strip() if wf_config.get("image_node_title") else ""

    # 修改提示词节点
    if positive_target and positive_prompt:
        for node_id, node in workflow.items():
            matched = (positive_target.isdigit() and node_id == positive_target) or (node.get("_meta", {}).get("title") == positive_target)
            if matched and "inputs" in node and "text" in node["inputs"]:
                node["inputs"]["text"] = positive_prompt
                break

    # 修改 Latent 尺寸节点
    if latent_target and size_width and size_height:
        for node_id, node in workflow.items():
            matched = (latent_target.isdigit() and node_id == latent_target) or (node.get("_meta", {}).get("title") == latent_target)
            if matched and "inputs" in node:
                node["inputs"]["width"] = size_width
                node["inputs"]["height"] = size_height
                break

    # 修改图片输入节点（用于反推等）
    if image_target and input_image_path:
        for node_id, node in workflow.items():
            matched = (image_target.isdigit() and node_id == image_target) or (node.get("_meta", {}).get("title") == image_target)
            if matched and "inputs" in node:
                key = next((k for k in ["image", "choose file to upload"] if k in node["inputs"]), None)
                if key:
                    node["inputs"][key] = input_image_path
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
    """轮询 ComfyUI 的历史接口，等待任务完成并通过 /view 接口获取图片"""
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
                    if output_type == "image":
                        images = []
                        for node_output in outputs.values():
                            # 跳过同时包含文字输出的节点（如 Display Any）
                            if "images" not in node_output:
                                continue
                            for img in node_output["images"]:
                                filename = img.get("filename", "")
                                subfolder = img.get("subfolder", "")
                                img_type = img.get("type", "output")
                                view_url = f"{server}/view"
                                params = {"filename": filename, "type": img_type}
                                if subfolder:
                                    params["subfolder"] = subfolder
                                img_resp = requests.get(view_url, params=params, timeout=15)
                                if img_resp.status_code == 200:
                                    temp_dir = BASE_DIR / "temp_images"
                                    temp_dir.mkdir(exist_ok=True)
                                    local_path = temp_dir / filename
                                    with open(local_path, 'wb') as f:
                                        f.write(img_resp.content)
                                    images.append(str(local_path))
                        return images
                    elif output_type == "text":
                        # 提取所有文字输出（增加 "output" 字段，适配 Display Any 节点）
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

# =====================================================================
# 帮助信息
# =====================================================================
ADMIN_HELP = r"""🔐 管理员说明
【管理指令】(仅管理员私聊)
  管理 → 进入管理菜单（群聊/私聊/其他设置）
  状态 → 查看系统运行状态
  过滤开/过滤关 → 控制提示词过滤
  快捷指令：
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
  尺寸触发词 → 切换尺寸
【后台管理】http://127.0.0.1:5000
【当前状态】ComfyUI: {comfyui_status} 排队: {queue_count} 任务"""

def send_help(qq, msg_type, gid=None):
    """发送帮助信息（管理员私聊显示管理版，普通用户显示公共说明）"""
    if msg_type == "private" and is_admin(qq):
        txt = ADMIN_HELP.format(comfyui_status="在线" if comfyui_online else "离线", queue_count=queue_counter)
    else:
        txt = get_config("public_help", "").strip() or "📋 帮助\n发送 说明 查看帮助"
    mid = send_msg(qq, gid, msg_type, txt)
    if mid and msg_type == "group":
        schedule_delete(mid, 15)  # 群聊帮助 15 秒后自动撤回

# =====================================================================
# 全局任务管理
# =====================================================================
task_queue = queue.Queue()      # 等待监控的任务队列
prompt_context = {}             # 任务上下文（prompt_id → 详细信息）
start_time_global = time.time() # 系统启动时间（用于状态查询）

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
    """检查群内用户的使用权限（是否在白名单，今日次数是否用完）"""
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
    if used >= max_count:
        return False, f"今日次数已用完 ({max_count}/天)"
    return True, None

def increment_usage(gid, qq):
    """增加群成员的每日使用次数计数"""
    today = datetime.date.today().isoformat()
    db = get_db()
    db.execute("INSERT INTO daily_usage VALUES (?,?,?,1) ON CONFLICT(qq, group_id, date) DO UPDATE SET count=count+1", (qq, gid, today))
    db.commit()
    db.close()

def has_image(event):
    """检查 QQ 消息事件中是否包含图片"""
    for segment in event.get("message", []):
        if segment.get("type") == "image":
            return True
    return "[CQ:image" in event.get("raw_message", "")

def handle_register(sqq, gid, mt, at_bot_prefix=None):
    """处理用户注册请求"""
    if is_blacklisted(gid, sqq):
        return True  # 黑名单静默忽略
    db = get_db()
    existing = db.execute("SELECT 1 FROM group_member_whitelist WHERE group_id=? AND qq=?", (gid, sqq)).fetchone()
    if existing:
        # 已注册 → 提示并 15 秒后撤回
        db.close()
        msg_id = send_msg(None, gid, mt, f"[CQ:at,qq={sqq}] 已在白名单")
        if msg_id:
            schedule_delete(msg_id, 15)
        return True
    # 新用户注册
    nickname = get_group_nickname(gid, sqq)
    db.execute("INSERT OR REPLACE INTO group_member_whitelist VALUES (?,?,?)", (gid, sqq, nickname))
    db.commit()
    db.close()
    logger.info(f"自助注册: 群{gid} QQ{sqq} ({nickname})")
    send_msg(None, gid, mt, f"[CQ:at,qq={sqq}] ✅ 注册成功，剩余{get_config('max_daily_count','20')}次（每日0点刷新）")
    return True

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
            parts = msg.split()
            qq = parts[1] if len(parts) > 1 else ""
            remark = " ".join(parts[2:]) if len(parts) > 2 else ""
            if qq.isdigit():
                db = get_db()
                db.execute("INSERT OR REPLACE INTO group_member_whitelist VALUES (?,?,?)", (gid, qq, remark))
                db.commit(); db.close()
                menu_send(sqq, f"✅ 已添加 {qq}", menu); return show_member_list(sqq, gid)
            else:
                menu_send(sqq, "QQ号格式错误", menu)
            return True
        if msg == "黑名单":
            return show_group_blacklist(sqq, gid)
        menu_send(sqq, "可用指令：拉黑 序号 / 移除 序号 / 重置 序号 / 全部重置 / 添加成员 QQ号 备注 / 黑名单 / 返回", menu)
        return True
    return True

def handle_private_menu(sqq, msg, menu):
    """私聊白名单管理子菜单"""
    if msg == "返回":
        menu["mode"] = "main"; menu["step"] = None; menu.pop("confirm", None)
        return menu_send(sqq, "📋 管理菜单\n1. 群聊管理\n2. 私聊管理\n3. 其他设置\n回复序号或 退出", menu)
    if msg.startswith("添加 "):
        parts = msg.split()
        qq = parts[1] if len(parts) > 1 else ""
        remark = " ".join(parts[2:]) if len(parts) > 2 else ""
        if qq.isdigit():
            db = get_db()
            db.execute("INSERT OR REPLACE INTO private_whitelist VALUES (?,?)", (qq, remark))
            db.commit(); db.close()
            menu_send(sqq, f"✅ 已添加 {qq}", menu); return show_private_list(sqq)
        else:
            menu_send(sqq, "QQ号格式错误", menu); return True
    if msg.startswith("移除 "):
        parts = msg.split()
        if len(parts) == 2 and parts[1].isdigit():
            idx = int(parts[1]) - 1
            db = get_db()
            members = db.execute("SELECT qq, remark FROM private_whitelist").fetchall()
            db.close()
            if 0 <= idx < len(members):
                target_qq = members[idx]["qq"]
                if menu.get("confirm") != f"remove_private_{target_qq}":
                    menu["confirm"] = f"remove_private_{target_qq}"
                    menu_send(sqq, f"⚠️ 确认移除 {target_qq}（{members[idx]['remark']}）？回复 确认移除 {target_qq}", menu)
                    return True
                else:
                    del menu["confirm"]
                    db = get_db()
                    db.execute("DELETE FROM private_whitelist WHERE qq=?", (target_qq,))
                    db.commit(); db.close()
                    menu_send(sqq, f"✅ 已移除 {target_qq}", menu); return show_private_list(sqq)
            else:
                menu_send(sqq, "序号无效", menu)
        else:
            menu_send(sqq, "格式：移除 序号", menu)
        return True
    return show_private_list(sqq)

def handle_other_menu(sqq, msg, menu):
    """其他设置子菜单（过滤开关、过滤词、每日上限、公共说明）"""
    step = menu.get("step", "main")
    if step == "main":
        if msg == "返回":
            menu["mode"] = "main"; menu["step"] = None; menu.pop("other_confirm", None)
            return menu_send(sqq, "📋 管理菜单\n1. 群聊管理\n2. 私聊管理\n3. 其他设置\n回复序号或 退出", menu)
        if msg == "1":
            current = get_config("filter_enabled") == "true"
            menu["step"] = "filter_toggle"
            menu_send(sqq, f"⚠️ 过滤当前为「{'开启' if current else '关闭'}」，回复 开 或 关 切换，回复 返回 取消", menu)
            return True
        if msg == "2":
            words = get_config("filter_words", "")
            word_list = [w.strip() for w in words.split(",") if w.strip()]
            menu["step"] = "filter_words"
            menu_send(sqq, f"📋 当前过滤词（{len(word_list)}个）: {', '.join(word_list) if word_list else '无'}\n回复：添加 xxx / 删除 xxx / 返回", menu)
            return True
        if msg == "3":
            current_limit = get_config("max_daily_count", "20")
            menu["step"] = "daily_limit"
            menu_send(sqq, f"⚠️ 当前每日上限: {current_limit} 次\n回复 数字 修改，回复 返回 取消", menu)
            return True
        if msg == "4":
            current_help = get_config("public_help", "")
            menu["step"] = "public_help"
            preview = current_help[:200] + ("..." if len(current_help) > 200 else "")
            menu_send(sqq, f"📋 当前公共说明:\n{preview}\n\n回复 新文本 替换，回复 返回 取消", menu)
            return True
        return show_other_menu(sqq)

    # 过滤开关子步骤
    if step == "filter_toggle":
        if msg == "返回":
            menu["step"] = "main"; return show_other_menu(sqq)
        if msg in ("开", "开启", "on", "true"):
            set_config("filter_enabled", "true")
            menu_send(sqq, "✅ 过滤已开启", menu)
            menu["step"] = "main"; return show_other_menu(sqq)
        if msg in ("关", "关闭", "off", "false"):
            set_config("filter_enabled", "false")
            menu_send(sqq, "✅ 过滤已关闭", menu)
            menu["step"] = "main"; return show_other_menu(sqq)
        menu_send(sqq, "请回复 开 或 关，或 返回", menu); return True

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

    return show_other_menu(sqq)

def show_other_menu(sqq):
    """显示其他设置菜单"""
    current_filter = "开启" if get_config("filter_enabled") == "true" else "关闭"
    words = get_config("filter_words", "")
    word_count = len([w for w in words.split(",") if w.strip()])
    limit = get_config("max_daily_count", "20")
    menu = admin_menu.get(sqq)
    lines = ["📋 其他设置", f"当前过滤: {current_filter} | 上限: {limit}次/天",
             "1. 过滤开关", f"2. 过滤词管理 (共{word_count}个)", "3. 每日上限", "4. 公共说明",
             "回复: 序号选择 / 返回"]
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
    return menu_send(sqq, "\n".join(lines), admin_menu.get(sqq))

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
# 核心消息处理
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

    # 群聊中未 @机器人 则忽略
    if msg_type == "group" and not is_at_bot:
        return

    # ── 管理员私聊指令 ──
    if msg_type == "private" and is_admin(sender_qq):
        if handle_quick_commands(sender_qq, msg):
            return
        if msg == "状态":
            # 计算运行时长
            uptime_seconds = int(time.time() - start_time_global)
            days, rem = divmod(uptime_seconds, 86400)
            hours, rem2 = divmod(rem, 3600)
            mins, _ = divmod(rem2, 60)
            uptime_str = f"{days}天{hours}小时{mins}分" if days > 0 else f"{hours}小时{mins}分"

            db = get_db()
            today = datetime.date.today().isoformat()

            # 统计数据
            today_imgs = db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='image' AND status='成功'", (today,)).fetchone()["c"]
            today_texts = db.execute("SELECT COUNT(*) as c FROM history WHERE date(time)=? AND output_type='text' AND status='成功'", (today,)).fetchone()["c"]
            today_users = db.execute("SELECT COUNT(DISTINCT qq) as c FROM history WHERE date(time)=?", (today,)).fetchone()["c"]

            # 系统规模数据
            total_registered = db.execute("SELECT COUNT(*) as c FROM group_member_whitelist").fetchone()["c"]
            total_blacklisted = db.execute("SELECT COUNT(*) as c FROM group_blacklist").fetchone()["c"]
            total_groups = db.execute("SELECT COUNT(*) as c FROM group_whitelist").fetchone()["c"]
            db.close()

            max_count = int(get_config("max_daily_count", "20"))
            filter_status = "开启" if get_config("filter_enabled") == "true" else "关闭"

            status_text = (
                f"📊 ArtLink 状态\n"
                f"ComfyUI: {'在线 ✅' if comfyui_online else '离线 ❌'}\n"
                f"排队任务: {queue_counter}\n"
                f"今日生图: {today_imgs}\n"
                f"今日反推: {today_texts}\n"
                f"活跃用户: {today_users}\n"
                f"每日上限: {max_count}次（0点刷新）\n"
                f"过滤状态: {filter_status}\n"
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
        db = get_db()
        row = db.execute("SELECT 1 FROM private_whitelist WHERE qq=?", (sender_qq,)).fetchone()
        db.close()
        if not row:
            # 私聊无权限静默期：30 秒内不重复发提示
            with warn_lock:
                last_warn = private_warned.get(sender_qq, 0)
                now = time.time()
                if now - last_warn < WARN_INTERVAL:
                    return  # 静默
                private_warned[sender_qq] = now
            send_msg(sender_qq, None, "private", "⚠️ 无权限")
            return
    else:
        # 群聊：检查群白名单
        db = get_db()
        row = db.execute("SELECT 1 FROM group_whitelist WHERE group_id=?", (group_id,)).fetchone()
        db.close()
        if not row:
            send_msg(None, group_id, "group", "⚠️ 该群未授权")
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
            # 未注册提醒（15 秒后撤回）
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

    # ── 尺寸触发词（已支持空格可有可无）──
    size_presets = load_size_presets()
    for sp in size_presets:
        trigger_word = sp["trigger_word"]
        # 改为 startswith(trigger_word)，不再强制要求空格
        if trigger_word and msg.startswith(trigger_word):
            user_key = f"group_{group_id}_{sender_qq}" if msg_type == "group" else f"private_{sender_qq}"
            save_user_size(user_key, sp["width"], sp["height"])
            if msg == trigger_word:
                tip = f"已切换为{sp['name']}({sp['width']}×{sp['height']})"
                mid = send_msg(sender_qq, group_id, msg_type, tip if msg_type == "private" else f"[CQ:at,qq={sender_qq}] {tip}")
                if mid and msg_type == "group":
                    schedule_delete(mid, 15)
                return
            # 提取剩余部分，跳过触发词本身
            msg = msg[len(trigger_word):].strip()
            break

    # ── 生图/反推触发词匹配（已支持空格可有可无）──
    db = get_db()
    triggers = db.execute("SELECT word, workflow FROM trigger_words").fetchall()
    db.close()
    # 按触发词长度从长到短排序，避免短词误匹配
    trigger_list = [{"word": r["word"], "workflow": r["workflow"]} for r in triggers]
    trigger_list.sort(key=lambda x: len(x["word"]), reverse=True)
    matched = None
    for row in trigger_list:
        # 改为 startswith(row["word"])，不再强制要求空格
        if msg == row["word"] or msg.startswith(row["word"]):
            matched = (row["word"], row["workflow"])
            break
    if not matched:
        return

    prompt_text = msg[len(matched[0]):].strip()
    workflow, wf_config = load_workflow_json(matched[1])
    if workflow is None:
        return

    output_type = str(wf_config.get("output_type", "image")).strip()
    has_positive = bool(str(wf_config.get("positive_node_title", "")).strip())
    has_latent = bool(str(wf_config.get("latent_node_title", "")).strip())
    has_image_node = bool(str(wf_config.get("image_node_title", "")).strip())

    # ── 图片下载 ──
    img_filename = None
    if matched[0] == "反推" and has_image(event):
        img_filename = download_qq_image(event)
        if not img_filename:
            send_msg(sender_qq, group_id, msg_type, "❌ 图片下载失败，请稍后重试")
            return
        prompt_text = ""
    elif has_image_node and has_image(event):
        img_filename = download_qq_image(event)
        if not img_filename:
            send_msg(sender_qq, group_id, msg_type, "❌ 图片下载失败，请稍后重试")
            return

    # ── 提示词检查 ──
    if output_type != "text" and has_positive and not prompt_text:
        example = str(wf_config.get("example", "")).strip()
        tip = f"需要提示词，如：{matched[0]} {example}" if example else "需要提示词"
        send_msg(sender_qq, group_id, msg_type, tip if msg_type == "private" else f"[CQ:at,qq={sender_qq}] {tip}")
        return

    # ── 提示词过滤 ──
    if get_config("filter_enabled") == "true":
        filter_words = get_config("filter_words", "")
        if filter_words:
            words_to_remove = [w.strip() for w in filter_words.split(",") if w.strip()]
            for word in words_to_remove:
                # 使用 \b 单词边界精确匹配，不误伤其他词
                prompt_text = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_text, flags=re.IGNORECASE)
            # 清理多余逗号和空格
            prompt_text = re.sub(r',\s*,', ',', prompt_text).strip(', ')

    # ── 群聊生图次数检查（反推不消耗次数） ──
    if msg_type == "group" and output_type == "image":
        allowed, reason = check_group_permission(group_id, sender_qq)
        if not allowed:
            send_msg(None, group_id, "group", f"[CQ:at,qq={sender_qq}] {reason}")
            return
        increment_usage(group_id, sender_qq)

    # ── ComfyUI 在线检查 ──
    if not check_comfyui_online():
        send_msg(sender_qq, group_id, msg_type, "⚠️ ComfyUI 离线")
        return

    # ── 获取用户尺寸记忆 ──
    width, height = (None, None)
    if has_latent:
        user_key = f"group_{group_id}_{sender_qq}" if msg_type == "group" else f"private_{sender_qq}"
        width, height = get_user_size(user_key)

    # ── 排队计数 ──
    with queue_lock:
        queue_counter += 1
        position = queue_counter

    # ── 提交任务 ──
    start_time = time.time()
    workflow = modify_workflow(workflow, wf_config, prompt_text if has_positive else None, width, height, img_filename)
    prompt_id = submit_to_comfyui(workflow)
    if not prompt_id:
        with queue_lock:
            queue_counter -= 1
        send_msg(sender_qq, group_id, msg_type, "❌ 提交失败")
        return

    # ── 记录尺寸名称 ──
    size_name = ""
    if has_latent and width and height:
        for sp in size_presets:
            if sp["width"] == width and sp["height"] == height:
                size_name = sp["name"]
                break
        if not size_name:
            size_name = f"{width}x{height}"

    # ── 记录任务上下文 ──
    prompt_context[prompt_id] = {
        "message_type": msg_type,
        "group_id": group_id,
        "sender_qq": sender_qq,
        "prompt_text": prompt_text,
        "size_name": size_name,
        "output_type": output_type,
        "workflow_name": matched[1],
        "retry": 0,
        "start_time": start_time
    }

    # ── 发送提交回复 ──
    tag = "📝" if output_type == "text" else "🎨"
    if msg_type == "group":
        db = get_db()
        today = datetime.date.today().isoformat()
        used_row = db.execute("SELECT count FROM daily_usage WHERE qq=? AND group_id=? AND date=?", (sender_qq, group_id, today)).fetchone()
        db.close()
        used = used_row["count"] if used_row else 0
        max_count = int(get_config("max_daily_count", "20"))
        remaining = max_count - used
        reply = f"[CQ:at,qq={sender_qq}] {tag} 已提交 [{matched[1]}] 排队{position-1}人 · 剩余{remaining}次（每日0点刷新）"
    else:
        reply = f"{tag} 已提交 [{matched[1]}] 排队{position-1}人"
    msg_id = send_msg(sender_qq, group_id, msg_type, reply)
    if msg_id:
        schedule_delete(msg_id, 15)

    # 记录历史（进行中状态）
    add_history(sender_qq, group_id, matched[1], output_type, prompt_text, size_name, "进行中")
    task_queue.put(prompt_id)

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

                # ── 生成失败，尝试重试 ──
                if not result:
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
                    # 重试失败
                    send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], "❌ 失败")
                    add_history(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""),
                               ctx.get("output_type", "image"), ctx.get("prompt_text", ""),
                               ctx.get("size_name", ""), "失败", duration)
                    continue

                # ── 图片输出 ──
                sent_ids = []
                total = len(result) if ctx.get("output_type") == "image" else 1

                if ctx.get("output_type") == "image":
                    if ctx["message_type"] == "private":
                        # 私聊：完整信息 + 多图序号
                        for idx, img_path in enumerate(result):
                            abs_path = str(Path(img_path).resolve()).replace("\\", "/")
                            if total == 1:
                                cap = f"✅ 完成 · {duration:.0f}秒"
                                if ctx.get("prompt_text"):
                                    cap += f"\n{ctx['prompt_text']}"
                                if ctx.get("size_name"):
                                    cap += f" · {ctx['size_name']}"
                                text = f"{cap}\n[CQ:image,file=file:///{abs_path}]"
                            else:
                                if idx == 0:
                                    cap = f"✨ 任务完成 · {duration:.0f}秒 (第1/{total}张)"
                                else:
                                    cap = f"(第{idx+1}/{total}张)"
                                text = f"{cap}\n[CQ:image,file=file:///{abs_path}]"
                            mid = send_msg(ctx["sender_qq"], None, "private", text)
                            if mid:
                                sent_ids.append(mid)
                            if idx < total - 1:
                                time.sleep(1)  # 发送间隔 1 秒
                    else:
                        # 群聊：神秘返图 + 多图序号
                        for idx, img_path in enumerate(result):
                            abs_path = str(Path(img_path).resolve()).replace("\\", "/")
                            if total == 1:
                                text = f"✨ 任务完成 · {duration:.0f}秒\n[CQ:image,file=file:///{abs_path}]"
                            else:
                                if idx == 0:
                                    text = f"✨ 任务完成 · {duration:.0f}秒 (第1/{total}张)\n[CQ:image,file=file:///{abs_path}]"
                                else:
                                    text = f"(第{idx+1}/{total}张)\n[CQ:image,file=file:///{abs_path}]"
                            mid = send_msg(None, ctx["group_id"], "group", text)
                            if mid:
                                sent_ids.append(mid)
                            if idx < total - 1:
                                time.sleep(1)

                    add_history(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""),
                               "image", ctx.get("prompt_text", ""), ctx.get("size_name", ""), "成功", duration)

                # ── 文字输出 ──
                else:
                    text = "\n".join(result) if result else "(无输出)"
                    msg_text = f"📝 {ctx.get('workflow_name','')} · {duration:.0f}秒\n{text}"
                    mid = send_msg(ctx["sender_qq"], ctx.get("group_id"), ctx["message_type"], msg_text)
                    if mid:
                        sent_ids.append(mid)
                    add_history(ctx["sender_qq"], ctx.get("group_id"), ctx.get("workflow_name", ""),
                               "text", ctx.get("prompt_text", ""), ctx.get("size_name", ""), "成功", duration)

                # 记录撤回信息
                if sent_ids:
                    add_recent_task(ctx.get("group_id"), ctx["sender_qq"], sent_ids)

                # 定期清理，防止内存泄漏
                if len(prompt_context) > 500:
                    prompt_context.clear()

            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"监控异常: {e}")

    def stop(self):
        self.running = False

# =====================================================================
# 后台线程
# =====================================================================
def comfyui_health_check():
    """每 30 秒检查一次 ComfyUI 是否在线"""
    while True:
        time.sleep(30)
        check_comfyui_online()

def menu_timeout_check():
    """检查管理菜单超时（30 秒无操作自动退出）"""
    while True:
        time.sleep(10)
        now = time.time()
        for sqq in list(admin_menu.keys()):
            menu = admin_menu.get(sqq)
            if menu and now - menu.get("last_active", 0) > 30:
                last = menu.get("last_msg_id")
                if last:
                    delete_msg(last)
                del admin_menu[sqq]
                send_msg(sqq, None, "private", "⏰ 菜单已超时退出")

# =====================================================================
# WebSocket 连接
# =====================================================================
def on_message(ws, msg):
    """收到 WebSocket 消息时在新线程中处理"""
    try:
        event = json.loads(msg)
        threading.Thread(target=handle_message, args=(event,), daemon=True).start()
    except Exception as e:
        logger.error(f"WS消息解析异常: {e}")

def ws_connect():
    """WebSocket 连接主循环，断线自动重连"""
    while True:
        try:
            ws = websocket.WebSocketApp(
                NAPCAT_WS,
                on_message=on_message,
                on_error=lambda w, e: logger.error(f"WS错误: {e}"),
                on_close=lambda w, c, m: logger.warning(f"WS断开: {c}"),
                on_open=lambda w: logger.info("WebSocket 已连接")
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.error(f"WS异常: {e}")
        time.sleep(10)

# =====================================================================
# 主入口
# =====================================================================
def main():
    print("=" * 50)
    print("  ArtLink v1.0.1 - 触发词优化 & 下载重试")
    print("=" * 50)

    init_db()
    init_config()

    # 自动创建 workflows 工作流文件夹
    workflows_dir = BASE_DIR / "workflows"
    if not workflows_dir.exists():
        workflows_dir.mkdir(parents=True, exist_ok=True)
        logger.info("已创建默认工作流文件夹: workflows/")

    # 确保 ComfyUI 输入目录存在
    input_dir = Path(get_config("comfyui_input_dir", str(COMFYUI_INPUT)))
    input_dir.mkdir(parents=True, exist_ok=True)
    logger.info("初始化完成")

    # 启动各后台线程
    monitor = ResultMonitor()
    threading.Thread(target=monitor.run, daemon=True).start()          # 结果监控
    threading.Thread(target=ws_connect, daemon=True).start()           # WebSocket
    threading.Thread(target=comfyui_health_check, daemon=True).start() # ComfyUI 健康检查
    threading.Thread(target=menu_timeout_check, daemon=True).start()   # 管理菜单超时检查

    logger.info(f"管理后台: http://{APP_HOST}:{APP_PORT}")
    print(f"\n  管理后台: http://{APP_HOST}:{APP_PORT}")
    print("  按 Ctrl+C 停止\n")

    # 启动 Flask Web 服务
    try:
        app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        monitor.stop()
        print("已停止")

if __name__ == "__main__":
    os.chdir(str(BASE_DIR))
    main()