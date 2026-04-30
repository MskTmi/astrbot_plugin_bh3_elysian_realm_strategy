# 基于 AstrBot 的崩坏3 往世乐土攻略查询插件
> 移植自 Mirai 版 [Bh3-ElysianRealm-Strategy](https://github.com/MskTmi/Bh3-ElysianRealm-Strategy)，并补齐了适配 AstrBot 的仓库同步、关键词索引和图片回复能力。

## 功能

- 发送角色关键词即可返回本地攻略图。
- 支持多流派共享关键词。同一关键词命中多个变体时，自动返回最近更新的那一张图。
- `/更新乐土攻略` 会比较更新前后的 git commit，自动识别变动图片并刷新对应角色的 UTC 更新时间。
- 插件根目录的 `elysian-realm-index.json` 作为内置索引模板，运行时会写入 `data` 目录并持续更新。
- 保留 AstrBot 侧的关键词管理命令，方便为新增图片补充触发词。
- `/乐土关键词列表` 默认在群聊中使用单次合并转发发送，合并转发中的每条消息最 10 条攻略；私聊等非群聊场景回退为 txt 文件回复。

## 指令

- `/获取乐土攻略`
	首次克隆攻略仓库并建立本地索引
- `/强制获取乐土攻略`
	删除现有攻略仓库目录并重新浅克隆远端仓库，适用于解决无法正常获取或是更新导致的奇怪问题
- `/更新乐土攻略`
	拉取最新提交，识别本次更新涉及的图片角色，并更新对应时间戳。
	目前执行 `/更新乐土攻略` 后，如有新增图片或新增角色，仍需要使用 `/添加乐土关键词 <图片名> <关键词1,关键词2>` 手动补充关键词。
- `/添加乐土关键词 <图片名> <关键词1,关键词2>`
	为某张攻略图追加关键词
- `/删除乐土关键词 <图片名>`
	删除某张攻略图的关键词配置
- `/乐土关键词列表`
	查看当前索引中的图片名和关键词
- `/RealmCommand list`
	兼容 Mirai 旧命令格式；输出行为与 `/乐土关键词列表` 一致

## 使用示例

```text
用户: /更新乐土攻略
Bot: 更新的角色: Felis_Ultimate, Human

用户: 猫猫乐土
Bot: [自动发送最近更新的那一张猫猫乐土攻略图]

用户: 猫猫普攻流
Bot: [发送 Felis_Attack 图片]
```

## 数据文件

- 插件根目录的 `elysian-realm-index.json` 为随插件分发的默认索引模板
- 本地仓库默认存放在 `data/plugin_data/astrbot_plugin_bh3_elysian_realm_strategy/ElysianRealm-Data`
- 索引文件默认存放在 `data/plugin_data/astrbot_plugin_bh3_elysian_realm_strategy/elysian-realm-index.json`

新版 `elysian-realm-index.json` 结构如下：

```json
{
	"Felis_Attack": {
		"keywords": ["猫猫乐土", "猫猫普攻流"],
		"last_updated": "2024-12-25T10:00:00+00:00"
	},
	"Felis_Ultimate": {
		"keywords": ["猫猫乐土", "猫猫大招流"],
		"last_updated": "2024-12-31T10:00:00+00:00"
	}
}
```

## 插件配置

插件通过 `_conf_schema.json` 暴露了以下分组配置：

- `repository_settings`: 仓库同步配置
- `reply_settings`: 自动回复配置
- `permission_settings`: 权限控制配置

主要配置项如下：

- `repository_settings.repository_url`: 攻略仓库地址
- `repository_settings.repository_proxy_method`: Git 拉取代理方式
- `repository_settings.repository_proxy_custom_url`: 自定义 Git 代理地址
- `reply_settings.enable_private_reply`: 私聊自动回复开关
- `reply_settings.enable_group_reply`: 群聊自动回复开关
- `reply_settings.private_whitelist`: 私聊白名单，按完整 UMO 限制
- `reply_settings.group_whitelist`: 群聊白名单，按完整 UMO 限制
- `permission_settings.admin_whitelist`: 管理员标识列表，支持会话 UMO 或管理员用户的 FriendMessage UMO
- `permission_settings.allow_non_admin_commands`: 是否允许非管理员使用管理指令
- `permission_settings.non_admin_allowed_commands`: 允许非管理员使用的指令列表

Git 代理配置说明：

- `repository_settings.repository_proxy_method=direct`: 直连 GitHub。
- `repository_settings.repository_proxy_method=https://edgeone.gh-proxy.com`: 使用 EdgeOne 代理。
- `repository_settings.repository_proxy_method=https://hk.gh-proxy.com/`: 使用香港代理。
- `repository_settings.repository_proxy_method=https://gh-proxy.com/`: 使用 gh-proxy 代理。
- `repository_settings.repository_proxy_method=https://gh.lk.cc`: 使用 gh.lk.cc 代理。
- `repository_settings.repository_proxy_method=custom`: 使用 `repository_settings.repository_proxy_custom_url` 作为代理前缀。

代理会同时作用于首次获取攻略和后续更新。若本地仓库已存在，插件会在更新前自动同步 origin 远端地址到当前配置。

权限控制规则如下：

- 若平台事件能直接提供管理员身份，则管理员默认可用全部管理指令，非管理员仅可使用 `permission_settings.non_admin_allowed_commands` 中列出的指令。
- 若平台无法提供管理员身份，可以通过 `permission_settings.admin_whitelist` 手动填写管理员会话 UMO，或管理员用户的 FriendMessage UMO；匹配到名单的用户可用全部管理指令。
- 若 `permission_settings.admin_whitelist` 留空且平台也未暴露管理员身份，插件保持升级前的兼容行为，不会额外拦截现有指令。
- 若 `permission_settings.allow_non_admin_commands` 为关闭，则非管理员无法使用任何管理指令。
- `permission_settings.non_admin_allowed_commands` 现使用 `list + options` 展示；只需勾选主指令名即可，对应别名和内部命令标识也会自动生效。若 `permission_settings.allow_non_admin_commands` 为开启且该配置留空，则非管理员默认可使用全部管理指令。配置面板中的 `hint` 会按换行逐条说明各指令用途。

本地持久化目录固定为 `data/plugin_data/astrbot_plugin_bh3_elysian_realm_strategy`，攻略仓库目录固定为 `ElysianRealm-Data`。

## 更新日志

更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 未来规划

- 在图片库中补充和维护索引表，减少手动维护关键词的成本。
- 结合图片库索引表，在后续版本中实现更新仓库后自动同步关键词，尽量减少手动执行 `/添加乐土关键词 <图片名> <关键词1,关键词2>` 的场景。


## 开发参考

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [原始 Mirai 插件](https://github.com/MskTmi/Bh3-ElysianRealm-Strategy)
