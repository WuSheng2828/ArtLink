#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ArtLink 词库可视化编辑器 v3.1
==============================
端口：19876
功能：词条管理（tags）、集中添加、标签码管理、标签关系（独立开关）、随机设置
保存：wordbank.json + wordbank_filter_config.json
"""

import json, time, shutil
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
WORDANK_PATH = BASE_DIR / "wordbank.json"
BACKUP_DIR = BASE_DIR / "wordbank_backups"
CONFIG_PATH = BASE_DIR / "wordbank_filter_config.json"

def load_wordbank():
    if not WORDANK_PATH.exists(): return {"version":"2.0","categories":[]}
    try:
        with open(WORDANK_PATH,'r',encoding='utf-8') as f: return json.load(f)
    except: return {"version":"2.0","categories":[]}

def save_wordbank(data):
    BACKUP_DIR.mkdir(exist_ok=True)
    if WORDANK_PATH.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WORDANK_PATH, BACKUP_DIR / f"wordbank_{ts}.json")
        for old in sorted(BACKUP_DIR.glob("wordbank_*.json"))[:-10]: old.unlink()
    with open(WORDANK_PATH,'w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False,indent=2)

_DEFAULT_TAGS = [
    "sfw","nsfw","女性","男性","非二元","青少年","成人","成熟",
    "人类","狐娘","猫娘","兔娘","其他兽娘","精灵","恶魔","天使","机械","怪物","非人形",
    "小胸","中胸","大胸","肌肉","兽耳","狐耳","猫耳","兔耳","犬耳","特殊耳","精灵耳","尾巴","翅膀","角",
    "胸部","长发","短发","全身","上半身","特写","单人","生殖器","化妆",
    "服装","上装","下装","配饰","鞋子","赤脚","袜子","内衣","泳装","暴露","全裸",
    "性暗示","性行为","自慰","体液","姿势","暴露姿势","表情","脸红",
    "室内","室外","自然","城市","幻想","私密空间","公共场所","白天","夜晚",
    "可爱","性感","端庄","写实","二次元","插画","水墨","胶片","Q版"
]

def load_config():
    defaults = {
        "category_order":True,"category_order_list":list(range(1,37)),
        "tag_relations_enabled":True,"tag_relations":[],"all_tags":_DEFAULT_TAGS
    }
    if not CONFIG_PATH.exists(): return defaults
    try:
        with open(CONFIG_PATH,'r',encoding='utf-8') as f: data=json.load(f)
        for k in defaults:
            if k not in data: data[k]=defaults[k]
        if not data.get("all_tags"): data["all_tags"]=_DEFAULT_TAGS
        for rel in data.get("tag_relations",[]):
            if "enabled" not in rel: rel["enabled"]=True
        return data
    except: return defaults

def save_config(data):
    with open(CONFIG_PATH,'w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False,indent=2)
EDITOR_HTML = r'''<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ArtLink 词库编辑器 v3.1</title>
<style>
:root{
    --bg:#f5f6fa;--card:#fff;--text:#1e293b;--muted:#94a3b8;
    --blue:#4f8cf7;--blue-h:#3b7ae3;--green:#22c55e;--green-h:#16a34a;
    --red:#ef4444;--red-h:#dc2626;--amber:#f59e0b;--amber-h:#d97706;
    --border:#e2e8f0;--shadow:0 1px 3px rgba(0,0,0,.05);--radius:10px
}
*,*::before,*::after{box-sizing:border-box}
body{
    font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
    margin:0;padding:20px 24px;background:var(--bg);color:var(--text);line-height:1.5
}
.container{max-width:1300px;margin:0 auto}
h1{font-size:1.4em;font-weight:700;margin:0 0 2px 0;color:var(--text)}
.subtitle{font-size:.9em;color:var(--muted);margin:0 0 16px 0}

.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px;padding:8px 14px;background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow)}
.toolbar .btn{padding:7px 16px;cursor:pointer;background:var(--blue);color:#fff;border:none;border-radius:7px;font-size:.9em;font-weight:500;transition:all .15s;white-space:nowrap}
.toolbar .btn:hover{background:var(--blue-h);transform:translateY(-1px)}
.toolbar .btn.accent{background:var(--green)}.toolbar .btn.accent:hover{background:var(--green-h)}
.toolbar .btn.danger{background:var(--red)}.toolbar .btn.danger:hover{background:var(--red-h)}
.toolbar .btn.warn{background:var(--amber)}.toolbar .btn.warn:hover{background:var(--amber-h)}
.toolbar .info{color:var(--muted);font-size:.85em;margin-left:auto}
.toolbar .font-slider{display:flex;align-items:center;gap:4px;font-size:.8em;color:var(--muted);margin-left:8px}
.toolbar .font-slider input[type=range]{width:80px;height:4px;cursor:pointer;accent-color:var(--blue)}

.tab-bar{display:flex;gap:2px;margin:0 0 14px 0;border-bottom:2px solid var(--border);overflow-x:auto}
.tab-btn{flex-shrink:0;padding:10px 18px;cursor:pointer;background:transparent;border:none;border-bottom:2px solid transparent;margin-bottom:-2px;font-size:.9em;font-weight:500;color:var(--muted);transition:all .2s;white-space:nowrap;border-radius:8px 8px 0 0}
.tab-btn:hover{color:var(--text);background:rgba(79,140,247,.05)}
.tab-btn.active{color:var(--blue);border-bottom-color:var(--blue);background:rgba(79,140,247,.06);font-weight:600}
.tab-content{display:none;animation:fadeIn .2s ease}
.tab-content.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

.section{background:var(--card);border-radius:var(--radius);padding:14px 16px;margin:0 0 10px 0;box-shadow:var(--shadow)}
.section h3{font-size:1em;font-weight:600;margin:0 0 8px 0;color:var(--text);padding-bottom:6px;border-bottom:2px solid var(--border)}
.btn{padding:6px 14px;cursor:pointer;background:var(--blue);color:#fff;border:none;border-radius:6px;font-size:.85em;font-weight:500;transition:all .15s;white-space:nowrap}
.btn:hover{opacity:.9;transform:translateY(-1px)}
.btn.accent{background:var(--green)}.btn.danger{background:var(--red)}.btn.warn{background:var(--amber)}
.btn.small{padding:4px 10px;font-size:.8em}.btn.xs{padding:3px 8px;font-size:.75em}
.btn.ghost{background:transparent;color:var(--blue);border:1px solid var(--border)}.btn.ghost:hover{background:#f8fafc}
input,select,textarea{padding:7px 10px;border:1px solid var(--border);border-radius:7px;font-size:.9em;background:var(--card);color:var(--text);font-family:inherit;transition:border-color .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(79,140,247,.12)}
select{min-width:60px}

.add-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 12px;background:#f8fafc;border-radius:var(--radius);margin-bottom:14px;border:1px dashed var(--border)}
.tag-chips{display:flex;flex-wrap:wrap;gap:4px;max-width:420px}
.tag-chip{display:inline-flex;align-items:center;gap:3px;padding:3px 9px;border-radius:14px;font-size:.8em;cursor:pointer;border:1px solid var(--border);transition:all .12s;user-select:none;white-space:nowrap}
.tag-chip:hover{border-color:var(--blue)}
.tag-chip.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.tag-chip .rm-btn{cursor:pointer;font-size:13px;line-height:1;opacity:.6;margin-left:2px}

.cat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:10px}
.cat-card{background:var(--card);border-radius:var(--radius);padding:10px 12px;box-shadow:var(--shadow);cursor:grab;transition:box-shadow .15s}
.cat-card:hover{box-shadow:0 2px 8px rgba(0,0,0,.08)}
.cat-card.dragging{opacity:.4;border:2px dashed var(--blue)}
.cat-card.drag-over{border-top:3px solid var(--blue)}
.cat-card .cat-head{display:flex;align-items:center;gap:6px;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--border);flex-wrap:wrap}
.cat-card .cat-hid{font-size:.75em;color:var(--muted);background:#f1f5f9;padding:2px 7px;border-radius:8px;font-weight:600}
.cat-card .cat-hname{font-size:.9em;font-weight:600;cursor:pointer;padding:1px 4px;border-radius:4px}
.cat-card .cat-hname:hover{background:#eef2ff}
.cat-card .cat-hname-input{font-size:.9em;font-weight:600;padding:1px 4px;border:1px solid var(--blue);border-radius:4px;outline:none;width:120px}
.cat-card .cat-hcnt{font-size:.75em;color:var(--muted);margin-left:auto}
.cat-card .word-grid{display:flex;flex-wrap:wrap;gap:5px;min-height:28px;padding:3px;border:1px dashed transparent;border-radius:6px;transition:all .15s;font-size:.85em}
.cat-card .word-grid.drag-over{border-color:var(--blue);background:#eef2ff}
.word-tag{display:inline-flex;align-items:center;gap:3px;padding:3px 8px;border-radius:5px;font-size:.85em;cursor:grab;transition:all .12s;user-select:none;white-space:nowrap}
.word-tag.sfw{background:#dcfce7;color:#166534;border:1px solid #bbf7d0}
.word-tag.nsfw{background:#fee2e2;color:#991b1b;border:1px solid #fecaca}
.word-tag:hover{transform:translateY(-1px);box-shadow:0 1px 3px rgba(0,0,0,.08)}
.word-tag.dragging{opacity:.4}
.word-tag .zh-text{font-size:.8em;color:inherit;opacity:.7;margin-left:2px}
.word-tag .tag-dots{font-size:.7em;color:inherit;opacity:.5}
.word-tag .del-btn,.word-tag .edit-btn{cursor:pointer;color:inherit;opacity:.4;background:none;border:none;padding:0}
.word-tag .del-btn{font-size:13px;margin-left:2px}.word-tag .edit-btn{font-size:11px}
.word-tag .del-btn:hover,.word-tag .edit-btn:hover{opacity:1}

.rel-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:6px;margin:8px 0}
.rel-card{display:flex;align-items:center;gap:6px;padding:7px 10px;background:#f8fafc;border-radius:8px;border:1px solid var(--border);font-size:.85em;transition:all .12s}
.rel-card:hover{border-color:#cbd5e1}
.rel-card .rel-tag{font-weight:500;min-width:40px;text-align:center}
.rel-card .rel-icon{font-size:15px;flex-shrink:0}
.rel-card .rel-badge{font-size:.75em;padding:2px 7px;border-radius:10px;font-weight:600;flex-shrink:0}
.rel-card .rel-badge.oppose{background:#fee2e2;color:#991b1b}
.rel-card .rel-badge.attract{background:#dcfce7;color:#166534}
.rel-card .rel-strength{font-size:.75em;color:var(--muted);flex-shrink:0}
.rel-card .rel-actions{margin-left:auto;display:flex;gap:3px;flex-shrink:0}
.rel-card .rel-switch{flex-shrink:0}

.switch{width:36px;height:18px;position:relative;flex-shrink:0;display:inline-block}
.switch input{opacity:0;width:0;height:0}
.switch .slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#cbd5e1;transition:.3s;border-radius:18px}
.switch .slider::before{content:"";position:absolute;height:14px;width:14px;left:2px;bottom:2px;background:#fff;transition:.3s;border-radius:50%}
.switch input:checked+.slider{background:var(--green)}
.switch input:checked+.slider::before{transform:translateX(18px)}
.switch-wrap{display:inline-flex;align-items:center;gap:6px;font-size:.9em}

.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(15em,1fr));gap:6px}
.pick-item{display:flex;align-items:center;gap:5px;padding:6px 10px;background:#f8fafc;border-radius:7px;border:1px solid var(--border);font-size:.85em;min-width:0}
.pick-item .pick-name{font-weight:500;flex:1;min-width:0;word-break:break-all;overflow-wrap:break-word}
.pick-item .pick-id{font-size:.75em;color:var(--muted);flex-shrink:0}
.pick-item .pick-input{width:3em;min-width:2.5em;padding:3px 2px;border:1px solid var(--border);border-radius:4px;font-size:.85em;text-align:center;background:#fff;flex-shrink:0}
.pick-item .pick-input:focus{outline:none;border-color:var(--blue)}
.pick-item .pick-sep{flex-shrink:0;color:var(--muted);font-size:.8em}
.pick-item .pick-cnt{font-size:.75em;color:var(--muted);flex-shrink:0;white-space:nowrap}

.sort-grid{display:flex;flex-wrap:wrap;gap:6px;padding:10px;background:#f8fafc;border-radius:var(--radius);min-height:40px}
.sort-tag{display:inline-flex;align-items:center;gap:4px;padding:5px 9px;border-radius:7px;font-size:.8em;background:#fff;border:1px solid var(--border);cursor:grab;user-select:none;white-space:nowrap;transition:all .12s}
.sort-tag:hover{border-color:var(--blue);box-shadow:0 1px 4px rgba(79,140,247,.15)}
.sort-tag.dragging{opacity:.35;border:2px dashed var(--blue)}
.sort-tag.drag-over{box-shadow:0 0 0 2px var(--blue);transform:scale(1.04)}
.sort-tag .sort-idx{font-size:.7em;color:#fff;background:var(--blue);min-width:16px;height:16px;border-radius:8px;display:inline-flex;align-items:center;justify-content:center;font-weight:700}

.float-pool{position:fixed;bottom:20px;right:20px;width:260px;max-height:360px;background:var(--card);border-radius:var(--radius);box-shadow:0 4px 20px rgba(0,0,0,.12);z-index:9999;display:flex;flex-direction:column;overflow:hidden;border:2px dashed var(--blue)}
.float-pool.minimized{height:40px;overflow:hidden}
.pool-header{display:flex;align-items:center;gap:6px;padding:6px 10px;background:var(--blue);color:#fff;cursor:pointer;flex-shrink:0;font-size:.8em;font-weight:500}
.pool-body{padding:6px;overflow-y:auto;flex:1;min-height:50px;display:flex;flex-wrap:wrap;gap:4px;align-content:flex-start}
.pool-body.drag-over{background:#eef2ff}
.pool-body .word-tag{cursor:grab}

.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.35);z-index:10000;justify-content:center;align-items:center}
.modal-overlay.active{display:flex}
.modal-box{background:var(--card);border-radius:var(--radius);padding:18px;width:420px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,.15)}
.modal-box h3{font-size:1em;margin:0 0 10px 0}
.modal-box .field{margin-bottom:8px}
.modal-box .field label{display:block;font-size:.8em;color:var(--muted);margin-bottom:2px}
.modal-box .field input,.modal-box .field select{width:100%;padding:7px 10px}
.modal-box .modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}

.toast{position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:99999;padding:9px 20px;border-radius:8px;font-size:.9em;color:#fff;box-shadow:0 4px 16px rgba(0,0,0,.2);animation:toastIn .25s ease}
.toast.success{background:var(--green)}.toast.error{background:var(--red)}.toast.info{background:var(--blue)}
@keyframes toastIn{from{opacity:0;transform:translateX(-50%) translateY(-16px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}

@media(max-width:700px){
    body{padding:10px}
    .toolbar{flex-direction:column;align-items:stretch}
    .add-bar{flex-direction:column;align-items:stretch}
    .cat-grid,.rel-grid{grid-template-columns:1fr}
    .float-pool{width:220px;bottom:10px;right:10px}
}
</style></head>
<body>
<div class="container">
<h1>&#128221; ArtLink 词库编辑器 v3.1</h1>
<p class="subtitle">独立运行 · 端口 19876</p>

<div class="toolbar">
    <button class="btn" onclick="addCategory()">+ 添加分类</button>
    <button class="btn accent" onclick="saveAll()">&#128190; 保存全部</button>
    <button class="btn warn" onclick="refreshFromFile()">&#128259; 重新加载</button>
    <button class="btn" onclick="reloadMainCache()">&#128260; 刷新缓存</button>
    <span class="info" id="statusInfo">就绪</span>
    <div class="font-slider">
        <span>A-</span>
        <input type="range" id="fontSize" min="12" max="22" value="14" step="1" oninput="changeFontSize(this.value)">
        <span>A+</span>
    </div>
</div>

<div class="tab-bar">
    <div class="tab-btn active" data-tab="tab_words" onclick="switchTab('tab_words',this)">&#128221; 词库管理</div>
    <div class="tab-btn" data-tab="tab_rules" onclick="switchTab('tab_rules',this)">&#128279; 规则管理</div>
    <div class="tab-btn" data-tab="tab_random" onclick="switchTab('tab_random',this)">&#127922; 随机设置</div>
</div>

<div id="tab_words" class="tab-content active">
    <div class="add-bar">
        <span style="font-weight:600;font-size:.9em">&#10133; 添加词条</span>
        <select id="addCatId" style="min-width:120px"><option value="">-- 选择分类 --</option></select>
        <input type="text" id="addText" placeholder="英文标签" size="16">
        <input type="text" id="addZh" placeholder="中文(可选)" size="9">
        <div class="tag-chips" id="addTagChips"></div>
        <input type="text" id="addNewTag" placeholder="新标签码" size="9" style="width:85px">
        <button class="btn accent small" onclick="addWordGlobal()">添加</button>
    </div>
    <div class="cat-grid" id="categoryList"></div>
</div>

<div id="tab_rules" class="tab-content">
    <div class="section">
        <h3>&#128295; 规则开关</h3>
        <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
            <div class="switch-wrap"><span>分类顺序排列</span><label class="switch"><input type="checkbox" id="swCatOrder" onchange="toggleCfg('category_order')" checked><span class="slider"></span></label></div>
            <div class="switch-wrap"><span>标签关系引擎</span><label class="switch"><input type="checkbox" id="swTagRel" onchange="toggleCfg('tag_relations_enabled')" checked><span class="slider"></span></label></div>
        </div>
    </div>
    <div class="section">
        <h3>&#127991;&#65039; 标签码管理</h3>
        <div style="display:flex;gap:6px;margin-bottom:8px">
            <input type="text" id="newTagCode" placeholder="新标签码" size="14" style="width:140px">
            <button class="btn small" onclick="addTagCode()">&#10133; 添加</button>
        </div>
        <div class="tag-chips" id="tagCodeList" style="max-width:100%"></div>
    </div>
    <div class="section">
        <h3>&#128279; 标签关系（每行可独立开关）</h3>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:10px;padding:8px;background:#f8fafc;border-radius:8px">
            <select id="relTagA" style="min-width:80px"><option value="">标签A</option></select>
            <select id="relType" style="min-width:60px"><option value="对立">&#128683;对立</option><option value="吸引">&#129309;吸引</option></select>
            <select id="relTagB" style="min-width:80px"><option value="">标签B</option></select>
            <select id="relStrength" style="min-width:55px"><option value="弱">弱</option><option value="中" selected>中</option><option value="强">强</option></select>
            <button class="btn small" onclick="addRelation()">&#10133; 添加</button>
        </div>
        <div class="rel-grid" id="relGrid"></div>
    </div>
    <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn accent" onclick="saveRules()">&#128190; 保存全部规则</button>
        <button class="btn" onclick="loadRules()">&#128259; 重新加载</button>
    </div>
</div>

<div id="tab_random" class="tab-content">
    <div class="section">
        <h3>&#128202; 取词数量</h3>
        <div class="pick-grid" id="pickGrid"></div>
    </div>
    <div class="section">
        <h3>&#128260; 标签排列顺序</h3>
        <button class="btn small ghost" onclick="resetSortOrder()" style="margin-bottom:6px">&#128260; 重置默认</button>
        <div class="sort-grid" id="sortGrid"></div>
    </div>
    <div style="display:flex;gap:8px;justify-content:center;padding:8px 0">
        <button class="btn accent" onclick="saveRandomSettings()" style="padding:9px 24px">&#128190; 保存随机设置</button>
        <button class="btn ghost" onclick="loadRandomSettings()" style="padding:9px 24px">&#128259; 重新加载</button>
    </div>
</div>
</div>

<div class="float-pool" id="floatPool">
    <div class="pool-header" onclick="togglePool()"><span>&#128230; 临时池</span><span id="poolCount" style="font-size:.7em;background:rgba(255,255,255,.2);padding:1px 7px;border-radius:10px">0</span><span style="margin-left:auto">&#9660;</span></div>
    <div class="pool-body" id="poolBody" ondragover="onDragOver(event)" ondrop="onDropToPool(event)"></div>
    <button class="btn danger xs" onclick="clearPool()" style="margin:4px 6px 6px auto;display:block">清空</button>
</div>

<div class="modal-overlay" id="editModal">
    <div class="modal-box">
        <h3>&#9998; 编辑词条</h3>
        <div class="field"><label>英文</label><input type="text" id="edText"></div>
        <div class="field"><label>中文</label><input type="text" id="edZh"></div>
        <div class="field"><label>标签码（逗号分隔）</label><input type="text" id="edTags"></div>
        <div class="field"><label>移动到分类</label><select id="edCat"></select></div>
        <div class="modal-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn accent" onclick="confirmEdit()">确认</button></div>
    </div>
</div>

<div class="modal-overlay" id="relModal">
    <div class="modal-box">
        <h3>&#128279; 编辑关系</h3>
        <div class="field"><label>标签A</label><select id="edRelA"></select></div>
        <div class="field"><label>关系类型</label><select id="edRelType"><option value="对立">&#128683; 对立</option><option value="吸引">&#129309; 吸引</option></select></div>
        <div class="field"><label>标签B</label><select id="edRelB"></select></div>
        <div class="field"><label>强度</label><select id="edRelStr"><option value="弱">弱</option><option value="中">中</option><option value="强">强</option></select></div>
        <div class="modal-actions"><button class="btn ghost" onclick="closeRelModal()">取消</button><button class="btn accent" onclick="confirmEditRel()">确认</button></div>
    </div>
</div>
<script>
var ALL_TAGS = {{ all_tags_json | safe }};
var DEFAULT_ORDER = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36];
var wb=null,cfg=null,rels=[],pool=[],sOrder=null;
var eInfo=null,dragData=null,dragCatIdx=null,sDragIdx=null,eRelIdx=null;

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function gTags(w){return w.tags||[w.type||'sfw'];}
function hasN(w){return gTags(w).indexOf('nsfw')!==-1;}
function ts2s(t){return(t||[]).join(', ');}

function changeFontSize(v){document.documentElement.style.fontSize=v+'px';localStorage.setItem('ed_fs',v);}
(function(){var s=localStorage.getItem('ed_fs')||14;document.getElementById('fontSize').value=s;changeFontSize(s);})();

function ld(){
    fetch('/api/wordbank').then(r=>r.json()).then(d=>{
        wb=d;rCats();upPool();populateAddForm();
        sS('已加载 '+((wb.categories||[]).reduce(function(s,c){return s+(c.words||[]).length},0))+' 条词','info');
    }).catch(e=>sS('加载失败','error'));
}
function loadRules(){
    fetch('/api/config').then(r=>r.json()).then(d=>{
        cfg=d;rels=d.tag_relations||[];
        if(!d.all_tags||!d.all_tags.length)d.all_tags=ALL_TAGS.slice();ALL_TAGS=d.all_tags;
        for(var i=0;i<rels.length;i++){if(rels[i].enabled===undefined)rels[i].enabled=true;}
        document.getElementById('swCatOrder').checked=cfg.category_order!==false;
        document.getElementById('swTagRel').checked=cfg.tag_relations_enabled!==false;
        if(d.category_order_list&&Array.isArray(d.category_order_list))sOrder=d.category_order_list.slice();
        else sOrder=(wb&&wb.categories)?wb.categories.map(function(c){return c.id;}):DEFAULT_ORDER.slice();
        var ta=document.getElementById('tab_random');if(ta&&ta.classList.contains('active'))rRS();
        var tr=document.getElementById('tab_rules');if(tr&&tr.classList.contains('active')){pT();rR();rT();}
        populateAddForm();
    }).catch(e=>sS('规则加载失败','error'));
}

function switchTab(id,btn){
    var cs=document.querySelectorAll('.tab-content');for(var i=0;i<cs.length;i++)cs[i].classList.remove('active');
    var bs=document.querySelectorAll('.tab-btn');for(var j=0;j<bs.length;j++)bs[j].classList.remove('active');
    document.getElementById(id).classList.add('active');if(btn)btn.classList.add('active');
    if(id==='tab_random')rRS();
    if(id==='tab_rules'){pT();rR();rT();}
    if(id==='tab_words')populateAddForm();
}

function populateAddForm(){
    var s=document.getElementById('addCatId');s.innerHTML='<option value="">-- 选择分类 --</option>';
    if(!wb||!wb.categories)return;
    wb.categories.forEach(function(c){s.innerHTML+='<option value="'+c.id+'">'+esc(c.name)+' (ID:'+c.id+')</option>';});
    rAC();
}
function rAC(){
    var el=document.getElementById('addTagChips');el.innerHTML='';
    ALL_TAGS.forEach(function(t){
        var chip=document.createElement('span');chip.className='tag-chip';chip.textContent=t;
        chip.onclick=function(){chip.classList.toggle('on');};el.appendChild(chip);
    });
    var inp=document.getElementById('addNewTag');
    inp.onkeydown=function(e){
        if(e.key==='Enter'){
            var v=inp.value.trim();
            if(v&&ALL_TAGS.indexOf(v)===-1){ALL_TAGS.push(v);rAC();sS('已添加: '+v,'success');}
            inp.value='';
        }
    };
}
function addWordGlobal(){
    var cid=parseInt(document.getElementById('addCatId').value);
    var text=document.getElementById('addText').value.trim();
    var zh=document.getElementById('addZh').value.trim();
    if(!cid||!text){sS('请选择分类并输入英文','error');return;}
    var chips=document.querySelectorAll('#addTagChips .tag-chip.on'),tags=[];
    chips.forEach(function(c){tags.push(c.textContent);});
    if(tags.indexOf('sfw')===-1&&tags.indexOf('nsfw')===-1)tags.push('sfw');
    var cat=wb.categories.find(function(c){return c.id===cid});if(!cat)return;
    if(cat.words.some(function(w){return w.text.toLowerCase()===text.toLowerCase()})){sS('已存在','error');return;}
    var nw={text:text,tags:tags};if(zh)nw.zh=zh;
    cat.words.push(nw);rCats();document.getElementById('addText').value='';document.getElementById('addZh').value='';
    sS('已添加: '+text,'success');
}

function rCats(){
    var el=document.getElementById('categoryList');el.innerHTML='';
    if(!wb||!wb.categories)return;
    wb.categories.forEach(function(cat,idx){
        var card=document.createElement('div');card.className='cat-card';card.dataset.index=idx;card.draggable=true;
        card.ondragstart=function(e){catDS(e,idx);};card.ondragend=catDE;
        card.ondragover=function(e){e.preventDefault();card.classList.add('drag-over');};
        card.ondragleave=function(e){card.classList.remove('drag-over');};
        card.ondrop=function(e){catDp(e,idx);};
        card.innerHTML=
            '<div class="cat-head">'+
            '<span class="cat-hid">ID:'+cat.id+'</span>'+
            '<span class="cat-hname" onclick="ecn('+cat.id+')" id="cn_'+cat.id+'">'+esc(cat.name)+'</span>'+
            '<span class="cat-hcnt">'+(cat.words||[]).length+'条</span>'+
            '<button class="btn danger xs" onclick="event.stopPropagation();dc('+cat.id+')" style="font-size:.7em;padding:2px 7px">删</button>'+
            '</div>'+
            '<div class="word-grid" id="wg_'+cat.id+'" ondragover="onDragOver(event)" ondrop="onDropToCat(event,'+cat.id+')">'+
            (cat.words||[]).map(function(w){return rt(w,cat.id);}).join('')+'</div>';
        el.appendChild(card);
    });
}
function rt(w,catId){
    var cls=hasN(w)?'nsfw':'sfw',zh=w.zh?' <span class="zh-text">('+esc(w.zh)+')</span>':'';
    var td=' <span class="tag-dots">['+ts2s(gTags(w))+']</span>';
    var te=esc(w.text).replace(/'/g,"\\'"),ze=esc(w.zh||'').replace(/'/g,"\\'");
    return '<span class="word-tag '+cls+'" draggable="true" ondragstart="ds(event,\''+te+'\','+catId+')" ondragend="de(event)">'+
        '<button class="edit-btn" onclick="event.stopPropagation();oe(\''+te+'\',\''+ze+'\','+catId+')">&#9998;</button> '+
        esc(w.text)+zh+td+
        '<button class="del-btn" onclick="event.stopPropagation();dw('+catId+',\''+te+'\')">&times;</button></span>';
}

function dw(catId,text){
    if(!confirm('删除「'+text+'」？'))return;
    var cat=wb.categories.find(function(c){return c.id===catId});if(!cat)return;
    cat.words=cat.words.filter(function(w){return w.text!==text});rCats();sS('已删除','info');
}
function oe(text,zh,catId){
    var cat=wb.categories.find(function(c){return c.id===catId});if(!cat)return;
    var w=cat.words.find(function(w){return w.text===text});if(!w)return;
    document.getElementById('editModal').classList.add('active');
    document.getElementById('edText').value=text;document.getElementById('edZh').value=zh||'';document.getElementById('edTags').value=ts2s(gTags(w));
    var s=document.getElementById('edCat');s.innerHTML='';
    wb.categories.forEach(function(c){var o=document.createElement('option');o.value=c.id;o.textContent=c.name+' (ID:'+c.id+')';if(c.id===catId)o.selected=true;s.appendChild(o);});
    eInfo={text:text,catId:catId};
}
function closeModal(){document.getElementById('editModal').classList.remove('active');eInfo=null;}
function confirmEdit(){
    if(!eInfo)return;
    var nt=document.getElementById('edText').value.trim(),nz=document.getElementById('edZh').value.trim(),
        gs=document.getElementById('edTags').value.trim(),nc=parseInt(document.getElementById('edCat').value);
    if(!nt){sS('英文不能为空','error');return;}
    var ntags=gs?gs.split(',').map(function(x){return x.trim();}).filter(function(x){return x;}):['sfw'];
    if(ntags.indexOf('sfw')===-1&&ntags.indexOf('nsfw')===-1)ntags.push('sfw');
    var oc=wb.categories.find(function(c){return c.id===eInfo.catId}),ow=oc?oc.words.find(function(w){return w.text===eInfo.text}):null;
    if(nc===eInfo.catId&&ow){ow.text=nt;ow.zh=nz||undefined;ow.tags=ntags;delete ow.type;}
    else if(nc!==eInfo.catId&&ow){
        oc.words=oc.words.filter(function(w){return w.text!==eInfo.text});
        var nc2=wb.categories.find(function(c){return c.id===nc});var nw={text:nt,tags:ntags};if(nz)nw.zh=nz;nc2.words.push(nw);
    }
    rCats();sS('已保存','success');closeModal();
}

function addCategory(){
    var n=prompt('新分类名称:');if(!n)return;
    var m=(wb.categories||[]).reduce(function(m,c){return Math.max(m,c.id||0)},0);
    wb.categories.push({id:m+1,name:n,pick_min:1,pick_max:1,words:[]});rCats();sS('已添加','success');
}
function dc(id){if(!confirm('确认删除？'))return;wb.categories=wb.categories.filter(function(c){return c.id!==id});rCats();}
function ecn(id){
    var s=document.getElementById('cn_'+id),c=s.textContent,i=document.createElement('input');i.className='cat-hname-input';i.value=c;
    i.onblur=function(){var n=i.value.trim();if(n&&n!==c){var cat=wb.categories.find(function(c2){return c2.id===id});if(cat){cat.name=n;rCats();}}else rCats();};
    i.onkeydown=function(e){if(e.key==='Enter')i.blur();if(e.key==='Escape')rCats();};s.replaceWith(i);i.focus();i.select();
}
function catDS(e,idx){dragCatIdx=idx;e.target.classList.add('dragging');e.dataTransfer.effectAllowed='move';}
function catDE(e){e.target.classList.remove('dragging');var cs=document.querySelectorAll('.cat-card');for(var i=0;i<cs.length;i++)cs[i].classList.remove('drag-over');}
function catDp(e,ti){e.preventDefault();e.target.classList.remove('drag-over');if(dragCatIdx===null||dragCatIdx===ti)return;
    var cats=wb.categories,it=cats.splice(dragCatIdx,1)[0];cats.splice(ti,0,it);rCats();sS('已调整','info');dragCatIdx=null;}

function ds(e,text,catId){dragData={text:text,sourceCatId:catId};e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',text);e.target.classList.add('dragging');}
function de(e){e.target.classList.remove('dragging');var gs=document.querySelectorAll('.word-grid,.pool-body');for(var i=0;i<gs.length;i++)gs[i].classList.remove('drag-over');}
function onDragOver(e){e.preventDefault();e.dataTransfer.dropEffect='move';e.currentTarget.classList.add('drag-over');}
function onDropToCat(e,tid){e.preventDefault();e.currentTarget.classList.remove('drag-over');if(!dragData)return;if(dragData.sourceCatId===-1)rp(dragData.text);mw(dragData.sourceCatId,dragData.text,tid);dragData=null;}
function onDropToPool(e){e.preventDefault();e.currentTarget.classList.remove('drag-over');if(!dragData)return;ap(dragData.text);if(dragData.sourceCatId!==-1){var cat=wb.categories.find(function(c){return c.id===dragData.sourceCatId});if(cat){cat.words=cat.words.filter(function(w){return w.text!==dragData.text});rCats();}}dragData=null;}
function mw(fId,text,tId){var f=wb.categories.find(function(c){return c.id===fId}),t=wb.categories.find(function(c){return c.id===tId});if(!f||!t)return;var idx=f.words.findIndex(function(w){return w.text===text});if(idx===-1)return;var w=f.words.splice(idx,1)[0];if(!t.words.some(function(ww){return ww.text.toLowerCase()===text.toLowerCase()}))t.words.push(w);rCats();sS('已移动','info');}

function ap(text){if(!pool.some(function(p){return p.text===text}))pool.push({text:text});rP();}
function rp(text){pool=pool.filter(function(p){return p.text!==text});rP();}
function clearPool(){pool=[];rP();}
function rP(){var b=document.getElementById('poolBody');b.innerHTML='';pool.forEach(function(p){var t=document.createElement('span');t.className='word-tag sfw';t.draggable=true;t.textContent=p.text+' ';t.ondragstart=function(e){dragData={text:p.text,sourceCatId:-1};e.dataTransfer.setData('text/plain',p.text);t.classList.add('dragging');};t.ondragend=function(e){t.classList.remove('dragging');};var d=document.createElement('button');d.className='del-btn';d.innerHTML='&times;';d.onclick=function(){rp(p.text);};t.appendChild(d);b.appendChild(t);});upPool();}
function upPool(){document.getElementById('poolCount').textContent=pool.length;}
function togglePool(){var p=document.getElementById('floatPool');p.classList.toggle('minimized');}

function toggleCfg(key){var el=document.getElementById(key==='category_order'?'swCatOrder':'swTagRel');if(!cfg)cfg={};cfg[key]=el.querySelector('input').checked;}
function pT(){
    var sA=document.getElementById('relTagA'),sB=document.getElementById('relTagB');
    sA.innerHTML='<option value="">标签A</option>';sB.innerHTML='<option value="">标签B</option>';
    ALL_TAGS.forEach(function(t){sA.innerHTML+='<option value="'+t+'">'+t+'</option>';sB.innerHTML+='<option value="'+t+'">'+t+'</option>';});
}
function rR(){
    var el=document.getElementById('relGrid');el.innerHTML='';
    if(!rels.length){el.innerHTML='<p style="color:var(--muted);font-size:.85em">暂无关系</p>';return;}
    rels.forEach(function(rel,idx){
        var card=document.createElement('div');card.className='rel-card';var op=rel.relation==='对立',en=rel.enabled!==false;
        card.innerHTML=
            '<span class="rel-tag">'+esc(rel.tag_a)+'</span>'+
            '<span class="rel-icon">'+(op?'&#128683;':'&#129309;')+'</span>'+
            '<span class="rel-badge '+(op?'oppose':'attract')+'">'+rel.relation+'</span>'+
            '<span>→</span><span class="rel-tag">'+esc(rel.tag_b)+'</span>'+
            '<span class="rel-strength">'+rel.strength+'</span>'+
            '<span class="rel-actions">'+
            '<label class="switch rel-switch" onclick="event.stopPropagation()"><input type="checkbox" '+(en?'checked':'')+' onchange="toggleRel('+idx+',this.checked)"><span class="slider"></span></label>'+
            '<button class="btn ghost xs" onclick="eor('+idx+')">&#9998;</button>'+
            '<button class="btn danger xs" onclick="dr('+idx+')">&times;</button></span>';
        el.appendChild(card);
    });
}
function toggleRel(idx,checked){rels[idx].enabled=checked;}
function addRelation(){
    var a=document.getElementById('relTagA').value,b=document.getElementById('relTagB').value,t=document.getElementById('relType').value,s=document.getElementById('relStrength').value;
    if(!a||!b){sS('请选择两个标签','error');return;}if(a===b){sS('不能跟自己配','error');return;}
    if(rels.some(function(r){return(r.tag_a===a&&r.tag_b===b)||(r.tag_a===b&&r.tag_b===a);})){sS('已有关系','error');return;}
    rels.push({tag_a:a,tag_b:b,relation:t,strength:s,enabled:true});rR();sS('已添加','success');
}
function dr(idx){if(!confirm('删除？'))return;rels.splice(idx,1);rR();sS('已删除','info');}
function eor(idx){
    var rel=rels[idx];document.getElementById('relModal').classList.add('active');
    var sA=document.getElementById('edRelA');sA.innerHTML='';ALL_TAGS.forEach(function(t){sA.innerHTML+='<option value="'+t+'"'+(t===rel.tag_a?' selected':'')+'>'+t+'</option>';});
    document.getElementById('edRelType').value=rel.relation;
    var sB=document.getElementById('edRelB');sB.innerHTML='';ALL_TAGS.forEach(function(t){sB.innerHTML+='<option value="'+t+'"'+(t===rel.tag_b?' selected':'')+'>'+t+'</option>';});
    document.getElementById('edRelStr').value=rel.strength||'中';eRelIdx=idx;
}
function closeRelModal(){document.getElementById('relModal').classList.remove('active');eRelIdx=null;}
function confirmEditRel(){if(eRelIdx===null)return;rels[eRelIdx].tag_a=document.getElementById('edRelA').value;rels[eRelIdx].tag_b=document.getElementById('edRelB').value;rels[eRelIdx].relation=document.getElementById('edRelType').value;rels[eRelIdx].strength=document.getElementById('edRelStr').value;closeRelModal();rR();sS('已更新','success');}
function rT(){
    var el=document.getElementById('tagCodeList');el.innerHTML='';
    ALL_TAGS.forEach(function(t){
        var chip=document.createElement('span');chip.className='tag-chip';chip.textContent=t;
        var rm=document.createElement('span');rm.className='rm-btn';rm.innerHTML='&times;';
        rm.onclick=function(e){e.stopPropagation();if(!confirm('删除标签码「'+t+'」?'))return;var idx=ALL_TAGS.indexOf(t);if(idx!==-1)ALL_TAGS.splice(idx,1);rT();sS('已删除: '+t,'info');};
        chip.appendChild(rm);el.appendChild(chip);
    });
}
function addTagCode(){var v=document.getElementById('newTagCode').value.trim();if(!v){sS('请输入','error');return;}if(ALL_TAGS.indexOf(v)!==-1){sS('已存在','error');return;}ALL_TAGS.push(v);rT();document.getElementById('newTagCode').value='';sS('已添加: '+v,'success');}
function saveRules(){
    if(!cfg)cfg={};cfg.tag_relations=rels.slice();cfg.all_tags=ALL_TAGS.slice();
    cfg.category_order=document.getElementById('swCatOrder').querySelector('input').checked;
    cfg.tag_relations_enabled=document.getElementById('swTagRel').querySelector('input').checked;
    fetch('/api/save_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)})
    .then(r=>r.json()).then(d=>{if(d.status==='ok')sS('规则已保存','success');else sS('保存失败','error');}).catch(e=>sS('保存失败','error'));
}

function rRS(){rPG();rSG();}
function rPG(){
    if(!wb||!wb.categories)return;var el=document.getElementById('pickGrid');el.innerHTML='';
    wb.categories.forEach(function(cat){
        var div=document.createElement('div');div.className='pick-item';
        div.innerHTML='<span class="pick-name" title="'+esc(cat.name)+'">'+esc(cat.name)+'</span><span class="pick-id">ID:'+cat.id+'</span>'+
            '<input class="pick-input" type="number" min="0" id="tp_'+cat.id+'" value="'+(cat.pick_min != null ? cat.pick_min : 1)+'" onchange="ut('+cat.id+')"><span class="pick-sep">~</span>'+
            '<input class="pick-input" type="number" min="0" id="tx_'+cat.id+'" value="'+(cat.pick_max != null ? cat.pick_max : 1)+'" onchange="ut('+cat.id+')">'+
            '<span class="pick-cnt">'+(cat.words||[]).length+'条</span>';
        el.appendChild(div);
    });
}
function ut(catId){var cat=wb.categories.find(function(c){return c.id===catId});if(!cat)return;cat.pick_min=parseInt(document.getElementById('tp_'+catId).value)||0;cat.pick_max=parseInt(document.getElementById('tx_'+catId).value)||0;if(cat.pick_min>cat.pick_max)cat.pick_max=cat.pick_min;}
function rSG(){
    if(!sOrder||!sOrder.length)sOrder=(wb&&wb.categories)?wb.categories.map(function(c){return c.id;}):DEFAULT_ORDER.slice();
    var el=document.getElementById('sortGrid'),cm={};el.innerHTML='';
    if(wb&&wb.categories)wb.categories.forEach(function(c){cm[c.id]=c;});
    sOrder.forEach(function(cid,idx){
        var cat=cm[cid],name=cat?cat.name:'(缺失:'+cid+')';
        var tag=document.createElement('div');tag.className='sort-tag';tag.dataset.index=idx;tag.draggable=true;
        tag.ondragstart=function(e){sDs(e,idx);};tag.ondragend=sDe;tag.ondragover=function(e){e.preventDefault();this.classList.add('drag-over');};tag.ondragleave=function(e){this.classList.remove('drag-over');};tag.ondrop=function(e){sDp(e,idx);};
        tag.innerHTML='<span class="sort-idx">'+(idx+1)+'</span><span>'+esc(name)+'</span><span style="font-size:.7em;color:var(--muted)">ID:'+cid+'</span>';
        el.appendChild(tag);
    });
}
function sDs(e,idx){sDragIdx=idx;e.target.classList.add('dragging');e.dataTransfer.effectAllowed='move';}
function sDe(e){e.target.classList.remove('dragging');var ts=document.querySelectorAll('.sort-tag');for(var i=0;i<ts.length;i++)ts[i].classList.remove('drag-over');}
function sDp(e,ti){e.preventDefault();e.target.classList.remove('drag-over');if(sDragIdx===null||sDragIdx===ti)return;var it=sOrder.splice(sDragIdx,1)[0];sOrder.splice(ti,0,it);rSG();sS('已调整','info');sDragIdx=null;}
function resetSortOrder(){if(!confirm('重置？'))return;sOrder=DEFAULT_ORDER.slice();rSG();sS('已重置','info');}
function saveRandomSettings(){
    if(!wb||!sOrder)return;if(!cfg)cfg={};cfg.category_order_list=sOrder.slice();
    fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(wb)})
    .then(r=>r.json()).then(d=>{if(d.status!=='ok'){sS('词库保存失败','error');return;}return fetch('/api/save_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});})
    .then(r=>r&&r.json()).then(d=>{if(d&&d.status==='ok')sS('已保存','success');else if(d)sS('排序保存失败','error');}).catch(e=>sS('保存失败','error'));}
function loadRandomSettings(){if(!confirm('重新加载？'))return;ld();loadRules();sS('已重新加载','info');}

function saveAll(){
    fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(wb)})
    .then(r=>r.json()).then(d=>{
        if(d.status==='ok'){sS('词库已保存','success');if(cfg){cfg.tag_relations=rels.slice();cfg.all_tags=ALL_TAGS.slice();fetch('/api/save_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});}}
        else sS('保存失败','error');
    }).catch(e=>sS('保存失败','error'));
}
function refreshFromFile(){if(!confirm('重新加载？'))return;ld();loadRules();sS('已重新加载','info');}
function reloadMainCache(){fetch('/api/reload_cache',{method:'POST'}).then(r=>r.json()).then(d=>{if(d.status==='ok')sS('缓存已刷新','success');else sS('主程序未运行','error');}).catch(e=>sS('主程序未运行','error'));}

function sS(msg,type){var el=document.getElementById('statusInfo');el.textContent=msg;el.style.color=type==='error'?'var(--red)':type==='success'?'var(--green)':'var(--muted)';}
function showToast(msg,type){var t=document.createElement('div');t.className='toast '+(type||'info');t.textContent=msg;document.body.appendChild(t);setTimeout(function(){t.remove();},1800);}
window.onload=function(){ld();loadRules();};
</script></body></html>'''

@app.route('/')
def index():
    cfg=load_config()
    tags=cfg.get("all_tags",_DEFAULT_TAGS)
    return render_template_string(EDITOR_HTML, all_tags_json=json.dumps(tags))

@app.route('/api/wordbank')
def api_wb(): return jsonify(load_wordbank())

@app.route('/api/save',methods=['POST'])
def api_save():
    try: save_wordbank(request.json); return jsonify({"status":"ok"})
    except Exception as e: return jsonify({"status":"error","message":str(e)})

@app.route('/api/config')
def api_cfg(): return jsonify(load_config())

@app.route('/api/save_config',methods=['POST'])
def api_sc():
    try: save_config(request.json); return jsonify({"status":"ok"})
    except Exception as e: return jsonify({"status":"error","message":str(e)})

@app.route('/api/reload_cache',methods=['POST'])
def api_rc():
    try:
        import requests
        r=requests.post("http://127.0.0.1:5000/api/random/wordbank/reload",timeout=3)
        return jsonify({"status":"ok" if r.status_code==200 else "error"})
    except Exception as e: return jsonify({"status":"error","message":str(e)})

if __name__=='__main__':
    print("="*50)
    print("  ArtLink 词库编辑器 v3.1")
    print(f"  端口: 19876")
    print("="*50)
    app.run(host='127.0.0.1',port=19876,debug=False)
