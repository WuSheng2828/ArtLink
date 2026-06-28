#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ArtLink 词库过滤外挂文件（框架版）
====================================
规则注册框架 + 分类排序 + 标签关系引擎（对立/吸引）
"""

import json
import random
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# =====================================================================
# 分类顺序（默认值）
# =====================================================================
CATEGORY_ORDER = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
    31, 32, 33, 34, 35, 36
]


# =====================================================================
# 配置加载
# =====================================================================
def load_rule_config():
    config_path = BASE_DIR / "wordbank_filter_config.json"
    defaults = {
        "category_order": True,
        "category_order_list": None,
        "tag_relations_enabled": True,
        "tag_relations": [],
        "all_tags": [],
    }
    if not config_path.exists():
        return defaults
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for k in defaults:
            if k not in data:
                data[k] = defaults[k]
        return data
    except:
        return defaults


def load_relations():
    """供外部调用的关系加载函数"""
    cfg = load_rule_config()
    return cfg.get("tag_relations", [])


# =====================================================================
# 内置规则1：分类顺序排列
# =====================================================================
def rule_category_order(picked_by_category, sfw_enabled, nsfw_enabled, full_prompt_text):
    rule_config = load_rule_config()
    if not rule_config.get("category_order", True):
        return None
    custom_order = rule_config.get("category_order_list", None)
    order = custom_order if custom_order else CATEGORY_ORDER
    ordered_tags = []
    for cat_id in order:
        words = picked_by_category.get(cat_id, [])
        ordered_tags.extend(words)
    new_prompt = ", ".join(ordered_tags)
    return (picked_by_category, new_prompt, False)


# =====================================================================
# 内置规则2：标签对立检测
# =====================================================================
def rule_tag_oppose(picked_by_category, sfw_enabled, nsfw_enabled, full_prompt_text):
    """检测所有已选标签中是否存在对立关系，触发重抽"""
    rule_config = load_rule_config()
    if not rule_config.get("tag_relations_enabled", True):
        return None
    relations = rule_config.get("tag_relations", [])
    if not relations:
        return None

    # 收集所有已选词条的标签码
    all_tags = set()
    for words in picked_by_category.values():
        for w in words:
            # 从词库中拿到这个词的tags（需要遍历所有分类）
            all_tags.add(w.lower())

    # 检查对立关系
    for rel in relations:
        if rel["relation"] != "对立":
            continue
        tag_a = rel["tag_a"].lower()
        tag_b = rel["tag_b"].lower()
        if tag_a in all_tags and tag_b in all_tags:
            if rel.get("enabled") is False:  # ← 加这行
                continue                      # ← 加这行
            strength = rel.get("strength", "中")
            if strength == "强":
                return (picked_by_category, full_prompt_text, True)
            elif strength == "中":
                if random.random() < 0.5:
                    return (picked_by_category, full_prompt_text, True)
            # 弱：不触发重抽

    return None


# =====================================================================
# 规则注册列表
# =====================================================================
RULES = [
    ("分类顺序排列", rule_category_order, "category_order"),
    ("标签对立检测", rule_tag_oppose, "tag_relations_enabled"),
]


# =====================================================================
# 主入口
# =====================================================================
def filter_by_rules(picked_by_category, sfw_enabled=True, nsfw_enabled=False, full_prompt_text=""):
    rule_config = load_rule_config()

    for rule_name, rule_func, config_key in RULES:
        if not rule_config.get(config_key, True):
            continue
        try:
            result = rule_func(picked_by_category, sfw_enabled, nsfw_enabled, full_prompt_text)
            if result is not None:
                picked_by_category, full_prompt_text, needs_retry = result
                if needs_retry:
                    return picked_by_category, full_prompt_text, True
        except Exception as e:
            import logging
            logging.getLogger("ArtLink").warning(f"规则 [{rule_name}] 异常: {e}，跳过")
            continue

    return picked_by_category, full_prompt_text, False
