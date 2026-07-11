from __future__ import annotations

import json
import os
import re
from datetime import datetime
from functools import lru_cache
import asyncio

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.filter.permission import PermissionType
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType


@register(
    "astrbot_plugin_soft_whitelist",
    "TenmaGabriel0721",
    "只拦截 message 的高优先级软白名单插件，不处理 request、notice 和 meta_event",
    "v1.4.0",
    "local",
)
class SoftWhitelist(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.state_path = os.path.join(os.path.dirname(__file__), "auto_reply_state.json")
        self.auto_reply_state = self._load_auto_reply_state()
        self._state_dirty = False  # 延迟刷盘脏标记
        # 群白名单索引缓存 {group_id: item_dict}，惰性构建，配置变更时失效
        self._group_index: dict[str, dict] | None = None
        self._group_index_dirty: bool = True
        logger.info("[soft_whitelist] 插件初始化完成")
        asyncio.create_task(self._periodic_flush_state())

    # 场景到嵌套配置键的映射
    SCENE_KEY_MAP = {
        "group": "group_chat",
        "friend": "friend_chat",
        "temp": "temp_session",
    }

    def _cfg(self, key: str, default=None):
        """支持点号分隔的嵌套键读取，如 group_chat.enabled"""
        keys = key.split(".")
        val = self.config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default

    def _set_cfg(self, key: str, value):
        """支持点号分隔的嵌套键写入，如 group_chat.enabled"""
        keys = key.split(".")
        val = self.config
        for k in keys[:-1]:
            if k not in val or not isinstance(val[k], dict):
                val[k] = {}
            val = val[k]
        val[keys[-1]] = value
        self.config.save_config()
        # 写入 whitelist_items 时使群索引缓存失效
        if "whitelist_items" in key:
            self._invalidate_group_index()

    def _list_cfg(self, key: str) -> list[str]:
        return [str(x) for x in self._cfg(key, [])]

    def _replace_list_cfg(self, key: str, value: list[str]):
        self._set_cfg(key, value)

    def _add_to_list_cfg(self, key: str, value: str) -> tuple[bool, list[str]]:
        data = self._list_cfg(key)
        if value in data:
            return False, data
        data.append(value)
        self._set_cfg(key, data)
        return True, data

    def _remove_from_list_cfg(self, key: str, value: str) -> tuple[bool, list[str]]:
        data = self._list_cfg(key)
        if value not in data:
            return False, data
        data.remove(value)
        self._set_cfg(key, data)
        return True, data

    # ── template_list 辅助方法 ──────────────────────────────────────

    def _get_template_values(self, key: str, template_key: str) -> list[str]:
        """从 template_list 中提取指定模板的 value 列表"""
        return [
            str(item.get("value", ""))
            for item in self._cfg(key, [])
            if isinstance(item, dict) and item.get("__template_key") == template_key
        ]

    def _get_template_values_grouped(self, key: str) -> dict[str, list[str]]:
        """一次读取 template_list，按 __template_key 分组返回所有 value 列表"""
        result: dict[str, list[str]] = {}
        for item in self._cfg(key, []):
            if isinstance(item, dict):
                tk = item.get("__template_key")
                val = str(item.get("value", ""))
                if tk:
                    result.setdefault(tk, []).append(val)
        return result

    def _add_template_item(
        self, key: str, template_key: str, value: str, extra: dict | None = None
    ) -> tuple[bool, list[dict]]:
        """向 template_list 中添加一个模板项，extra 为额外字段"""
        items = self._cfg(key, [])
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("__template_key") == template_key
                and str(item.get("value", "")) == value
            ):
                return False, items
        entry = {"__template_key": template_key, "value": value}
        if extra:
            entry.update(extra)
        items.append(entry)
        self._set_cfg(key, items)
        return True, items

    def _remove_template_item(
        self, key: str, template_key: str, value: str
    ) -> tuple[bool, list[dict]]:
        """从 template_list 中移除一个模板项"""
        items = self._cfg(key, [])
        new_items = [
            item
            for item in items
            if not (
                isinstance(item, dict)
                and item.get("__template_key") == template_key
                and str(item.get("value", "")) == value
            )
        ]
        if len(new_items) == len(items):
            return False, items
        self._set_cfg(key, new_items)
        return True, new_items

    # ── 群聊 allowed_users 管理（单模板结构） ─────────────────────

    def _get_group_all_users(self) -> list[str]:
        """从所有群条目的 allowed_users 中提取所有用户"""
        items = self._cfg("group_chat.whitelist_items", [])
        users: set[str] = set()
        for item in items:
            if isinstance(item, dict) and item.get("__template_key") == "group":
                for u in item.get("allowed_users", []):
                    users.add(str(u))
        return list(users)

    def _add_user_to_group_entries(self, value: str) -> tuple[bool, list]:
        """向所有群条目的 allowed_users 中添加用户"""
        items = self._cfg("group_chat.whitelist_items", [])
        added = False
        for item in items:
            if not isinstance(item, dict) or item.get("__template_key") != "group":
                continue
            allowed = item.get("allowed_users", [])
            # 使用生成器表达式避免创建临时列表
            if not any(str(u) == value for u in allowed):
                allowed.append(value)
                item["allowed_users"] = allowed
                added = True
        if added:
            self._set_cfg("group_chat.whitelist_items", items)
        return added, items

    def _remove_user_from_group_entries(self, value: str) -> tuple[bool, list]:
        """从所有群条目的 allowed_users 中移除用户"""
        items = self._cfg("group_chat.whitelist_items", [])
        removed = False
        for item in items:
            if not isinstance(item, dict) or item.get("__template_key") != "group":
                continue
            allowed = item.get("allowed_users", [])
            new_allowed = [u for u in allowed if str(u) != value]
            if len(new_allowed) < len(allowed):
                item["allowed_users"] = new_allowed
                removed = True
        if removed:
            self._set_cfg("group_chat.whitelist_items", items)
        return removed, items

    # ── 群白名单索引缓存 ────────────────────────────────────────────

    def _invalidate_group_index(self):
        """使群白名单索引缓存失效，下次访问时重建"""
        self._group_index = None
        self._group_index_dirty = True

    def _rebuild_group_index(self):
        """从 whitelist_items 重建 {group_id: item} 索引"""
        items = self._cfg("group_chat.whitelist_items", [])
        index: dict[str, dict] = {}
        for item in items:
            if isinstance(item, dict) and item.get("__template_key") == "group":
                gid = str(item.get("value", ""))
                if gid:
                    index[gid] = item
        self._group_index = index
        self._group_index_dirty = False

    def _get_group_item(self, group_id: str) -> dict | None:
        """O(1) 获取群白名单条目，索引过期时自动重建"""
        if not group_id:
            return None
        if self._group_index_dirty or self._group_index is None:
            self._rebuild_group_index()
        return self._group_index.get(group_id)

    # ── 拦截关键词辅助方法 ─────────────────────────────────────────

    def _get_message_text(self, raw_message: dict) -> str:
        """从 raw_message 中提取纯文本内容"""
        msg = raw_message.get("message", "")
        if isinstance(msg, list):
            parts = []
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
            return "".join(parts)
        return str(msg) if msg else ""

    @staticmethod
    @lru_cache(maxsize=128)
    def _compile_pattern(pattern: str) -> re.Pattern | None:
        """预编译正则表达式并缓存，失败时记录警告日志"""
        try:
            return re.compile(pattern)
        except re.error as e:
            logger.warning(f"[soft_whitelist] 无效的正则表达式已被跳过: {pattern!r}, 错误: {e}")
            return None

    def _is_blocked_by_keywords(self, raw_message: dict, group_id: str = "") -> bool:
        """检查群聊消息是否匹配拦截关键词。

        每个群可独立配置拦截模式：
        - 共存（默认）：全局拦截 + 该群群拦截 同时生效
        - 替换：只使用该群的群拦截，忽略全局拦截
        """
        text = self._get_message_text(raw_message)
        if not text:
            return False

        global_patterns = self._list_cfg("group_chat.block_keywords")

        # O(1) 查找该群的独立配置（利用索引缓存，避免 O(n) 遍历）
        group_patterns: list[str] = []
        block_mode = "共存"
        if group_id:
            item = self._get_group_item(group_id)
            if item:
                group_patterns = item.get("group_block_keywords", []) or []
                block_mode = str(item.get("block_mode", "共存"))

        # 根据模式确定最终正则列表
        if block_mode == "替换":
            patterns = list(group_patterns)
        else:  # 共存
            patterns = list(global_patterns)
            patterns.extend(group_patterns)

        if not patterns:
            return False

        for pattern in patterns:
            compiled = self._compile_pattern(pattern)
            if compiled and compiled.search(text):
                return True
        return False

    async def _get_joined_group_ids(self, event: AiocqhttpMessageEvent) -> set[str]:
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "get_group_list"):
            raise RuntimeError("当前平台不支持获取群列表")

        group_list = await bot.get_group_list()
        if isinstance(group_list, dict):
            group_list = group_list.get("data", [])
        if not isinstance(group_list, list):
            raise RuntimeError("获取群列表返回了无法识别的数据")

        joined_group_ids: set[str] = set()
        for group in group_list:
            if not isinstance(group, dict):
                continue
            group_id = group.get("group_id")
            if group_id is not None:
                joined_group_ids.add(str(group_id))
        return joined_group_ids

    def _prune_group_cfg(
        self,
        key: str,
        joined_group_ids: set[str],
    ) -> tuple[list[str], list[str]]:
        before = self._list_cfg(key)
        after: list[str] = []
        removed: list[str] = []
        for group_id in before:
            if group_id in joined_group_ids:
                after.append(group_id)
            else:
                removed.append(group_id)
        if removed:
            self._replace_list_cfg(key, after)
        return removed, after

    def _prune_template_group_cfg(
        self, key: str, joined_group_ids: set[str]
    ) -> tuple[list[str], list[dict]]:
        """从 template_list 中剔除 Bot 已退出的 'group' 模板项（单次遍历）"""
        items = self._cfg(key, [])
        removed_group_ids: list[str] = []
        after: list[dict] = []
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("__template_key") == "group"
                and str(item.get("value", "")) not in joined_group_ids
            ):
                removed_group_ids.append(str(item.get("value", "")))
            else:
                after.append(item)
        if not removed_group_ids:
            return [], items
        self._set_cfg(key, after)
        return removed_group_ids, after

    @staticmethod
    def _validate_numeric_id(raw: str, label: str = "ID") -> str | None:
        """校验并返回纯数字 ID，无效时返回 None"""
        cleaned = raw.strip()
        if not cleaned or not cleaned.isdigit():
            return None
        return cleaned

    def _format_removed_ids(self, ids: list[str]) -> str:
        if not ids:
            return "无"
        max_show = 30
        shown = ", ".join(ids[:max_show])
        if len(ids) > max_show:
            shown += f" 等 {len(ids)} 个"
        return shown

    def _load_auto_reply_state(self) -> dict:
        try:
            if not os.path.exists(self.state_path):
                return {}
            with open(self.state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            # 清理超过 30 天的过期记录
            today = datetime.now()
            stale_keys = [
                k for k, v in data.items()
                if isinstance(v, str) and (today - datetime.strptime(v, '%Y-%m-%d')).days > 30
            ]
            for k in stale_keys:
                del data[k]
            if stale_keys:
                logger.info(f"[soft_whitelist] 已清理 {len(stale_keys)} 条过期的自动回复状态记录")
            return data
        except Exception as e:
            logger.warning(f"[soft_whitelist] 读取自动回复状态失败: {e}")
            return {}

    def _flush_state(self):
        """将自动回复状态立即刷入磁盘"""
        if not self._state_dirty:
            return
        try:
            with open(self.state_path, 'w', encoding='utf-8') as f:
                json.dump(self.auto_reply_state, f, ensure_ascii=False, indent=2)
            self._state_dirty = False
        except Exception as e:
            logger.error(f"[soft_whitelist] 保存自动回复状态失败: {e}")

    async def _periodic_flush_state(self):
        """后台定时刷盘任务（每 60 秒），含异常保护"""
        while True:
            try:
                await asyncio.sleep(60)
                self._flush_state()
            except asyncio.CancelledError:
                # 插件卸载时主动取消任务，立即刷一次盘确保数据不丢
                self._flush_state()
                raise
            except Exception as e:
                logger.error(f"[soft_whitelist] 定时刷盘异常: {e}", exc_info=True)

    def _today_str(self) -> str:
        return datetime.now().strftime('%Y-%m-%d')

    def _auto_reply_key(self, scene: str, sender_id: str) -> str:
        return f"{scene}:{sender_id}"

    def _can_send_auto_reply(self, scene: str, sender_id: str) -> bool:
        if not sender_id:
            return False
        key = self._auto_reply_key(scene, sender_id)
        today = self._today_str()
        last_day = str(self.auto_reply_state.get(key, ''))
        return last_day != today

    def _mark_auto_reply_sent(self, scene: str, sender_id: str):
        if not sender_id:
            return
        key = self._auto_reply_key(scene, sender_id)
        self.auto_reply_state[key] = self._today_str()
        self._state_dirty = True  # 标记脏，由后台任务延迟刷盘

    def _sender_id(self, event: AiocqhttpMessageEvent, raw_message: dict) -> str:
        sender_id = raw_message.get("user_id") or event.get_sender_id() or ""
        return str(sender_id) if sender_id is not None else ""

    def _group_id(self, event: AiocqhttpMessageEvent, raw_message: dict) -> str:
        group_id = raw_message.get("group_id") or event.get_group_id() or ""
        return str(group_id) if group_id is not None else ""

    def _is_group_admin(self, event: AiocqhttpMessageEvent) -> bool:
        try:
            sender = event.get_sender() or {}
            role = str(sender.get("role", ""))
            return role in {"owner", "admin"}
        except Exception:
            return False

    def _is_bot_admin(self, sender_id: str) -> bool:
        if not bool(self._cfg("allow_bot_admins", True)):
            return False
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            admins = [str(x) for x in admins]
            return sender_id in admins
        except Exception:
            return False

    def _get_scene(self, raw_message: dict) -> str | None:
        if raw_message.get("post_type") == "notice":
            return "group" if raw_message.get("group_id") else "friend"
        if raw_message.get("post_type") != "message":
            return None

        message_type = raw_message.get("message_type")
        if message_type == "group":
            return "group"
        if message_type == "private":
            sub_type = str(raw_message.get("sub_type", ""))
            if sub_type == "friend":
                return "friend"
            return "temp"
        return None

    def _check_group_list_layer(self, group_id: str) -> bool:
        """第一层筛选：黑白名单列表。

        白名单模式：仅 group_whitelist 中的群放行 → 进入二次筛选
        黑名单模式：group_blacklist 中的群直接拦截，其余放行 → 进入二次筛选
        """
        if not group_id:
            return False
        mode = str(self._cfg("group_chat.group_filter_mode", "白名单"))
        if mode == "黑名单":
            blacklist = self._list_cfg("group_chat.group_blacklist")
            return group_id not in blacklist
        # 白名单模式（默认）
        whitelist = self._list_cfg("group_chat.group_whitelist")
        return group_id in whitelist

    def _match_whitelist(self, scene: str, sender_id: str, group_id: str) -> bool:
        if scene == "group":
            # 第一层：黑白名单列表筛选（群级别）
            if not self._check_group_list_layer(group_id):
                return False

            # 第二层：二次筛选（详细设置 - whitelist_items）
            item = self._get_group_item(group_id)
            if item is None:
                # 不在详细设置中 → 直接放过
                return True
            allowed = item.get("allowed_users", [])
            if not allowed:  # 空列表 = 该群所有人放行
                return True
            # 使用生成器表达式避免创建临时列表
            return any(str(u) == sender_id for u in allowed)

        if scene == "friend":
            user_whitelist = self._list_cfg("friend_chat.user_whitelist")
            return sender_id in user_whitelist

        if scene == "temp":
            user_whitelist = self._list_cfg("temp_session.user_whitelist")
            group_whitelist = self._list_cfg("temp_session.group_whitelist")
            return (sender_id in user_whitelist) or (group_id in group_whitelist)

        return False

    def _scene_enabled(self, scene: str) -> bool:
        key = f"{self.SCENE_KEY_MAP.get(scene, '')}.enabled"
        return bool(self._cfg(key, True))

    def _allow_message_scene(
        self,
        event: AiocqhttpMessageEvent,
        raw_message: dict,
        scene: str,
        sender_id: str = "",
        group_id: str = "",
    ) -> bool:
        # 批量读取全部所需配置（一次遍历替代多次 _cfg 调用）
        c = self.config
        enabled = bool(c.get("enabled", True))
        if not enabled:
            return True

        # 仅当 sender_id/group_id 未传入时再提取（避免 soft_filter 重复提取）
        if not sender_id:
            sender_id = self._sender_id(event, raw_message)
        if not group_id:
            group_id = self._group_id(event, raw_message)
        self_id = str(event.get_self_id()) if event.get_self_id() is not None else ""

        scene_map = self.SCENE_KEY_MAP.get(scene, "")
        scene_cfg = c.get(scene_map, {}) if scene_map else {}

        if not bool(scene_cfg.get("enabled", True)):
            return True

        if bool(c.get("allow_self", True)) and sender_id and sender_id == self_id:
            return True

        if self._is_bot_admin(sender_id):
            return True

        if scene == "group" and bool(scene_cfg.get("allow_group_admins", True)) and self._is_group_admin(event):
            return True

        if not self._match_whitelist(scene, sender_id, group_id):
            return False

        # 即使在白名单中，群聊消息若匹配拦截关键词也拦截（传入 group_id 支持群独立配置）
        if scene == "group" and self._is_blocked_by_keywords(raw_message, group_id):
            return False

        return True

    def _is_allowed_request(self, raw_message: dict) -> bool:
        if raw_message.get("post_type") != "request":
            return False

        request_type = str(raw_message.get("request_type", ""))
        if request_type == "friend":
            return True
        if request_type == "group" and str(raw_message.get("sub_type", "")) == "invite":
            return True
        return False

    def _get_block_reply(self, scene: str) -> str:
        if scene == "friend":
            return str(self._cfg("friend_chat.block_reply_text", "你暂时不在我的白名单里，先别急着找我哦"))
        if scene == "temp":
            return str(self._cfg("temp_session.block_reply_text", "你暂时不在我的白名单里，可以先走审批哦"))
        return ""

    def _should_auto_reply_when_blocked(self, scene: str, raw_message: dict, sender_id: str) -> bool:
        if scene == "friend":
            if not bool(self._cfg("friend_chat.block_auto_reply", False)):
                return False
        elif scene == "temp":
            if not bool(self._cfg("temp_session.block_auto_reply", False)):
                return False
        else:
            return False

        # 仅在收到对方发来的消息时回复
        if raw_message.get("post_type") != "message":
            return False
        if not sender_id:
            return False

        # 每人每天最多回复一次
        return self._can_send_auto_reply(scene, sender_id)

    def _format_status(self) -> str:
        # 一次性读取全部配置（直接访问字典，避免多次 _cfg 调用）
        c = self.config
        enabled = bool(c.get("enabled", True))
        allow_self = bool(c.get("allow_self", True))
        allow_admins = bool(c.get("allow_bot_admins", True))

        gc = c.get("group_chat", {})
        fc = c.get("friend_chat", {})
        ts = c.get("temp_session", {})

        gc_enabled = bool(gc.get("enabled", True))
        gc_admin = bool(gc.get("allow_group_admins", True))
        gc_filter_mode = str(gc.get("group_filter_mode", "白名单"))
        gc_wl = ", ".join(self._list_cfg("group_chat.group_whitelist")) or "空"
        gc_bl = ", ".join(self._list_cfg("group_chat.group_blacklist")) or "空"
        gc_grouped = self._get_template_values_grouped("group_chat.whitelist_items")
        gc_groups = ", ".join(gc_grouped.get("group", [])) or "空"
        gc_users = ", ".join(self._get_group_all_users()) or "空"
        gc_keywords_cnt = len(gc.get("block_keywords", []))

        fc_enabled = bool(fc.get("enabled", True))
        fc_list = ", ".join(self._list_cfg("friend_chat.user_whitelist")) or "空"
        fc_auto_reply = bool(fc.get("block_auto_reply", False))
        fc_reply_text = fc.get("block_reply_text", "") or "空"

        ts_enabled = bool(ts.get("enabled", True))
        ts_users = ", ".join(self._list_cfg("temp_session.user_whitelist")) or "空"
        ts_groups = ", ".join(self._list_cfg("temp_session.group_whitelist")) or "空"
        ts_auto_reply = bool(ts.get("block_auto_reply", False))
        ts_reply_text = ts.get("block_reply_text", "") or "空"

        lines = [
            "【软白名单状态】",
            f"总开关: {'开' if enabled else '关'}",
            f"放行Bot自身: {'开' if allow_self else '关'}",
            f"放行AstrBot管理员: {'开' if allow_admins else '关'}",
            f"放行群管理: {'开' if gc_admin else '关'}",
            "",
            "事件策略:",
            "- 不处理 request / notice / meta_event，避免影响好友申请、群邀请和其他管理插件",
            "- message 按白名单放行，其余拦截",
            "- 非白名单自动回复：仅对方来消息时触发，且每人每天最多一次",
            "",
            f"群聊拦截: {'开' if gc_enabled else '关'}",
            f"- 第一层筛选模式: {gc_filter_mode}",
            f"- 第一层白名单列表: {gc_wl}",
            f"- 第一层黑名单列表: {gc_bl}",
            f"- 第二层（二次筛选）群白名单: {gc_groups}",
            f"- 第二层群成员白名单: {gc_users}",
            f"- 拦截关键词: {gc_keywords_cnt} 条正则",
            "",
            f"好友白名单: {'开' if fc_enabled else '关'}",
            f"- 好友白名单: {fc_list}",
            f"- 非白名单自动回复: {'开' if fc_auto_reply else '关'}",
            f"- 自动回复内容: {fc_reply_text}",
            "",
            f"临时会话白名单: {'开' if ts_enabled else '关'}",
            f"- 临时用户白名单: {ts_users}",
            f"- 临时来源群白名单: {ts_groups}",
            f"- 非白名单自动回复: {'开' if ts_auto_reply else '关'}",
            f"- 自动回复内容: {ts_reply_text}",
        ]
        return "\n".join(lines)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("软白名单状态", alias={"白名单状态"})
    async def white_status(self, event: AiocqhttpMessageEvent):
        """查看软白名单开关、白名单列表和自动回复配置。"""
        yield event.plain_result(self._format_status())

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("加群白")
    async def add_group_white(self, event: AiocqhttpMessageEvent, target: str = ""):
        """加群白 <群号>，将指定群聊加入群聊白名单。"""
        target = self._validate_numeric_id(target, "群号")
        if not target:
            yield event.plain_result("用法: ~加群白 群号  （群号必须为纯数字）")
            return
        ok, data = self._add_template_item(
            "group_chat.whitelist_items",
            "group",
            target,
            {
                "allowed_users": [],
                "block_mode": "共存",
                "group_block_keywords": [],
            },
        )
        if ok:
            yield event.plain_result(f"已加入群白名单: {target}")
        else:
            yield event.plain_result(f"这个群已经在白名单里了: {target}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("删群白")
    async def del_group_white(self, event: AiocqhttpMessageEvent, target: str = ""):
        """删群白 <群号>，将指定群聊移出群聊白名单。"""
        target = self._validate_numeric_id(target, "群号")
        if not target:
            yield event.plain_result("用法: ~删群白 群号  （群号必须为纯数字）")
            return
        ok, data = self._remove_template_item("group_chat.whitelist_items", "group", target)
        if ok:
            yield event.plain_result(f"已移出群白名单: {target}")
        else:
            yield event.plain_result(f"这个群不在白名单里: {target}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("加群员白")
    async def add_group_user_white(self, event: AiocqhttpMessageEvent, target: str = ""):
        """加群员白 <QQ号>，将指定 QQ 加入所有群条目的允许成员列表。"""
        target = self._validate_numeric_id(target, "QQ号")
        if not target:
            yield event.plain_result("用法: ~加群员白 QQ号  （QQ号必须为纯数字）")
            return
        ok, data = self._add_user_to_group_entries(target)
        if ok:
            yield event.plain_result(f"已加入群成员白名单: {target}")
        else:
            yield event.plain_result(f"这个QQ已经在群成员白名单里了，或没有已添加的群号")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("删群员白")
    async def del_group_user_white(self, event: AiocqhttpMessageEvent, target: str = ""):
        """删群员白 <QQ号>，将指定 QQ 移出所有群条目的允许成员列表。"""
        target = self._validate_numeric_id(target, "QQ号")
        if not target:
            yield event.plain_result("用法: ~删群员白 QQ号  （QQ号必须为纯数字）")
            return
        ok, data = self._remove_user_from_group_entries(target)
        if ok:
            yield event.plain_result(f"已移出群成员白名单: {target}")
        else:
            yield event.plain_result(f"这个QQ不在群成员白名单里: {target}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("加好友白")
    async def add_friend_white(self, event: AiocqhttpMessageEvent, target: str = ""):
        """加好友白 <QQ号>，将指定 QQ 加入好友私聊白名单。"""
        target = self._validate_numeric_id(target, "QQ号")
        if not target:
            yield event.plain_result("用法: ~加好友白 QQ号  （QQ号必须为纯数字）")
            return
        ok, data = self._add_to_list_cfg("friend_chat.user_whitelist", target)
        if ok:
            yield event.plain_result(f"已加入好友白名单: {target}")
        else:
            yield event.plain_result(f"这个QQ已经在好友白名单里了: {target}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("删好友白")
    async def del_friend_white(self, event: AiocqhttpMessageEvent, target: str = ""):
        """删好友白 <QQ号>，将指定 QQ 移出好友私聊白名单。"""
        target = self._validate_numeric_id(target, "QQ号")
        if not target:
            yield event.plain_result("用法: ~删好友白 QQ号  （QQ号必须为纯数字）")
            return
        ok, data = self._remove_from_list_cfg("friend_chat.user_whitelist", target)
        if ok:
            yield event.plain_result(f"已移出好友白名单: {target}")
        else:
            yield event.plain_result(f"这个QQ不在好友白名单里: {target}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("加临时白")
    async def add_temp_white(
        self,
        event: AiocqhttpMessageEvent,
        kind: str = "",
        target: str = "",
    ):
        """加临时白 <用户|群> <QQ号|群号>，加入临时会话用户或来源群白名单。"""
        kind = str(kind).strip()
        target = str(target).strip()
        if kind not in {"用户", "群"}:
            yield event.plain_result("用法: ~加临时白 用户 QQ号  或  ~加临时白 群 群号")
            return
        label = "QQ号" if kind == "用户" else "群号"
        target = self._validate_numeric_id(target, label)
        if not target:
            yield event.plain_result(f"用法: ~加临时白 {kind} {label}  （{label}必须为纯数字）")
            return
        key = "temp_session.user_whitelist" if kind == "用户" else "temp_session.group_whitelist"
        ok, data = self._add_to_list_cfg(key, target)
        if ok:
            yield event.plain_result(f"已加入临时{kind}白名单: {target}")
        else:
            yield event.plain_result(f"这个目标已经在临时{kind}白名单里了: {target}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("删临时白")
    async def del_temp_white(
        self,
        event: AiocqhttpMessageEvent,
        kind: str = "",
        target: str = "",
    ):
        """删临时白 <用户|群> <QQ号|群号>，移出临时会话用户或来源群白名单。"""
        kind = str(kind).strip()
        target = str(target).strip()
        if kind not in {"用户", "群"}:
            yield event.plain_result("用法: ~删临时白 用户 QQ号  或  ~删临时白 群 群号")
            return
        label = "QQ号" if kind == "用户" else "群号"
        target = self._validate_numeric_id(target, label)
        if not target:
            yield event.plain_result(f"用法: ~删临时白 {kind} {label}  （{label}必须为纯数字）")
            return
        key = "temp_session.user_whitelist" if kind == "用户" else "temp_session.group_whitelist"
        ok, data = self._remove_from_list_cfg(key, target)
        if ok:
            yield event.plain_result(f"已移出临时{kind}白名单: {target}")
        else:
            yield event.plain_result(f"这个目标不在临时{kind}白名单里: {target}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("好友白回复")
    async def set_friend_reply(
        self,
        event: AiocqhttpMessageEvent,
        mode: str = "",
        text: str = "",
    ):
        """好友白回复 <开|关|设定> [回复内容]，管理好友非白名单自动回复。"""
        mode = str(mode).strip()
        text = str(text).strip()
        if mode in {"开", "关"}:
            self._set_cfg("friend_chat.block_auto_reply", mode == "开")
            yield event.plain_result(f"好友非白名单自动回复已{'开启' if mode == '开' else '关闭'}")
            return
        if mode == "设定":
            if not text:
                yield event.plain_result("用法: ~好友白回复 设定 回复内容")
                return
            self._set_cfg("friend_chat.block_reply_text", text)
            yield event.plain_result(f"好友非白名单自动回复内容已更新:\n{text}")
            return
        yield event.plain_result("用法: ~好友白回复 开|关  或  ~好友白回复 设定 回复内容")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("临时白回复")
    async def set_temp_reply(
        self,
        event: AiocqhttpMessageEvent,
        mode: str = "",
        text: str = "",
    ):
        """临时白回复 <开|关|设定> [回复内容]，管理临时会话非白名单自动回复。"""
        mode = str(mode).strip()
        text = str(text).strip()
        if mode in {"开", "关"}:
            self._set_cfg("temp_session.block_auto_reply", mode == "开")
            yield event.plain_result(f"临时会话非白名单自动回复已{'开启' if mode == '开' else '关闭'}")
            return
        if mode == "设定":
            if not text:
                yield event.plain_result("用法: ~临时白回复 设定 回复内容")
                return
            self._set_cfg("temp_session.block_reply_text", text)
            yield event.plain_result(f"临时会话非白名单自动回复内容已更新:\n{text}")
            return
        yield event.plain_result("用法: ~临时白回复 开|关  或  ~临时白回复 设定 回复内容")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.command("清理退群白", alias={"剔除退群白", "清理群白"})
    async def prune_left_group_white(self, event: AiocqhttpMessageEvent, mode: str = ""):
        """清理退群白 [强制]，剔除群聊白名单和临时来源群白名单中 Bot 已退出的群。"""
        try:
            mode = str(mode).strip()
            joined_group_ids = await self._get_joined_group_ids(event)
            # group_chat 使用 template_list 格式
            group_items = self._cfg("group_chat.whitelist_items", [])
            # temp_session 使用普通 list 格式
            temp_group_whitelist = self._list_cfg("temp_session.group_whitelist")
            tracked_group_count = len(group_items) + len(temp_group_whitelist)
            if not joined_group_ids and tracked_group_count and mode != "强制":
                yield event.plain_result(
                    "清理已取消: 当前平台返回的Bot加入群数为0，但本地仍有群白名单记录。\n"
                    "这可能是适配器接口异常或缓存未就绪。确认Bot确实不在任何群后，可使用: ~清理退群白 强制"
                )
                return
            removed_group, after_group = self._prune_template_group_cfg(
                "group_chat.whitelist_items",
                joined_group_ids,
            )
            removed_temp, after_temp = self._prune_group_cfg(
                "temp_session.group_whitelist",
                joined_group_ids,
            )
        except Exception as e:
            logger.error(f"[soft_whitelist] 清理已退群聊失败: {e}", exc_info=True)
            yield event.plain_result(f"清理失败: {e}")
            return

        total_removed = len(removed_group) + len(removed_temp)
        lines = [
            "【退群白名单清理】",
            f"当前Bot加入群数: {len(joined_group_ids)}",
            f"已剔除群白名单: {len(removed_group)} 个",
            f"- {self._format_removed_ids(removed_group)}",
            f"已剔除临时来源群白名单: {len(removed_temp)} 个",
            f"- {self._format_removed_ids(removed_temp)}",
            f"剩余群白名单: {len(after_group)} 个",
            f"剩余临时来源群白名单: {len(after_temp)} 个",
        ]
        if total_removed == 0:
            lines.append("未发现已退群聊。")
        yield event.plain_result("\n".join(lines))

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100000)
    async def soft_filter(self, event: AiocqhttpMessageEvent):
        try:
            raw_message = getattr(event.message_obj, "raw_message", None)
            if not isinstance(raw_message, dict):
                return

            post_type = str(raw_message.get("post_type", ""))

            if post_type != "message":
                return

            scene = self._get_scene(raw_message)
            if scene is None:
                logger.info(f"[soft_whitelist] 无法识别消息场景，已拦截: {raw_message}")
                event.stop_event()
                return

            # 提前提取 sender_id / group_id，传给 _allow_message_scene 避免内部重复提取
            sender_id = self._sender_id(event, raw_message)
            group_id = self._group_id(event, raw_message)

            if self._allow_message_scene(event, raw_message, scene, sender_id, group_id):
                return

            logger.info(
                f"[soft_whitelist] 已拦截消息 scene={scene}, sender={sender_id}, group={group_id}"
            )

            if self._should_auto_reply_when_blocked(scene, raw_message, sender_id):
                reply_text = self._get_block_reply(scene).strip()
                if reply_text:
                    yield event.plain_result(reply_text)
                    self._mark_auto_reply_sent(scene, sender_id)

            event.stop_event()
        except Exception as e:
            logger.error(f"[soft_whitelist] 拦截异常: {e}", exc_info=True)
