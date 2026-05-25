# astrbot_plugin_soft_whitelist

高优先级软白名单插件。

## 当前策略
- 不处理 `request / notice / meta_event`，避免影响好友申请、群邀请和其他管理插件
- `message` 按白名单放行，其余拦截
- 非白名单的好友私聊 / 临时会话可自动回复
- 自动回复仅在收到对方消息时触发，且按对方QQ每天最多一次
- AstrBot 主配置里的 `admins_id` 可无视白名单
