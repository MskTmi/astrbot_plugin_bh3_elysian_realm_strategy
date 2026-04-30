from __future__ import annotations

from pathlib import Path
import traceback

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from .elysian_realm_service import (
        ElysianRealmService,
        GitCommandError,
        split_keywords,
    )
except ImportError:
    from elysian_realm_service import (  # type: ignore[no-redef]
        ElysianRealmService,
        GitCommandError,
        split_keywords,
    )


PLUGIN_NAME = "astrbot_plugin_bh3_elysian_realm_strategy"
INDEX_FILE_NAME = "elysian-realm-index.json"
LIST_FORWARD_CHUNK_SIZE = 10
DEFAULT_REPOSITORY_URL = "https://github.com/MskTmi/ElysianRealm-Data.git"
REPOSITORY_PROXY_PRESETS: dict[str, str] = {
    "direct": "",
    "edgeone": "https://edgeone.gh-proxy.com",
    "hk": "https://hk.gh-proxy.com/",
    "gh_proxy": "https://gh-proxy.com/",
    "gh_lk": "https://gh.lk.cc",
    "custom": "",
}
COMMAND_ALIASES: dict[str, set[str]] = {
    "fetch_strategy": {"获取乐土攻略", "GetStrategy", "fetch_strategy"},
    "force_fetch_strategy": {
        "强制获取乐土攻略",
        "ForceGetStrategy",
        "force_fetch_strategy",
    },
    "update_strategy": {"更新乐土攻略", "UpdateStrategy", "update_strategy"},
    "add_strategy_keywords": {"添加乐土关键词", "RealmAdd", "add_strategy_keywords"},
    "legacy_realm_command": {
        "RealmCommand",
        "realmcommand",
        "乐土指令",
        "legacy_realm_command",
    },
    "remove_strategy_keywords": {
        "删除乐土关键词",
        "RealmRemove",
        "remove_strategy_keywords",
    },
    "list_strategy_keywords": {"乐土关键词列表", "RealmList", "list_strategy_keywords"},
}


@register(
    PLUGIN_NAME,
    "MskTim",
    "崩坏3往世乐土攻略插件",
    "0.2.0",
)
class Bh3ElysianRealmStrategyPlugin(Star):
    # AstrBot 乐土攻略插件入口，负责命令分发与消息回复
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        # 初始化插件配置、存储路径和攻略服务
        super().__init__(context)
        self.config = config or {}
        self.service: ElysianRealmService | None = None
        self.service_error_message: str | None = None
        logger.info("乐土攻略重启中...")

        plugin_root = Path(__file__).resolve().parent
        data_path = Path(get_astrbot_data_path())
        plugin_name = getattr(self, "name", None) or PLUGIN_NAME
        storage_dir = data_path / "plugin_data" / plugin_name
        repo_directory_name = "ElysianRealm-Data"
        repository_url = str(
            self._get_config_value(
                "repository_settings",
                "repository_url",
                default=DEFAULT_REPOSITORY_URL,
                legacy_key="repository_url",
            )
            or DEFAULT_REPOSITORY_URL
        ).strip()
        repository_proxy_method = str(
            self._get_config_value(
                "repository_settings",
                "repository_proxy_method",
                default="direct",
                legacy_key="repository_proxy_method",
            )
            or "direct"
        ).strip()
        repository_proxy_custom_url = str(
            self._get_config_value(
                "repository_settings",
                "repository_proxy_custom_url",
                default="",
                legacy_key="repository_proxy_custom_url",
            )
            or ""
        ).strip()
        strategies_template_path = plugin_root / INDEX_FILE_NAME
        repository_url = self._build_repository_url(
            repository_url,
            repository_proxy_method,
            repository_proxy_custom_url,
        )

        try:
            self.service = ElysianRealmService(
                storage_dir=storage_dir,
                repo_directory_name=repo_directory_name,
                repository_url=repository_url,
                template_strategies_path=strategies_template_path,
            )
            self.service.load()
        except Exception as exc:
            self.service = None
            self.service_error_message = self._build_service_error_message(exc)
            self._log_unexpected_error("初始化乐土攻略服务", exc)
        self.enable_private_reply = self._get_bool_config(
            "reply_settings",
            "enable_private_reply",
            default=False,
            legacy_key="enable_private_reply",
        )
        self.enable_group_reply = self._get_bool_config(
            "reply_settings",
            "enable_group_reply",
            default=True,
            legacy_key="enable_group_reply",
        )
        self.allow_non_admin_commands = self._get_bool_config(
            "permission_settings",
            "allow_non_admin_commands",
            default=True,
            legacy_key="allow_non_admin_commands",
        )
        self.private_whitelist = self._parse_whitelist(
            self._get_config_value(
                "reply_settings",
                "private_whitelist",
                default="",
                legacy_key="private_whitelist",
            )
        )
        self.group_whitelist = self._parse_whitelist(
            self._get_config_value(
                "reply_settings",
                "group_whitelist",
                default="",
                legacy_key="group_whitelist",
            )
        )
        self.admin_whitelist = self._parse_whitelist(
            self._get_config_value(
                "permission_settings",
                "admin_whitelist",
                default=[],
                legacy_key="admin_whitelist",
            )
        )
        self.non_admin_allowed_commands = self._parse_command_allowlist(
            self._get_config_value(
                "permission_settings",
                "non_admin_allowed_commands",
                default=[],
                legacy_key="non_admin_allowed_commands",
            )
        )

    async def initialize(self):
        # 在插件启用时刷新本地索引状态
        logger.info("乐土攻略已启用！感觉如何？")
        service = self.service
        if service is None:
            logger.error(self.service_error_message or "乐土攻略服务未成功初始化")
            return

        try:
            if service.is_git_repository():
                service.scan_images()
                service.sync_discovered_images()
        except Exception as exc:
            self.service_error_message = self._build_service_error_message(exc)
            self._log_unexpected_error("初始化乐土攻略索引", exc)

    async def terminate(self):
        # 在插件关闭时记录结束日志
        logger.info("至此，乐土攻略被关闭了")

    @filter.command("获取乐土攻略", alias={"GetStrategy"})
    async def fetch_strategy(self, event: AstrMessageEvent):
        """拉取乐土攻略仓库并建立本地索引

        示例: /获取乐土攻略
        """
        event.stop_event()
        permission_denied = self._ensure_command_access(event, "fetch_strategy")
        if permission_denied is not None:
            yield permission_denied
            return
        service_unavailable = self._ensure_service_available(event)
        if service_unavailable is not None:
            yield service_unavailable
            return
        yield event.plain_result("正在获取乐土攻略并建立索引，请稍候...")
        try:
            result = await self.service.clone_repository()  # type: ignore[union-attr]
        except GitCommandError as exc:
            logger.error(f"获取乐土攻略失败: {exc}")
            yield event.plain_result(f"获取乐土攻略失败: {exc}")
            return
        except Exception as exc:
            yield self._handle_command_exception(event, "获取乐土攻略", exc)
            return

        if result["already_exists"]:
            yield event.plain_result(
                "本地已存在攻略仓库，无需重复获取"
                f" 当前共索引 {result['image_count']} 张攻略图"
            )
            return

        yield event.plain_result(
            "乐土攻略获取完成"
            f" 当前共索引 {result['image_count']} 张攻略图，可直接发送关键词查询"
        )

    @filter.command("更新乐土攻略", alias={"UpdateStrategy"})
    async def update_strategy(self, event: AstrMessageEvent):
        """更新本地攻略仓库并自动记录图片更新时间

        示例: /更新乐土攻略
        """
        event.stop_event()
        permission_denied = self._ensure_command_access(event, "update_strategy")
        if permission_denied is not None:
            yield permission_denied
            return
        service_unavailable = self._ensure_service_available(event)
        if service_unavailable is not None:
            yield service_unavailable
            return
        yield event.plain_result("正在更新乐土攻略并刷新索引，请稍候...")
        try:
            result = await self.service.update_repository()  # type: ignore[union-attr]
        except GitCommandError as exc:
            logger.error(f"更新乐土攻略失败: {exc}")
            yield event.plain_result(f"更新乐土攻略失败: {exc}")
            return
        except Exception as exc:
            yield self._handle_command_exception(event, "更新乐土攻略", exc)
            return

        if result["already_up_to_date"]:
            yield event.plain_result("已经是最新了")
            return

        updated_names = result["updated_names"]
        if not updated_names:
            yield event.plain_result(
                "仓库更新完成，但本次提交中没有检测到攻略图片变更"
            )
            return

        yield event.plain_result("更新的角色: " + ", ".join(updated_names))

    @filter.command("强制获取乐土攻略", alias={"ForceGetStrategy"})
    async def force_fetch_strategy(self, event: AstrMessageEvent):
        """删除现有攻略仓库并重新浅克隆远端仓库

        示例: /强制获取乐土攻略
        """
        event.stop_event()
        permission_denied = self._ensure_command_access(event, "force_fetch_strategy")
        if permission_denied is not None:
            yield permission_denied
            return
        service_unavailable = self._ensure_service_available(event)
        if service_unavailable is not None:
            yield service_unavailable
            return
        yield event.plain_result("正在强制重拉乐土攻略仓库并重建索引，请稍候...")
        try:
            result = await self.service.force_clone_repository()  # type: ignore[union-attr]
        except GitCommandError as exc:
            logger.error(f"强制获取乐土攻略失败: {exc}")
            yield event.plain_result(f"强制获取乐土攻略失败: {exc}")
            return
        except Exception as exc:
            yield self._handle_command_exception(event, "强制获取乐土攻略", exc)
            return

        yield event.plain_result(
            "乐土攻略仓库已强制重拉完成"
            f" 当前共索引 {result['image_count']} 张攻略图，可直接发送关键词查询"
        )

    @filter.command("添加乐土关键词", alias={"RealmAdd"})
    async def add_strategy_keywords(
        self,
        event: AstrMessageEvent,
        image_name: str,
        keywords: str,
    ):
        """为攻略图添加关键词，多个关键词使用逗号分隔

        示例: /添加乐土关键词 Felis 猫猫乐土,菲利丝乐土
        """
        event.stop_event()
        permission_denied = self._ensure_command_access(event, "add_strategy_keywords")
        if permission_denied is not None:
            yield permission_denied
            return
        service_unavailable = self._ensure_service_available(event)
        if service_unavailable is not None:
            yield service_unavailable
            return
        try:
            keyword_list = split_keywords(keywords)
            if not keyword_list:
                yield event.plain_result(
                    "请至少提供一个关键词，多个关键词可用逗号分隔"
                )
                return

            entry = self.service.store.add_keywords(image_name, keyword_list)  # type: ignore[union-attr]
            yield event.plain_result(
                f"已更新 {image_name} 的关键词: {', '.join(entry.keywords)}"
            )
        except Exception as exc:
            yield self._handle_command_exception(event, "添加乐土关键词", exc)

    @filter.command("RealmCommand", alias={"realmcommand", "乐土指令"})
    async def legacy_realm_command(
        self,
        event: AstrMessageEvent,
        action: str = "",
        image_name: str = "",
        keywords: str = "",
    ):
        """兼容 Mirai 版本的 /RealmCommand add|del|list 指令格式

        示例:
        /RealmCommand add Felis 猫猫乐土,菲利丝乐土
        /RealmCommand del Felis
        /RealmCommand list
        """
        event.stop_event()
        permission_denied = self._ensure_command_access(event, "legacy_realm_command")
        if permission_denied is not None:
            yield permission_denied
            return
        service_unavailable = self._ensure_service_available(event)
        if service_unavailable is not None:
            yield service_unavailable
            return
        try:
            normalized_action = action.strip().lower()

            if normalized_action in {"add", "添加"}:
                if not image_name or not keywords.strip():
                    yield event.plain_result(
                        "用法: /RealmCommand add <图片名> <关键词1,关键词2>"
                    )
                    return

                keyword_list = split_keywords(keywords)
                if not keyword_list:
                    yield event.plain_result(
                        "请至少提供一个关键词，多个关键词可用逗号分隔"
                    )
                    return

                entry = self.service.store.add_keywords(image_name, keyword_list)  # type: ignore[union-attr]
                yield event.plain_result(
                    f"已更新 {image_name} 的关键词: {', '.join(entry.keywords)}"
                )
                return

            if normalized_action in {"del", "remove", "删除"}:
                if not image_name:
                    yield event.plain_result("用法: /RealmCommand del <图片名>")
                    return

                removed = self.service.store.remove_entry(image_name)  # type: ignore[union-attr]
                if removed:
                    yield event.plain_result(f"已删除 {image_name} 的关键词配置")
                    return
                yield event.plain_result(f"没有找到名为 {image_name} 的攻略配置")
                return

            if normalized_action in {"list", "列表"}:
                yield await self._keyword_list_result(event)
                return

            yield event.plain_result(
                "支持的用法: /RealmCommand add <图片名> <关键词1,关键词2> | "
                "/RealmCommand del <图片名> | /RealmCommand list"
            )
        except Exception as exc:
            yield self._handle_command_exception(event, "RealmCommand", exc)

    @filter.command("删除乐土关键词", alias={"RealmRemove"})
    async def remove_strategy_keywords(self, event: AstrMessageEvent, image_name: str):
        """删除某个攻略图的关键词配置

        示例: /删除乐土关键词 Felis
        """
        event.stop_event()
        permission_denied = self._ensure_command_access(
            event, "remove_strategy_keywords"
        )
        if permission_denied is not None:
            yield permission_denied
            return
        service_unavailable = self._ensure_service_available(event)
        if service_unavailable is not None:
            yield service_unavailable
            return
        try:
            removed = self.service.store.remove_entry(image_name)  # type: ignore[union-attr]
            if removed:
                yield event.plain_result(f"已删除 {image_name} 的关键词配置")
                return
            yield event.plain_result(f"没有找到名为 {image_name} 的攻略配置")
        except Exception as exc:
            yield self._handle_command_exception(event, "删除乐土关键词", exc)

    @filter.command("乐土关键词列表", alias={"RealmList"})
    async def list_strategy_keywords(self, event: AstrMessageEvent):
        """列出全部攻略关键词

        示例: /乐土关键词列表
        """
        event.stop_event()
        permission_denied = self._ensure_command_access(event, "list_strategy_keywords")
        if permission_denied is not None:
            yield permission_denied
            return
        service_unavailable = self._ensure_service_available(event)
        if service_unavailable is not None:
            yield service_unavailable
            return
        try:
            yield await self._keyword_list_result(event)
        except Exception as exc:
            yield self._handle_command_exception(event, "乐土关键词列表", exc)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_keyword_message(self, event: AstrMessageEvent):
        # 监听普通消息，并在命中关键词时返回对应攻略图
        try:
            message = event.message_str.strip()
            if not message or message.startswith("/"):
                return
            if not self._should_reply(event):
                return
            if self.service is None:
                return

            try:
                match = await self.service.resolve_keyword(message)
            except GitCommandError as exc:
                logger.error(f"解析乐土关键词失败: {exc}")
                return

            if match is None:
                if self.service.has_keyword(message):
                    event.stop_event()
                    if not self.service.is_git_repository():
                        yield event.plain_result(
                            "未找到本地攻略仓库，请先执行:\n/获取乐土攻略"
                        )
                    else:
                        yield event.plain_result(
                            "已匹配到关键词，但未找到对应图片文件，请先执行:\n/更新乐土攻略"
                        )
                return

            event.stop_event()
            yield event.image_result(str(match.image_path))
        except Exception as exc:
            self._log_unexpected_error("处理乐土关键词消息", exc)

    async def _keyword_list_result(self, event: AstrMessageEvent):
        # 根据会话类型生成关键词列表消息或导出文件
        blocks = self.service.store.format_entry_blocks()
        if not blocks:
            return event.plain_result("当前没有任何乐土攻略配置")

        if self._get_message_type(self._get_umo(event)) == "GroupMessage":
            return event.chain_result(
                [Comp.Nodes(self._build_keyword_forward_nodes(blocks))]
            )

        file_path = self._write_keyword_list_file(blocks)
        return event.chain_result(
            [Comp.File(file=str(file_path), name="乐土关键词列表.txt")]
        )

    def _build_keyword_forward_nodes(self, blocks: list[str]) -> list[Comp.Node]:
        # 将关键词列表按固定数量拆分为合并转发节点
        nodes: list[Comp.Node] = []
        for index in range(0, len(blocks), LIST_FORWARD_CHUNK_SIZE):
            chunk = blocks[index : index + LIST_FORWARD_CHUNK_SIZE]
            nodes.append(self._create_forward_node(nodes, chunk))

        return nodes

    def _create_forward_node(
        self, existing_nodes: list[Comp.Node], blocks: list[str]
    ) -> Comp.Node:
        # 构造单条合并转发节点内容
        content = "\n\n".join(blocks)
        node_index = len(existing_nodes) + 1
        return Comp.Node(
            uin="0",
            name=f"乐土关键词列表 {node_index}",
            content=[Comp.Plain(content)],
        )

    def _write_keyword_list_file(self, blocks: list[str]) -> Path:
        # 将关键词列表导出为文本文件供私聊场景发送
        export_dir = self.service.storage_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        file_path = export_dir / "乐土关键词列表.txt"
        file_path.write_text("\n\n".join(blocks), encoding="utf-8")
        return file_path

    def _should_reply(self, event: AstrMessageEvent) -> bool:
        # 根据消息类型、开关和白名单决定是否执行自动回复
        umo = self._get_umo(event)
        message_type = self._get_message_type(umo)

        if message_type == "GroupMessage":
            if not self.enable_group_reply:
                return False
            if self.group_whitelist and umo not in self.group_whitelist:
                return False
            return True

        if not self.enable_private_reply:
            return False

        if self.private_whitelist and umo not in self.private_whitelist:
            return False
        return True

    def _ensure_command_access(self, event: AstrMessageEvent, command_key: str):
        # 在执行管理指令前检查当前用户是否具备权限
        if self._can_use_command(event, command_key):
            return None

        display_name = self._get_command_display_name(command_key)
        return event.plain_result(
            f"指令 {display_name} 仅管理员可用"
            "如需开放给非管理员，请将该指令加入权限配置中的允许非管理员使用的指令列表"
        )

    def _ensure_service_available(self, event: AstrMessageEvent):
        # 确认攻略服务可用，否则返回统一的失败提示
        if self.service is not None:
            return None
        return event.plain_result(
            self.service_error_message or "乐土攻略服务暂时不可用，请稍后重试"
        )

    def _handle_command_exception(
        self,
        event: AstrMessageEvent,
        action_name: str,
        exc: Exception,
    ):
        # 处理命令执行中的未预期异常并返回友好提示
        self.service_error_message = self._build_service_error_message(exc)
        self._log_unexpected_error(action_name, exc)
        return event.plain_result(f"{action_name}失败: 发生未预期错误，请稍后重试")

    def _build_service_error_message(self, exc: Exception) -> str:
        # 构造面向用户展示的服务异常摘要
        return f"乐土攻略服务暂时不可用: {exc}"

    def _log_unexpected_error(self, action_name: str, exc: Exception) -> None:
        # 记录包含堆栈信息的异常日志，便于排查
        logger.error(f"{action_name}时发生未处理异常: {exc}\n{traceback.format_exc()}")

    def _can_use_command(self, event: AstrMessageEvent, command_key: str) -> bool:
        # 综合平台权限、管理员配置和白名单判断指令可用性
        admin_status = self._get_native_admin_status(event)
        if self._is_configured_admin(event):
            return True
        if admin_status is True:
            return True
        if admin_status is False or self.admin_whitelist:
            return self._is_non_admin_command_allowed(command_key)
        return True

    def _is_configured_admin(self, event: AstrMessageEvent) -> bool:
        # 判断当前事件是否命中手动配置的管理员标识
        if not self.admin_whitelist:
            return False
        return bool(self._get_admin_umo_candidates(event) & self.admin_whitelist)

    def _get_admin_umo_candidates(self, event: AstrMessageEvent) -> set[str]:
        # 收集可用于管理员匹配的会话 UMO 与用户 UMO 候选值
        candidates: set[str] = set()

        current_umo = self._get_umo(event)
        if current_umo:
            candidates.add(current_umo)

        platform_name = self._get_platform_name(current_umo)
        for sender_id in self._get_sender_ids(event):
            if platform_name:
                candidates.add(f"{platform_name}:FriendMessage:{sender_id}")

        return candidates

    def _get_platform_name(self, umo: str) -> str:
        # 从 UMO 中提取平台名称部分
        parts = umo.split(":", 2)
        if parts:
            return parts[0].strip()
        return ""

    def _get_sender_ids(self, event: AstrMessageEvent) -> set[str]:
        # 尽量从事件及其嵌套对象中提取发送者标识
        sender_ids: set[str] = set()

        direct_values = (
            getattr(event, "sender_id", None),
            getattr(event, "user_id", None),
            getattr(event, "uid", None),
            getattr(event, "uin", None),
        )
        for value in direct_values:
            self._append_non_empty(sender_ids, value)

        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                self._append_non_empty(sender_ids, getter())
            except TypeError:
                pass

        nested_objects = [
            getattr(event, "sender", None),
            getattr(event, "message_obj", None),
            getattr(getattr(event, "message_obj", None), "sender", None),
            getattr(getattr(event, "message_obj", None), "member", None),
            getattr(getattr(event, "message_obj", None), "author", None),
        ]
        nested_attrs = ("sender_id", "user_id", "id", "uid", "uin", "qq", "member_id")
        for candidate in nested_objects:
            if candidate is None:
                continue
            for attr_name in nested_attrs:
                self._append_non_empty(sender_ids, getattr(candidate, attr_name, None))

        return sender_ids

    def _append_non_empty(self, values: set[str], value: object) -> None:
        # 将非空文本值追加到集合中
        text = str(value or "").strip()
        if text:
            values.add(text)

    def _is_non_admin_command_allowed(self, command_key: str) -> bool:
        # 判断非管理员是否可使用指定管理指令
        if not self.allow_non_admin_commands:
            return False

        if not self.non_admin_allowed_commands:
            return True

        aliases = COMMAND_ALIASES.get(command_key, {command_key})
        return any(
            self._normalize_command_name(alias) in self.non_admin_allowed_commands
            for alias in aliases
        )

    def _get_command_display_name(self, command_key: str) -> str:
        # 返回用于提示用户的指令显示名称
        aliases = COMMAND_ALIASES.get(command_key)
        if not aliases:
            return command_key
        for alias in aliases:
            if any(ord(char) > 127 for char in alias):
                return alias
        return next(iter(aliases))

    def _get_native_admin_status(self, event: AstrMessageEvent) -> bool | None:
        # 尝试从平台事件对象中读取原生管理员状态
        direct_attrs = (
            "is_admin",
            "is_owner",
            "is_master",
            "is_superuser",
            "is_admin_user",
        )
        nested_objects = [
            getattr(event, "sender", None),
            getattr(event, "message_obj", None),
            getattr(getattr(event, "message_obj", None), "sender", None),
            getattr(getattr(event, "message_obj", None), "member", None),
            getattr(getattr(event, "message_obj", None), "author", None),
        ]

        for attr_name in direct_attrs:
            status = self._coerce_admin_flag(getattr(event, attr_name, None))
            if status is not None:
                return status

        for candidate in nested_objects:
            if candidate is None:
                continue

            for attr_name in direct_attrs:
                status = self._coerce_admin_flag(getattr(candidate, attr_name, None))
                if status is not None:
                    return status

            role = getattr(candidate, "role", None)
            if isinstance(role, str):
                normalized = role.strip().lower()
                if normalized in {"admin", "administrator", "owner"}:
                    return True
                if normalized in {"member", "user", "guest"}:
                    return False

        return None

    def _coerce_admin_flag(self, value: object) -> bool | None:
        # 将不同类型的管理员标记值规范化为布尔结果
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "admin", "owner", "master"}:
                return True
            if normalized in {"0", "false", "no", "off", "member", "user", "guest"}:
                return False
        return None

    def _parse_command_allowlist(self, raw_value: object) -> set[str]:
        # 将指令白名单配置解析为归一化后的名称集合
        values: set[str] = set()

        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    command = item.get("command")
                    if isinstance(command, str) and command.strip():
                        values.add(command.strip())
                    continue

                text = str(item).strip()
                if text:
                    values.add(text)

            return {
                self._normalize_command_name(value) for value in values if value.strip()
            }

        values = self._parse_whitelist(raw_value)
        return {
            self._normalize_command_name(value) for value in values if value.strip()
        }

    def _normalize_command_name(self, value: str) -> str:
        # 规范化指令名称，便于别名统一比较
        return value.strip().lower()

    def _get_umo(self, event: AstrMessageEvent) -> str:
        # 读取事件的统一消息来源标识 UMO
        return str(getattr(event, "unified_msg_origin", "") or "").strip()

    def _get_message_type(self, umo: str) -> str:
        # 从 UMO 中提取消息类型字段
        parts = umo.split(":", 2)
        if len(parts) >= 2:
            return parts[1].strip()
        return ""

    def _get_config_value(
        self,
        *keys: str,
        default: object,
        legacy_key: str | None = None,
    ) -> object:
        # 优先读取新的 object 分组配置，并兼容旧版平铺配置键名
        current: object = self.config
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]

        if current is not None:
            return current
        if legacy_key is not None:
            return self.config.get(legacy_key, default)
        return default

    def _get_bool_config(
        self,
        *keys: str,
        default: bool,
        legacy_key: str | None = None,
    ) -> bool:
        # 将布尔配置项解析为最终布尔值
        value = self._get_config_value(*keys, default=default, legacy_key=legacy_key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _parse_whitelist(self, raw_value: object) -> set[str]:
        # 将白名单配置解析为去重后的字符串集合
        if isinstance(raw_value, list):
            return {str(token).strip() for token in raw_value if str(token).strip()}
        if isinstance(raw_value, str):
            value = raw_value.strip()
            return {value} if value else set()
        return set()

    def _build_repository_url(
        self,
        repository_url: str,
        proxy_method: str,
        custom_proxy_url: str,
    ) -> str:
        # 根据代理配置生成最终用于 git 操作的仓库地址
        base_url = repository_url.strip() or DEFAULT_REPOSITORY_URL
        raw_method = proxy_method.strip()
        normalized_method = raw_method.lower() or "direct"

        if normalized_method == "custom":
            proxy_prefix = custom_proxy_url.strip()
        elif raw_method.startswith(("http://", "https://")):
            proxy_prefix = raw_method
        else:
            proxy_prefix = REPOSITORY_PROXY_PRESETS.get(normalized_method, "")

        proxy_prefix = proxy_prefix.strip()
        if not proxy_prefix:
            return base_url

        return f"{proxy_prefix.rstrip('/')}/{base_url.lstrip('/')}"
