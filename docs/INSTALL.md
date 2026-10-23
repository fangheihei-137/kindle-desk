# 安装指南

## 1. 确认设备

当前实测设备为 Kindle Basic 7 / KT2（2014），固件 5.12.2.2，屏幕 600×800。其他分辨率、不同显示命令或触摸设备可能需要修改程序；请先作为适配开发对待，不要按“通用安装包”理解。

在 Kindle 设置 → 设备信息查看型号与固件。需要越狱时，用 [KindleModding 兼容向导](https://kindlemodding.org/jailbreak-wizard.html) 查找对应方案，并遵循该方案的完整说明。本项目不包含越狱载荷、不自动替换固件；无需为了安装本看板盲目升降级系统。

## 2. 准备启动器与 Python

- 需要一个可执行 shell 脚本的已安装启动环境。支持书库脚本的环境可以用 `IndependentDashboard.sh`；也附带 KUAL 扩展入口。KUAL 本身请参考 [KindleModding](https://kindlemodding.org/) 的适配说明安装。
- 从 [NiLuJe 的 Kindle 工具发布帖](https://www.mobileread.com/forums/showthread.php?t=225030) 中选择明确适配 **K5 / KT2** 的 Python 3.9 包。不要选 K2/K3/K4 架构的包。
- 按上游包说明准备运行环境。此项目使用路径 `/mnt/us/python3/bin/python3.9`，USB 视图中应为 `python3/bin/python3.9`。
- Python 环境必须包含 Pillow / PIL、SSL、FreeType、zlib。实际开发设备使用 Python 3.9.8 和 Pillow 9 系列。普通 Windows 或 x86 Linux Python 不能直接复制到 Kindle 使用。
- 运行时不随本项目重新打包，保留上游安装与更新渠道。电脑上的 `pip install -r requirements.txt` 仅用于电脑自检，不能代替 Kindle 运行环境。

## 3. 复制看板

下载仓库 ZIP 或克隆，解压后在该文件夹运行：

```sh
python -m pip install -r requirements.txt
python install.py --check
python install.py F:/
```

把 `F:/` 替换成 Kindle 的实际 USB 根目录；根目录应有 `documents`。macOS / Linux 也可指定挂载路径，例如 `/Volumes/Kindle`。

安装工具会检查并复制程序、开源字体、拼音词库、二维码组件；创建空日程和独立随机凭据，安装书库入口及 KUAL 扩展。不会读取或写入你的电子书。

已有 `kindle-standalone`、同名 KUAL 扩展或同名书库入口时，首次安装会停止。升级请用下一节的方法。如果复制因拔线或空间不足中断，请保留工具报错和 `kindle-desk-install-*` 暂存目录，确认状态后再重试。

安全弹出 USB，Kindle 连接 Wi-Fi。打开书库里的 `IndependentDashboard`，或从 KUAL 选择 Kindle Desk。首次启动可能需要约 30 秒。点右下角手机图标，让手机连接相同 Wi-Fi 后扫码。

## 4. 日常使用

安装完即可关闭电脑。设备自己获取天气与资讯，日程和待办存储在 Kindle 上。详情见 [使用说明](USAGE.md)。

城市留空时按网络出口 IP 估算；如果城市不对，在手机管理页手动填写城市。公共/访客 Wi-Fi 可能隔离设备，此时手机无法连接看板。

## 5. 更新

### 无线更新（程序与拼音词库）

在 Kindle 点手机图标，保持二维码页开启。电脑在同一 Wi-Fi 中执行：

```sh
python kindle-standalone/wireless_update.py 192.168.1.100
```

地址换成设备二维码下方显示的地址。首次 USB 安装会保存本机无线更新凭据；换电脑时可通过 USB 同步建立凭据。不要从公开示例或别人设备上复制 `wireless-device.json`。

更新前做预运行检查，保存旧程序；启动失败会自动恢复。不会用电脑上的示例数据覆盖设备日程。字体、启动器及恢复程序不会通过无线更新修改。

### USB 同步（已有安装）

Kindle 先退出看板回到书库，再接 USB。Windows 电脑执行：

```sh
python kindle-standalone/sync_standalone.py
```

会备份并同步程序、拼音词库及书库入口，保留设备 `data`。已有字体和二维码组件不会被替换；资源目录完全缺失时才复制完整目录。当前自动查盘仅适用于 Windows。新装请用 `install.py`。

## 6. 退出、备份与移除

首页右下角门形图标回到书库。看板不会开机自启。

备份时先退出看板、接 USB，再复制设备 `kindle-standalone/data` 整个文件夹。它包含日程、待办、配置和连接凭据，作为私有备份保存。

要移除看板，退出后接 USB，再手动移走 `kindle-standalone` 文件夹、`documents/IndependentDashboard.sh` 和 `extensions/kindle-desk`。先备份数据；Python 或其他应用仍在使用的公共运行环境不要一并删除。
