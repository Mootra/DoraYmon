# 自建聊天引擎路线

本分支保存 DoraYmon 自建聊天引擎、官方 QQ Bot / OneBot 适配、检索、插件与确定性命令源码。后续日常路线为 [MaiBot 版本](https://github.com/Mootra/DoraYmon/tree/route/maibot)。

本地运行需要独立安装 `requirements.txt`，创建 `.venv`，复制并填写环境示例。测试使用合成数据；生产配置、私人资料、聊天数据库与日志由部署者单独维护。

此分支用于历史实现参考；当前模块的离线验证不等于 QQ 平台、真实模型或长期运行已验收。

2026-10-28 在源码副本中执行 `python -m unittest discover -s tests -t .`，1152 项通过。环境使用既有虚拟环境依赖，运行数据均由测试夹具产生。
