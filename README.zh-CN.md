# OMEN PSU Monitor

[English](README.md)

用于 **HP GAMING PSU USB HID 电源 `03F0:04B0`** 的 Windows 桌面监测程序，可将实时读数接入 HWiNFO64 自定义传感器。

当前版本 **1.1.0**。本项目由社区独立开发，与 HP、HWiNFO 无隶属关系，功能范围是该型号电源的遥测监测。

## 兼容范围

| 项目 | 支持目标 |
| --- | --- |
| 操作系统 | Windows 10/11 x64；已在 Windows 11 x64 验证 |
| 设备 | HP GAMING PSU，USB VID `03F0`、PID `04B0` |
| 固件/设备版本 | 仅 `0200` |
| HID 接口 | Usage Page `FF00`、Usage `0001`；输入/输出报告各 64 字节，无 Feature 报告 |
| 界面语言 | 桌面界面中文；HWiNFO 传感器名称英文 |

硬件测试覆盖一台设备及其固件版本，兼容支持属于实验性质。读数来自设备上报，并采用从厂商软件行为中整理的换算系数，未经外部仪器独立校准。仅有相同品牌名称不能证明兼容。

## 功能

- 已测试设备提供 12 项传感器：功率、温度、效率、风扇转速，以及 3.3 V、5 V、5 V 待机、12 V 总路的电压和电流。
- 各项当前值、最低值、最高值、平均值及历史曲线。
- 1/2/3/5/10 秒采样，暂停/继续、重置统计、窗口置顶。
- CSV 实时记录与会话导出、自动重连、过期读数撤下。
- 可选接入 HWiNFO64 的 Sensors 窗口。

测量查询需要发送 HID 输出报告，厂商操作字段固定为 **`03 READ`**。程序仅实现能力列表和测量查询，不包含风扇设置、电源配置或固件更新命令。详见[协议说明](docs/PROTOCOL.md)。

## 使用 Windows 程序

1. 在本仓库的 Releases 页面下载 `OmenPsuMonitor-1.1.0-win64.zip` 并解压。
2. 双击 `OmenPsuMonitor.exe`。独立显示功能无需安装 Python 或 HWiNFO。
3. 等待显示“电源已连接”，点击传感器可查看曲线。

配置和程序日志默认保存在 `%LOCALAPPDATA%\OmenPsuMonitor`。如需随 U 盘等目录携带数据，可在可写文件夹中运行 `OmenPsuMonitor.exe --portable`，数据将保存到 EXE 旁的 `data` 子文件夹。首次正常启动时，如发现旧版旁置 `data/settings.json`，会迁移其中已知的配置项。

快捷键：`F5` 重置统计，`Ctrl+S` 导出会话历史。每项曲线最多保留 600 点，会话导出最多保留 50,000 组快照；长时间记录请开启 CSV 记录。

## 接入 HWiNFO64

1. 运行本程序，等待读数出现。
2. 勾选底部 **接入 HWiNFO64**。
3. 若未发现分组，完全退出并重新打开 HWiNFO，然后进入 Sensors。
4. 找到 **HP OMEN PSU (ImprovingDual)**。保持本程序运行以提供实时读数。

桥接使用 HWiNFO [官方自定义传感器接口](https://www.hwinfo.com/forum/threads/custom-user-sensors-in-hwinfo.5817/)，写入当前用户的注册表：

```text
HKCU\Software\HWiNFO64\Sensors\Custom\HP OMEN PSU (ImprovingDual)
```

本程序是外部传感器数据提供程序，HWiNFO 不加载其 DLL。HWiNFO 需另行安装，发布包不包含 HWiNFO。导出名称使用英文 ASCII 字符，避免部分 HWiNFO 配置下中文显示为问号。暂停采集、数据失效、关闭接入或正常退出时，会撤下本程序发布的实时值。

## 从源码运行

使用带 Tkinter 的 Windows x64 Python。已验证的构建环境为 **Python 3.14.7**，应用运行代码仅使用标准库。

在仓库文件夹中运行：

```powershell
python omen_psu_app.py
python -m unittest discover -s tests -v
```

命令行采集：

```powershell
# 离线显示固定查询报文，不打开设备。
python hp_omen_psu_monitor.py --dry-run
# 每隔至少两秒采样一次，共十次。
python hp_omen_psu_monitor.py --samples 10 --interval 2 --output telemetry.json
```

JSON 诊断结果包含原始报告与设备接口路径，公开分享前请检查并脱敏。离线测试使用合成数据，无需电源硬件或 HWiNFO。

构建 EXE 和发布压缩包见[发布说明](docs/RELEASING.md)，版本变更见 [CHANGELOG.md](CHANGELOG.md)，GitHub 同类项目调查见[相关项目](docs/ALTERNATIVES.md)。

## 常见问题

- **找不到设备：** 检查 Windows 是否识别 `VID_03F0&PID_04B0`。其他 OMEN 硬件不适用此协议。
- **设备版本或报告格式不匹配：** 程序会拒绝未经验证的变体。反馈时附上相应标识，不建议绕过校验。
- **超时或断连：** 重新连接设备再试。如同时开启其他电源轮询软件，可暂时关闭以排查干扰。
- **HWiNFO 没有分组或名称未更新：** 保持接入启用，完全重启 HWiNFO。同一时刻仅一个本程序实例可以占有该桥接分组。
- **查找日志：** 在上述数据目录查看 `app.log`。反馈请包含程序版本、Windows 版本、设备版本和脱敏后的错误片段。

## 许可

项目源码采用 [MIT License](LICENSE)。互操作与运行时组件说明见 [NOTICES.md](NOTICES.md)，Windows 压缩包内的 `THIRD_PARTY_NOTICES.txt` 包含运行时许可全文。HP/OMEN 和 HWiNFO 名称用于说明兼容产品，商标归各自所有者所有。
