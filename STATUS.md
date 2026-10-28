# 当前状态

更新：2026-10-28。当前技术路线为 MaiBot 1.3.15、统一 QQ 连接器 1.1.8、SnowLuma 接入及 DoraYmon 自有用餐与管理模块。

已实现餐饮候选筛选、带来源与日期的菜单资料、门店资料整合、Windows 桌面后台、按发送者鉴权的管理命令、分群回复参数、启动恢复脚本及合成聊天回放。具体配置见 [运维说明](docs/runbook.md)。

在本次源码副本上完成：

- Ruff 检查与格式检查通过。
- pytest：265 项通过；上游 Pydantic 接口产生 14 条弃用提示。
- 回放：10 个合成场景格式正确。
- 离线 wheel 构建通过。

这些结果验证离线实现及打包，不代表部署者的模型、QQ 账号、地图服务或长期运行已通过验收。需要自行配置实例及资料库；来源缺项、分店身份、菜单现价和营业状态分别核对。

历史自建路线见 [foundation](https://github.com/Mootra/DoraYmon/tree/archive/foundation-20261028)，精简 OneBot 路线见 [slim](https://github.com/Mootra/DoraYmon/tree/archive/slim-20261028)。
