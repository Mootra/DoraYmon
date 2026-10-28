# 配置与运维

所有命令从 /home/mootra/projects/DoraYmon-mai 执行，使用仓库的 Python 3.13 虚拟环境。当前 QQ 接入端为 SnowLuma，Windows 自动检查与 WSL 用户服务指向同一个 MaiBot 后端；精确运行状态见 STATUS 的 QQ-TRANSPORT-01。

## SnowLuma 常驻接入

Windows 运行目录为 C:\Users\31738\DoraYmon-SnowLuma，使用固定的官方 v2.2.0 Windows x64 完整版。运行脚本模板在 deploy/snowluma/windows；实际 integration.json、runtime-profile.json、加密管理凭据和 QQ 数据不入库。脚本需要 Windows PowerShell、WSL 以及装有 aiohttp 的 Python。

SnowLuma 自动注入关闭，全局 OneBot 网络端点为空。监督脚本通过官方管理 API 查询进程，只加载 integration.json 指定的机器人账号；机器人必须先在桌面 QQ 登录，日常使用的其他账号不会被加载。账号匹配后才执行只读 OneBot 探针并启动 MaiBot。WebUI 仅监听 127.0.0.1:5099，OneBot WebSocket 只监听 Windows 的 WSL 接口，保留 token 鉴权。

注入前按进程的 account.uin 匹配本地 QQ 登录身份，注入后按 Hook 报告的 uin 匹配；已有连接身份优先，避免过期本地记录选中其他账号。check-runtime.ps1 使用合成进程快照验证这条筛选规则，不连接真实 QQ。

```powershell
Set-Location "$env:USERPROFILE\DoraYmon-SnowLuma"
.\runtime.ps1 -Action Status
.\runtime.ps1 -Action Start
.\runtime.ps1 -Action Stop
```

新计划任务 DoraYmon-SnowLuma 使用当前 Windows 用户的登录触发器和每分钟检查，通过 runtime-hidden.vbs 隐藏运行。waiting_bot_login 表示需要在 QQ 登录指定机器人账号，不会转而接管其他账号。runtime-health.json 只含进程数、阶段、鉴权与后端状态，不含消息正文、账号标识和凭据。原启动快捷方式也转入该运行目录。

WSL 仍使用 doraymon-maibot-test.service，启动命令的接入文件改为 /mnt/c/Users/31738/DoraYmon-SnowLuma/integration.json。现有 CLI 的 --napcat 参数沿用文件接口 wsUrl/wsToken，也适用于 SnowLuma；run 只同步连接地址与插件代码，保留实例中 WebUI 修改过的模型和策略配置。请勿将 token 写入命令行 URL。

回退前停止新入口；运行 Windows 目录中的 rollback.ps1 可恢复保存的服务单元、连接器配置、启动脚本和 NapCat 监督任务。恢复后仍需单独核对 QQ 登录状态，不把恢复配置视为消息投递成功。备份位置见 STATUS。

以下 NapCat 配置与历史试运行流程保留用于回退。当前是否在线以及验收范围以 STATUS 为准。

## 桌面后台与管理员命令

本机已安装的 DoraYmon 可从桌面或开始菜单打开。关闭窗口会隐藏到托盘；托盘的“退出面板”只退出管理程序。机器人由原 SnowLuma 监督任务和 WSL 用户服务管理。“停止并关闭自启”才会停用机器人自动运行。

“设置 → 群聊回复”先选择具体群号，再调整该群的 @ 优先回复、平时与连续对话活跃度、连续对话时长。群列表来自已放行群及已保存配置，保存只修改所选群，不扩大聊天访问范围。切换群聊保留各群未保存的输入；“重新读取”舍弃当前群的未保存输入。首次迁移将原共享值复制给已放行群，本机当前为 2% / 50% / 5 分钟。活跃度是上游频率系数，不是逐条回复概率。回复生成期间开始增强，成功发送后重新计时；普通消息不会续期，时长为 0 时仅关闭该群增强。私聊保持原回复行为。保存会短暂重启聊天后端，并按所选群回读确认“已生效”；未确认时重新连接后读取设置。

管理插件的 reply_config_path 指向实例 config/bot_config.toml，reply_overlay_path 指向当前服务 --local 目录内的 bot_config.toml；安装器可通过 --local 指定。reply-settings.json 位于实例 data/plugins/mootra.doraymon-admin/，包含 defaults 和以群号为键的 groups。保存同步实例与本机覆盖中的群级 talk_value_rules，开启规则，由 SDK 入站 Hook 按群设置 @ 优先级，回复前与发送后 Hook 使用该群的增强频率和时长；全局 talk_value 保持不变。原来停用的旧规则不会被迁移重新启用，迁移前完整内容保留在备份中；仍在使用的其他目标规则保留。每份配置旁的 .reply-backup 是上次保存前的副本，权限 0600；完整部署前快照位置见 STATUS。普通重启保留设置，临时增强窗口重新计时。接口格式为 GET /reply-settings 返回 groups，POST /reply-settings 接收 {group_id, settings}，缺少完整群号时拒绝保存。

食物库支持店名、品牌、地址搜索及区域、品类、标签、核对状态筛选，可查看原始来源、获取时间和菜单缺项并导出 JSON。“热门已匹配”显示已对应的具体门店，“热门待匹配”保留原始名单、待核对原因和公开门店地址。来源提示暂停营业或已停业的门店保留展示，并从随机推荐排除。QQ 未登录时仍读取 WSL 最新公开资料并可暂停、继续采集；WSL 不可用时明确显示离线快照。目录任务的“部分完成”保留来源公布总数与可读取数量，不能按门店总量推定热门 100、全部品牌分店、完整菜单或营业状态已核对。

管理员命令由实例 `plugins/doraymon_admin/config.toml` 启用，权限只认宿主原始 QQ 发送者；无权限时静默拦截，不发送拒绝提示，也不进入正常聊天处理。`bot_config.toml` 的 `plugin.silent_permission_denied=true` 保持内置受保护命令的同样行为。已支持：

```text
/私聊 QQ号 内容
/群消息 群号 QQ号 内容
/好友列表
/群列表
/群成员 群号
/好友申请 请求标识 同意或拒绝
/机器人状态
/机器人帮助
#sl
```

`/群消息` 使用真实 @ 组件。SnowLuma 2.2.0 未提供主动申请加好友接口，该动作保持不可用；`/好友申请` 用于处理收到的申请。发送需获得最终消息编号才确认成功，待核对结果不会自动重发。

Windows 构建环境按 `desktop/requirements.txt` 安装依赖，使用 `tools/build_desktop.py --output <新版本目录>` 构建。WSL 的 `tools/install_desktop.py --help` 给出安装参数；必须指定现有实例、已准备的资料目录、Windows 构建解释器和新版本目录。安装器保留模型、人设和访问策略，备份插件配置，并将桌面凭据经标准输入交给当前 Windows 用户做 DPAPI 加密。安装后的路径与实际验证保存在 STATUS，旧版本目录保留用于回退。

采集独立运行于 `doraymon-catalog-nanchang.service`。人工维护也可执行：

```bash
uv run python -m doraymon.catalog_control --folder <现有资料目录> --action pause
uv run python -m doraymon.catalog_control --folder <现有资料目录> --action resume
```

暂停在当前请求结束后生效，继续复用已有缓存及原始读取日期。公开资料不能提供的内容保留待核对，不因重跑变成当前菜单或当前营业证明。

整合已有门店时，先等待采集服务结束或停止，再备份数据库、catalog.json 和 ranking-index.json。以下命令默认只预览，`--apply` 才写入，且与采集服务共用文件锁：

```bash
uv run python -m doraymon.catalog_consolidation --folder <现有资料目录>
uv run python -m doraymon.catalog_consolidation --folder <现有资料目录> --apply
```

整合只更新已有南昌门店的分类：统一已知英文品类，为缺品类的记录采用同一地点已捕获的咖啡或烘焙目录分类，保留目录 URL 与原获取日期。原品类文本、既有标签和分类依据分别保留；品类中的并列项目生成去重标签。旧缓存的缺值不会清除已有分类与标签。分类不代替菜单、价格或营业核实，不能凭相同地址合并不同店名。

需要重读特定已采集门店时，按 source-listings.json 中的地点 ID 定向刷新，每批最多 100 个；其他门店沿用现有缓存：

```bash
uv run python -m doraymon.catalog_sources --folder <现有资料目录> --refresh-ids <地点ID> <地点ID> --concurrency 1
```

旧缓存保存在 public-pages/versions/，原始资料时间随每次实际获取记录。中文地址和原显示地址分别保留；失败不覆盖已有缓存，已有高德标识与经营状态不会因普通来源缺值而清除。

采集服务在目录核对后读取公开热门名单。单独重读名单、导入已保存的高德公开地点事实或应用分店决定，也可使用同一资料目录：

```bash
uv run python -m doraymon.catalog_rankings_sources --folder <现有资料目录>
uv run python -m doraymon.catalog_rankings_sources --folder <现有资料目录> --place-evidence <公开地点事实.json>
uv run python -m doraymon.catalog_rankings_sources --folder <现有资料目录> --decisions <分店核对决定.json>
```

地点事实文件的 `places` 以原始 `amap:地点ID` 为键，记录官方公开 URL、名称、地址、类别和带时区的实际读取时间；未出现的字段不补猜。决定文件以已采集的名单条目 ID 为键，`action` 为 `match`、`import` 或 `hold`；`match` 需要现有 `shop_id`，三者都需要 `basis`，可附公开依据 `urls`。非精确对应不按相似店名自动合并；全批参数先验证，再写入门店并更新名单。重复应用相同决定复用既有门店身份。

原始名单与逐来源读取状态保存在 `ranking-index.json`，原页及摘要在 `public-rankings/`，门店仍在 `city-candidates.sqlite3`。各写入命令共用采集锁，服务运行时不会并发改库。热门覆盖的 `hot_total` 是综合名单候选数（最多 100），`hot_included` 是该名单已匹配门店数，`hot_missing` 是对应缺口，`hot_pending` 是距 100 个候选的名单缺口；匹配多个名单的同一分店只算一次。页面更新时间和原榜单年度分别展示，均不代替当前营业或菜单证明。

候选多于 100 时，hot_candidates 保存综合 100，ranking_candidates 保存全部来源候选；ranking_missing 保留全部未匹配项。饮品品牌条目的全市总数未知时保持空值，任务显示“待核对”；零记录也不表示当地没有该品牌。

完成 QQ 登录后使用桌面“启动机器人”或“重新连接”。若目标账号尚未被识别且全部 QQ 进程均处于 available，显式启动会刷新一次本地接入进程，再按真实账号匹配；周期巡检沿用普通检查。已有活动会话不会进入此刷新分支。恢复条件和自然登录恢复的实际验证分别记录在 STATUS。

SnowLuma 2.2.0 自带的 #sl 不校验发送者，须关闭机器人账号的 `statusCommand.enabled`；Windows 启动与周期监督会保持这一限制。`#sl` 由 DoraYmon 管理插件接收，先通过宿主 operator 权限与原始 QQ 发送者双重校验，再只读查询接入版本、在线状态、收发链路和运行时长；状态不可读时明确提示。`/机器人状态` 检查管理员命令连接。管理员配置为唯一 `qq:<owner_qq>`，不对其他用户或群聊天流额外放行命令；正常聊天访问策略独立设置。管理页面从桌面设置打开。内置入口行为依据 [SnowLuma 2.2.0 状态命令实现](https://github.com/SnowLuma/SnowLuma/blob/v2.2.0/packages/onebot/src/modules/status-command.ts)。

## 准备候选实例

```bash
uv sync --locked
uv run doraymon prepare
uv run doraymon install-adapter
```

默认实例为 ~/.local/share/doraymon/maibot，本机覆盖目录为 ~/.config/doraymon。prepare 调用锁定版 maibot init 创建缺失配置，再写入公共覆盖项和用餐插件。install-adapter 会下载官方连接器 v1.1.8；已有其他版本时停止，按升级流程处理。

1. 将 deploy/local.example/bot_config.toml 和 adapter_policy.toml 复制到本机覆盖目录，填写机器人账号、管理员和原封闭范围。群号与账号均填写字符串；学习列表会整体替换，须保留完整规则。
2. 再运行 prepare。默认群、私聊均为 block；仅允许本机覆盖中列出的群和人。
3. 在实例 config/model_config.toml 或 WebUI 中填写模型供应商和 key。模型列表只有 deepseek-flash，planner、replyer、utils 共用它；先小范围试聊并查看实际用量。prepare 只覆盖仓库定义的模型列表及任务分配，保留供应商配置和密钥。
4. 高德 key 填在实例 plugins/doraymon_meal/config.toml。插件首次加载时生成此配置，包含 [plugin] 与 config_version="1.0.0"；配置前也可手动创建这两个字段及 amap_key。不要将实例配置复制回仓库。
5. 嵌入模型准备好并确认学习范围后再开启长期记忆；当前 a_memorix 关闭。表达和黑话学习仅在获准私聊及显式列出的测试群开启。

```bash
uv run doraymon check
```

退出码：0 为一致，1 为有配置或插件代码差异，2 为准备失败。check 不写入文件，报告遮盖密钥。prepare 应用于停服状态，先保存本机配置备份；不要在 WebUI 编辑配置的同时执行。

模型列表和任务分配由 deploy/maibot/model_config.toml 维护，重新 prepare 会应用同一份单模型配置。MaiBot 程序与实例目录分开，普通重启不会重新生成默认模型列表；已验证锁定版的重新初始化和配置版本迁移保留模型列表及供应商 Key。升级后在独立实例执行 prepare 和 check，遇上游结构变化时先适配再启动；直接恢复上游默认配置可能重新带入示例模型。

模型价格字段仅供本机用量估算，使用 2026-10-24 核对的官方高峰费率（元/百万 tokens）；实际扣费以供应商账单为准。

## 用餐资料与导出

插件版本为 0.3.0，配置结构为 1.0.0，旧配置缺少的新项采用默认值。应用更新时先备份实例、停服，再按既有 prepare/run 流程同步代码；保留实例 plugins/doraymon_meal/config.toml。

以下字段都在实例插件配置的 [plugin] 下，不放进仓库：

```toml
[plugin]
config_version = "1.0.0"
enabled = true
amap_key = ""
menu_catalog_path = ""
meal_database_path = ""
candidate_database_path = ""
source_cache_hours = 24
xiaohongshu_mcp_url = ""
dianping_mcp_url = ""
source_timeout_seconds = 40
```

- amap_key 使用高德 Web 服务类型。旧项目同类型 Key 可填写到此字段，但账号权限、限额和限制条件应在实际调用前确认；本批未迁移运行凭据。
- menu_catalog_path 填获准使用的商家资料 JSON 绝对路径。单文件最多 2 MiB、500 条，每家最多采用 4 条；amap_id 必须对应高德地点 ID，时间含时区且不在未来。原始资料保存在仓库外；这是资料导入，不是自动获得菜单的接口。
- meal_database_path 填仓库外专用 SQLite 文件的绝对路径，留空不读写库。source_cache_hours 默认为 24，范围 1～168；只控制资料重查间隔，不代表信息有效期、当前供应或营业保证。库操作在线程中执行，库不可用时仍提供已得到的其他资料。
- candidate_database_path 填校内外店铺候选库的绝对路径，启用只读 campus_food。本机使用 /home/mootra/.local/share/doraymon/meal-library/screenshot-candidates.sqlite3，与上方高德资料库用途不同。默认选择 1 家，可指定 1～3 家；按校内/校外/不限和类别筛选。category 省略时沿用，空字符串清除类别；another 沿用条件排除本轮已选，reset 重新选，cancel 停止并清除本成员状态。仅完整店名参加随机抽取；未知归属只出现在“不限”范围。
- xiaohongshu_mcp_url 对应 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 的 Streamable HTTP 入口；dianping_mcp_url 对应 [cn-scraper-mcp](https://github.com/goesByhc/cn-scraper-mcp/tree/master) 的相同传输入口。仅填写已配置且已确认可用的地址，默认留空不调用。可用 HTTPS，或 localhost/127.0.0.1/::1 的 HTTP；不在 URL 放账号、token、查询参数或片段。外部服务的安装、登录、访问权限和平台允许用途分别处理，插件不执行这些操作。
- source_timeout_seconds 为每个远程来源的总等待上限（含同来源排队），默认 40 秒，最大 45 秒。来源故障不阻断其他可用资料；需要禁用某来源时将地址清空并应用配置。

资料格式示意（内容为合成示例，实际使用需替换地点 ID、出处、记录时间和餐点内容）：

```json
{
  "entries": [{
    "amap_id": "synthetic-place-id",
    "title": "合成商家菜单",
    "source_url": "https://example.com/synthetic-menu",
    "source_id": "synthetic-menu",
    "observed_at": "2026-10-24T12:00:00+08:00",
    "items": [{
      "name": "清汤面（合成示例）",
      "kind": "dish",
      "specification": "一碗",
      "preparation": "清汤煮制",
      "flavor": "资料说明可选少盐",
      "portion": "一人份",
      "price_yuan": 18,
      "service_mode": "dine_in"
    }]
  }]
}
```

旧版 text 菜单仍可读取；新版可只提供 items，或同时保留原文 text。每条最多 20 个餐点，kind 为 dish/package，service_mode 为 dine_in/delivery/unknown；未给出的价格用 null，其他未知属性留空。source 可填 catalog、meituan、dianping、xiaohongshu，代表资料原始来源，不会自动启用对应平台采集；published_at 有原始日期才填，source_id 尽量使用该资料在平台上的稳定标识。未知供应方式不能将外卖套餐当堂食菜单，商家资料和用户体验分别存储。

MaiBot 可将“少辣、适合聊天”等要求传入 nearby_food.requirements，后续换店沿用，空字符串清除；change=cancel 停止本成员的在途任务并清除决策状态。新查会替换旧任务，换店/近一点按本成员顺序处理。两秒内完成直接返回，慢查询最多发三条阶段消息。

用户要求文件时调用 meal_export，format 为 html、md 或 json；它使用当前成员尚未过期的同一资料快照，不重查、不另调模型生成事实。文件超过 1 MiB 不发送；发送未获最终消息 ID 时不要自动重发，先在 QQ 查看是否收到。实例不积累导出文件。导出不含消息/成员 ID、聊天原文或个人起点，但仍包含公开门店位置和来源文字；转交其他人前按来源允许用途处理。

离线转换已保存的 JSON，不读取实例或调用外部服务：

```bash
uv run python -m doraymon.meal /本机/meal.json --output-dir /本机/exports
```

CLI 输出独立 HTML、Markdown、JSON 文件并保留来源状态。HTML 不加载外部资源或内嵌地图，只提供位置链接，因此无需高德 JS API Key。JSON 可再次导入上述命令，资料时间要求带时区。

接入后按获准区域核对具体分店与资料时间，尤其检查同名店、缺菜单、旧笔记、来源失败和停止任务。获取时间仅代表取得资料，不能证明笔记新鲜、现价、当下营业或步行路线。当前点评仅门店详情，小红书仅笔记正文；美团商品与抖音尚未接入。

## 本地资料库

已创建独立空库 /home/mootra/.local/share/doraymon/meal-library/meal.sqlite3，未配置到运行实例，未填入演示或未经核实的真实菜单。测试与合成演示使用另外的临时/忽略目录，不修改此库。后续启用时再将该路径填入 meal_database_path。

以下命令仅处理指定的本机文件，不调用模型、地图或 QQ。导入前确认具体分店、高德 ID、资料日期、用途及允许共享的范围。所有运行数据留在仓库外。

```bash
uv run python -m doraymon.meal library init \
  --database /home/mootra/.local/share/doraymon/meal-library/meal.sqlite3
uv run python -m doraymon.meal library import-catalog /本机/已核对菜单.json \
  --database /home/mootra/.local/share/doraymon/meal-library/meal.sqlite3
uv run python -m doraymon.meal library show 高德地点ID \
  --database /home/mootra/.local/share/doraymon/meal-library/meal.sqlite3
uv run python -m doraymon.meal library show 高德地点ID --history \
  --database /home/mootra/.local/share/doraymon/meal-library/meal.sqlite3
```

import-catalog 按运营者提供的获准菜单作为共享资料导入，只保留明确记录，不从文本自动猜测菜品属性。只有地点 ID 时可先存资料；未有高德门店元数据就不能导出为有位置的候选页面。后续正常高德查询命中该 ID 时补齐门店元数据；推荐仍先经过高德查店，库中的 ID 不自动变成新门店。

导入以前保存的同一份 MealReport 时，必须明确选择 --shared 或 --scope 本机范围名。--shared 仅用于已确认允许共享的资料；--scope 作为本机私有命名空间保存，show 时也要给同一范围名，默认查询只读共享资料。机器人自身的私有范围由真实成员和本次用餐条件派生，不保存原始标识或提问文本。

```bash
uv run python -m doraymon.meal library import-report /本机/已核对资料.json --shared \
  --database /home/mootra/.local/share/doraymon/meal-library/meal.sqlite3
uv run python -m doraymon.meal library show 高德地点ID --output-dir /本机/exports \
  --database /home/mootra/.local/share/doraymon/meal-library/meal.sqlite3
```

show --output-dir 从当前库记录生成 HTML、Markdown、JSON，要求已有高德门店信息且不与 --history 同用。库中不保存本次出发点与距离，离线导出不会显示历史起点下的距离。普通 show 和导出会标记需复核记录：默认超过 24 小时未取得新记录、原文超过 90 天，或体验资料没有原文日期。读取不会刷新这些原始时间。

更新菜单时导入带真实新获取时间的资料，原版本保留；较旧资料晚导入不会覆盖较新记录。当前查询每店最多取 32 个最新资料文档、报告最多 8 条、历史查看最多 100 条，历史数据仍保存在库内。完全相同的观察记录重复导入不新增版本；重新取得相同内容而获取时间不同会留作一次新观察。

库为未知格式、未来版本或被占用时会返回错误或来源不可用，不清空或重建原库。备份时停止写入，再复制库文件；恢复后先用 show 检查来源、日期和记录，不把演示库替换进运行实例。关闭机器人内资料库读写时将 meal_database_path 留空并应用配置，既有历史文件保留。

## 独立测试实例

本机已准备 /home/mootra/.local/share/doraymon/maibot-test，覆盖目录为 /home/mootra/.config/doraymon/test。WebUI 使用 8003，消息服务使用 8012；账号与 OneBot 端点已就绪，连接器已在本机覆盖中启用。

```bash
uv run doraymon check \
  --instance ~/.local/share/doraymon/maibot-test \
  --local ~/.config/doraymon/test
```

模型 key 保存在该实例的 config/model_config.toml，沿用原实例配置。实际私聊与群聊访问范围由当前 adapter_policy.toml 决定，本次接入切换不修改访问策略。当前使用 SnowLuma，目标账号在线，统一 QQ 连接器已识别 snowluma 画像并激活消息网关。

常驻服务沿用 doraymon-maibot-test.service，已启用用户服务自启；Windows 登录及每分钟检查由 DoraYmon-SnowLuma 任务触发，新运行目录 runtime-profile.json 的 backendService 指向此单元。runtime.ps1 负责账号匹配、只读探针、后端启停与 WSL 网络地址更新；模板位于 deploy/snowluma/windows，隐藏启动入口为该运行目录的 runtime-hidden.vbs。MaiBot 管理页为 http://127.0.0.1:8003，SnowLuma 管理页为 http://127.0.0.1:5099。

桌面 QQ 与 SnowLuma 需保持运行，关闭管理网页不会退出账号；退出 QQ 客户端或机器人账号后需要重新登录。旧 doraymon-qq.service 已停止且禁用自启，MaiBot 单元使用 Conflicts 防止同一用户下两个后端同时启动。用户 lingering 已开启，Windows 检查入口保留隐藏的 WSL 保活进程。后端进程异常由 systemd 恢复，SnowLuma 进程退出后由下一轮检查重新启动；等待指定账号登录时暂停后端。电脑关机、睡眠或退出 Windows 会话时不保证在线；Windows 重启与多日运行须分别验收。

服务使用实例 config/agreements.env 中对应版本的 EULA_AGREE、PRIVACY_AGREE 校验值启动；升级后阅读该版本条款并更新确认。

Windows 常驻切换前的脚本、配置和任务 XML 已保存在 C:\Users\31738\DoraYmon-NapCat\trial-backups\maibot-resident-20261025-222629；WSL 实例配置与服务单元备份为 /home/mootra/.local/state/doraymon/maibot-runtime-20261025T142629Z。原试运行回退入口仍保留在 Windows 的 C:\Users\31738\DoraYmon-NapCat：

```powershell
& "$env:USERPROFILE\DoraYmon-NapCat\rollback-maibot-trial.ps1"
```

该脚本先停止候选，再恢复旧服务及检查任务保存的启用状态；候选停止失败时不会启动旧后端。恢复状态和原配置保存在 maibot-trial-backup-path.txt 指向的本机备份目录。

## 接通 QQ

同一个账号只允许一个后端回复。当前使用 SnowLuma，原 NapCat 进程和自动检查已停用；旧 DoraYmon 后端仍停止且禁用。

在取得账号及真实调用的具体授权、确认名单后执行：

```bash
uv run doraymon prepare --napcat /本机/NapCat运行目录/integration.json
uv run doraymon check --napcat /本机/NapCat运行目录/integration.json
uv run doraymon run --napcat /本机/NapCat运行目录/integration.json
```

接入文件使用现有 Windows 脚本发布的 wsUrl 和 wsToken；支持 UTF-8 BOM。连接器配置只接受带主机与端口的 ws:// 地址，token 单独读取。run 每次启动刷新地址和插件代码，保留 WebUI 编辑的其他设置。不要把 token 附在命令行 URL 中。

WebUI 仅监听本机；遥测、外部 Agent 及连接器的主动私聊、戳一戳、撤回、禁言、表情回应、合并转发工具均关闭。群参与、停止、重连和用餐多轮行为按 STATUS 的 P1 验收，不能从离线测试推定通过。

## 隔离回放

回放实例使用独立数据、端口和连接器，不能指向真实 NapCat。以下准备步骤不调用模型：

```bash
uv run doraymon prepare \
  --instance ~/.local/share/doraymon/replay \
  --local deploy/local.example/replay
uv run doraymon install-adapter --instance ~/.local/share/doraymon/replay
uv run doraymon prepare \
  --instance ~/.local/share/doraymon/replay \
  --local deploy/local.example/replay \
  --onebot ws://127.0.0.1:3901
uv run python -m doraymon.replay validate
```

真实模型回放前，在回放实例配置模型及预算。在一个终端启动回放台，另一个终端启动该实例：

```bash
uv run python -m doraymon.replay run --split development
```

```bash
uv run doraymon run \
  --instance ~/.local/share/doraymon/replay \
  --local deploy/local.example/replay \
  --onebot ws://127.0.0.1:3901
```

默认等待连接 300 秒，各场景留出模型应答时间；--speed 大于 1 仅用于冒烟，不用于质量或延迟比较。引用机器人但未收到前置回复时停止。报告保存在已忽略的 evals/runs/，含合成场景与模型输出；不提交真实聊天或密钥。

每轮对比从干净实例开始，或先停止服务并恢复基线数据备份。场景的 reply/silent 是评测目标，不是对 MaiBot 默认行为的承诺。保留组不用于反复调参。

## 日常操作与排障

- 配置不符：先 check；未知键说明锁定上游模板与覆盖项不兼容，核对版本后调整，不关闭检查。
- 插件未加载：核对 manifest 的宿主/SDK 版本范围及实例 config.toml 的 plugin.config_version；用餐插件要求 1.0.0，连接器要求 2.7.1。
- 无消息：核对账号与 NapCat 登录账号一致、WebSocket 地址、token、访问名单和 WebUI 中的网关状态。
- 用餐查不了：检查实例内的高德 key；地点有多个匹配时补充城市，失败结果不代替真实店铺。
- 地图发送未确认：先在 QQ 核对是否收到，不自动重发。普通回复的发送记录和模型调用在 MaiBot WebUI 查看。
- 插件目录为旧符号链接：停服后将实例配置单独保存，移除链接，再 prepare 复制代码并恢复实例配置；不要递归删除链接指向的源码。

## 备份、升级与回退

当前已提供试用服务单元、启动前备份和回退入口。生产健康状态接入、定时备份及实际恢复演练属于 P4。手动准备升级时：

1. 停止候选实例，将整个实例目录复制到仓库外的本机备份位置，保留版本号和时间。
2. 阅读上游 changelog，修改 MaiBot 版本并更新 uv.lock；升级连接器时同时核对配置结构版本。
3. 在独立实例用 prepare 应用仓库配置、check 核对，再运行离线检查、回放与测试群验收，之后安排主号切换。
4. 回退时恢复旧代码及锁文件，同时恢复升级前实例备份。新版可能已迁移数据库，不让旧版直接读取新数据。

旧服务及其数据库保留为回退来源，不自动导入 MaiBot。QQ 账号助手与 Windows 的官方 Bot 电脑控制桥接分别维护。
