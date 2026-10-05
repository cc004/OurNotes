# OurNotes

Windows 本地工具仓库：运行 nnnotes、从自己的 XAPK 恢复配置与 protobuf 协议，以及使用 Python 库和 CLI 管理账号、批量注册和刷初始。

两个上游项目均为 Git submodule：

- `nnnotes/`：[MetaSekaiLab/nnnotes](https://github.com/MetaSekaiLab/nnnotes)，固定到 `3a6bdfc`。
- `dumper/`：[MetaSekaiLab/Il2CppDumper-0x66](https://github.com/MetaSekaiLab/Il2CppDumper-0x66)，固定到 `6fd1d13`。

游戏包、提取的密钥、协议描述、账号凭据及缓存均在 Git 忽略范围内。主仓库尚未设置远程地址，脚本不会自动 commit 或 push。

## 准备与启动

需要 Windows、Git、Python 3.11（`py -3.11`）、可构建 net8.0 的 .NET SDK，以及 .NET 8 运行时。把 XAPK 放到根目录或 `xapk/` 中，在根目录的 PowerShell 执行：

```powershell
git submodule update --init --recursive
.\prepare.ps1 -RefreshMaster
.\run.ps1
```

资源浏览器地址：<http://127.0.0.1:8000/jp/ja/>。前台进程用 Ctrl+C 关闭；此前在本工作区启动的后台浏览器可用 `.\stop.ps1` 停止。

多份 XAPK 时用 `.\prepare.ps1 -Xapk "D:\路径\游戏.xapk" -RefreshMaster`。如果 PowerShell 阻止运行脚本，可使用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\prepare.ps1 -RefreshMaster`。

`prepare.ps1` 按顺序完成：

1. 创建 `.venv`，安装 nnnotes 对应版本的 Windows wheel、客户端及分析依赖，规范化解包主 APK 和 split APK。
2. 检查二进制指纹，解密 metadata，从 APK 提取 API/CDN 地址及资源、masterdata 解密参数，验证后写入私有 `nnnotes.toml`，保留其他配置。
3. 编译并运行 dumper 子模块，输出到 `work/dump/result/`。相同 APK 和 dumper 提交会复用结果；`-ForceDump` 强制重做。
4. 静态分析 ARM64 中 protobuf 初始化代码，恢复 `work/protocol/descriptor.pb`，验证所有依赖可加载。
5. 使用 `-RefreshMaster` 时下载并解码在线 masterdata，供卡池列表与批量计划校验使用。

当前适配范围为本次提供的**日服 1.0.4 ARM64 APK**。脚本检查 SHA-256；不同构建会停止，需更新分析适配。流程不需要 Android 设备。此次恢复 110 份 protobuf 描述文件、51 个服务、252 个 RPC；客户端仅开放下述账号操作。

只重新检查配置提取、不写配置：

```powershell
.\.venv\Scripts\python.exe scripts/dump_config.py --check-only
```

## 单账号 CLI

全局参数如 `--account` 放在子命令之前。账号名使用字母、数字、下划线或连字符。以下注册及写操作会立即请求服务器：

```powershell
.\client.ps1 --help
.\client.ps1 version
.\client.ps1 --account first register
.\client.ps1 --account first login
.\client.ps1 --account first finish-tutorial
.\client.ps1 --account first login-bonus
.\client.ps1 --account first presents
# 用 presents 返回的实际 present_id 领取，例如：
.\client.ps1 --account first open-presents 123456
.\client.ps1 --account first data
.\client.ps1 --account first status
```

登录通过已保存的服务端凭据调用 `Whoami` 校验，不是用户名/密码登录。注册的 `initial_data_group` 默认空字符串，可用 `register --initial-data-group VALUE` 指定。已有凭据可通过 `import-credential 文件.json` 导入一个新的账号名；JSON 需要非空字符串 `id`、`credential`，可选 `device_id` 必须使用服务器签发的值。新注册账号没有 device ID 时省略该请求头；自行生成 UUID 会导致认证失败。首次认证发送 APK 中的设备初始化标志，成功后保存状态。已有账号文件不会被导入覆盖。

查看本地 master 中的普通卡池及商品组合，然后指定抽卡：

```powershell
.\client.ps1 gacha-list
# 示例 ID，使用前确认卡池有效期、价格及账号资源：
.\client.ps1 --account first draw --gacha-id 1 --product-id 3
.\client.ps1 --account first gacha-history
```

`draw` 支持 `--pick-up ID1 ID2`；`name 名字` 修改昵称。是否满足卡池开启条件、余额及抽取限制由服务器判定。`gacha-list` 展示 master 中的常规商品关联，不查询当前账号的可抽状态；阶梯卡池没有专用流程。

协议查看命令只读取本地描述文件：

```powershell
.\client.ps1 methods --contains Gacha
.\client.ps1 describe /app.player.PlayerService/Register
.\client.ps1 describe /app.gacha.GachaService/Execute
```

## 批量注册与刷初始

可直接使用两份配置：`examples/live-initial.json` 是本次真实联调使用的单账号、一次十连原始配置；`examples/reroll-mygo.json` 最多注册 3 个账号，每个账号一次十连，以 4 星高松燈（master ID `51`）为目标，抽卡后绑定引继密码，命中即停止。后者尚未实际批量执行。两份配置都使用 `gacha_id=1, product_id=3`，当前卡池截止时间字段为 `2026/10/08 23:59:59`，过期后应更换卡池。

```powershell
# 先查看带目标卡的计划；添加 --execute 才实际注册和抽卡
.\client.ps1 workflow examples/reroll-mygo.json --job mygo01
```

编辑 `examples/reroll.json`，或复制到被 Git 忽略的 `work/` 中再编辑：

- `max_accounts`：最多创建多少账号，必须明确设置，范围 1–10000。
- `finish_tutorial`、`login_bonus`、`claim_presents`：是否完成教程、领取签到、领取礼物。
- `draws`：按顺序执行的卡池/商品组合；`times` 是调用次数，十连商品调用一次即十连，每步最多 1000 次。
- `target_card_ids`：目标卡牌的 master ID 列表，匹配任意一个即命中。可从 `work/master/jp/MasterMemberCard.json` 查找；不是账号内卡牌实例的 ID。
- `stop_on_match`：命中后停止后续抽卡和注册；目标列表为空时按数量上限完成所有账号。
- `account_delay_seconds`：账号之间等待时间，默认 3 秒；账号串行执行。
- `bind_transfer`：抽卡结束后为每个账号绑定引继密码并导出，命中目标时也先完成绑定再停止。`reroll.json` 和 `reroll-mygo.json` 已启用；旧计划省略此字段时保持关闭。

示例最多注册 3 个账号，每个账号调用一次 `gacha_id=1, product_id=3`。本次下载的 master 中该商品为十连、`_price=2000`；这些 ID 及价格只是当前本地数据示例，卡池可能过期，刷新 master 后应重新选择。示例目标列表为空，需要填入目标卡牌才能按命中条件提前停止。仅批量注册时可设 `draws: []`，并按需关闭其他操作。

```powershell
# 查看计划及调用上限，不创建账号、不抽卡
.\client.ps1 workflow examples/reroll.json --job batch01

# 执行；中断后使用同一条命令恢复
.\client.ps1 workflow examples/reroll.json --job batch01 --execute

# 查看某个批次账号
.\client.ps1 --account batch01_0001 status
.\client.ps1 --account batch01_0001 login
.\client.ps1 --account batch01_0001 data
```

账号保存在 `work/accounts/<job>_0001.json` 等文件中，进度保存在 `work/accounts/jobs/<job>.json`。所有账号都会保留，包括未命中账号。同一 job 名称绑定相同计划；修改计划后应使用新名称。已完成步骤通过持久化操作 ID 复用结果，完成的任务不会重新创建账号。

写操作发送前保存 `pending`，成功后原子保存结果。网络超时、响应无法解析等结果不明的情况会停止任务，并阻止该账号后续写操作；只读登录和查询仍可使用。当前没有自动对账或清除 pending 的命令，需要核对服务器结果后再处理状态文件，直接重试或删除文件不能保证不会重复扣资源。进程锁防止同一任务或账号同时写入。CLI 隐藏凭据字段，但账号 JSON 本身含完整登录凭据，应保留在本机。

## Python 库

使用 `.venv\Scripts\python.exe`。构造客户端不会创建账号，调用写方法才会发送请求：

```python
from ournotes import Client

with Client("nnnotes.toml", "work/protocol/descriptor.pb", "work/accounts/python01.json") as client:
    client.refresh_version()
    if not client.status()["registered"]:
        client.register(operation="register")
    print(client.login())
    client.finish_tutorial(operation="tutorial")
    client.login_bonus(operation="login_bonus")
    print(client.presents())
    print(client.player_data())
    # 选定实际可用商品后调用；相同 operation 只复用相同请求的已保存成功结果。
    # client.draw(1, 3, operation="initial_draw")
```

批量流程入口是 `ournotes.workflow.run_workflow(plan, job, directory, make_client)`；CLI 在调用它前额外检查本地 master 中的卡池与商品关联。protobuf 的 int64/uint64 在响应 JSON 中使用字符串。

## 抽卡后绑定引继

游戏使用**玩家数字 ID（profile ID）+ 绑定密码**引继，不会另行生成独立引继码。`RegisterPassword` 成功后保存绑定结果，生成的每账号独立密码为 16 位，包含大小写字母、数字和符号。玩家 ID 使用服务器返回的数字 ID，不使用认证 UUID。

启用 `"bind_transfer": true` 的批量任务在抽卡步骤完成后调用绑定。绑定完成前不会把账号标记为完成；绑定超时会保留原密码和 pending 状态，停止任务。正常续跑复用已成功绑定的密码，不重复抽卡或重置密码。

对已经抽好的账号，可单独绑定，无需修改已完成任务的配置：

```powershell
# 自动生成密码、绑定并保存本机引继文件
.\client.ps1 --account live_initial_20261005_0001 bind-transfer

# 可选：使用 UTF-8 文本文件中的密码，8–16 位可打印 ASCII 字符、不含空格
.\client.ps1 --account another_account bind-transfer --password-file work/transfer-password.txt

# 只重新导出已经成功保存的绑定结果，不调用服务器
.\client.ps1 --account live_initial_20261005_0001 export-transfer
```

引继文件保存在 `work/accounts/<账号名>.transfer.json`，其中 `profile_id` 和 `password` 就是游戏引继界面需要的两项；原账号 JSON 同时保留绑定记录。CLI 只显示玩家 ID 和保存路径，不输出密码。引继文件及默认账号目录均被 Git 忽略。重复 `bind-transfer` 会复用已绑定结果，指定与已绑定记录不同的密码会报错，不会静默重置。

Python 调用为 `client.bind_transfer()`，或 `client.bind_transfer(password)`；已导入但没有 profile ID 的凭据需先调用 `client.player_data()`。`client.export_transfer()` 可恢复本机导出文件。已完成的旧任务不会自动补执行新步骤，使用上面的单账号命令补绑即可。

本次已为联调账号 `live_initial_20261005_0001` 完成真实绑定；后续 `GetPlayerData` 确认 `has_password=true`。引继文件位于 `work/accounts/live_initial_20261005_0001.transfer.json`。没有执行换设备引继，当前验证覆盖密码绑定及服务端绑定状态。

## 文件与验证

| 路径 | 内容 |
| --- | --- |
| `nnnotes/`、`dumper/` | 固定提交的上游子模块 |
| `ournotes/` | Python 客户端、CLI、任务执行器、协议提取器 |
| `nnnotes.toml` | 私有配置，含提取的密钥和地址 |
| `work/apk/jp/` | 主 APK 和 split APK |
| `work/dump/` | 原生库、解密 metadata 与 dumper 输出 |
| `work/protocol/` | 从 APK 恢复的 protobuf 描述及清单 |
| `work/master/jp/` | 解码后的 masterdata |
| `work/accounts/` | 账号凭据、请求日志和批量任务进度 |
| `cache/`、`out/` | 下载缓存与导出文件 |

本地测试启动临时 gRPC 服务，不访问真实游戏服务器：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git submodule status
```

已验证完整准备流程、在线匿名版本查询、资源目录与资源下载解密，以及 238 张 masterdata 表的解码。本地模拟服务器测试覆盖注册、凭据登录、领奖、抽卡、批量停止、中断恢复及设备凭据处理。

2026-10-05 的真实日服联调完成了一个账号的注册、登录、教程完成、签到、领取 30 份礼物及 MyGO 卡池一次十连。服务器抽卡历史确认只有一次执行、消耗 2000 免费石，查询余额为 4320 免费石；获得 4 星高松燈「流星に放つ叫び」（卡牌 ID 51）。关闭并重建客户端后的登录、账号查询和抽卡历史查询均成功。

账号名为 `live_initial_20261005_0001`，完整凭据保存在 `work/accounts/live_initial_20261005_0001.json`，脱敏联调报告为 `work/reports/live-initial-20261005.json`。任务 `live_initial_20261005` 已完成；重复执行相同计划和任务名只返回已保存结果，不再注册或抽卡。

```powershell
.\client.ps1 --account live_initial_20261005_0001 login
.\client.ps1 --account live_initial_20261005_0001 gacha-history
.\client.ps1 workflow examples/live-initial.json --job live_initial_20261005 --execute
```

nnnotes Python 源码从子模块加载，Rust `_deck` 扩展使用相同版本的预编译 Windows wheel；修改 Rust 源码需另行安装工具链重编。仅安装 Python 环境、不做 dump 时可运行 `.\setup.ps1`。`.\run.ps1 browse-apk` 和 `catalog-apk` 提供离线 APK 目录浏览，其余参数转交 nnnotes。音频导出所需 FFmpeg/vgmstream、网页播放器构建所需 Node.js 未包含在基础环境中。
