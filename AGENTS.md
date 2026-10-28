# DoraYmon 协作规则

- 以本 WSL 检出为开发源；`/mnt/d/Code/QQ-DoraYmon/DoraYmon` 只用于带真实 `.env` 的 QQ 沙箱验收，不在那里开发。
- 开工先读 `docs/private/STATUS.md`，再读任务相关设计；协作偏好见 `docs/private/project_goal.md`。无本机私有文档的检出先读 `docs/README.md` 并核对代码，不推断运行状态。
- 本规则供项目内各 agent 共用；`CLAUDE.md` 只指向此处。文档职责见 `docs/README.md`，完成证据集中在 `docs/private/completed.md`，历史流水进入 `docs/private/archive/`，不再复制成多个当前入口。
- 项目是插件化 Python 助手（官方 QQ botpy、QQ 账号 OneBot、飞书接入中 + DeepSeek + SQLite），消息链路和各层职责见 `docs/ai_project_context.md`。保留目标、接口、隐私边界和验收标准；飞书事件源尚未接通，不把离线通过当作平台已可用。
- 默认由当前 agent 在单一工作区按依赖推进；计划中的 A/B 仅是功能模块编号。只有用户明确要求并行代理工作时，才安排代理分工和文件边界。
- 用户要求实现、修复、继续或持续推进时，可在当前授权范围内连续完成多个相邻步骤；每个连贯批次后运行相称测试并更新 `docs/private/STATUS.md`，不必因每个微小步骤暂停。
- 修改前简述当前结构、改动点和取舍，完成后说明结果和验证。只有用户明确要求学习、练习或先自己动手时，才保留教学检查点。
- 本地提交沿用已有授权，按可独立验证的改动批次组织；不要求每个编号独立提交。记录实际提交号，不把未提交改动写成已提交。
- 遇到 `STATUS.md` 标出的未决产品选择、需要真实聊天数据/私有资源、公开发布、推送或会扩大范围的动作时停止并询问；连续推进不得替用户决定这些事项。
- 离线测试使用 `.venv/bin/python -m unittest discover -s tests -t .`。测试中 DeepSeek 和 QQ API 必须 mock；不读取 `.env`、数据库、日志、真实聊天内容、`skills/private/` 或 `resources/private/`。
- 涉及存储的离线测试只使用临时库和合成数据；真实模型调用和 QQ 沙箱验收另行取得授权，`.env` 由用户维护。
- `docs/private/` 被 Git 忽略，状态更新属于本机记录；不要把它误报为已提交或可由 Git 恢复。大幅改写计划前保留本地备份，不强制加入 Git。
- 状态、完成索引和带日期审查分别记录当前任务、能力证据与发现；原样归档中的旧待办不是新指令。记录测试、真实模型评阅、平台往返和持续运行各自的边界。
