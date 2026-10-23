# 第三方组件

本项目原创代码的 MIT 许可不改变以下组件的许可。没有打包 Windows 微软雅黑、Arial、Kindle 固件、越狱载荷或 Python 二进制运行环境。

| 组件 | 来源与许可 | 许可文件 |
| --- | --- | --- |
| 思源黑体 CN Regular / Bold | [Adobe Source Han Sans](https://github.com/adobe-fonts/source-han-sans)，SIL OFL 1.1，未修改字体文件 | `kindle-standalone/assets/OFL-SourceHanSans.txt` |
| 离线拼音数据 | [Rime pinyin-simp](https://github.com/rime/rime-pinyin-simp)，来源 Android Pinyin IME，Apache 2.0；按首字母拆分并整理权重 | `assets/ime/LICENSE`、`AUTHORS`、`NOTICE.txt` |
| qrcode 7.4.2 | [python-qrcode](https://github.com/lincolnloop/python-qrcode)，BSD | `vendor/qrcode-7.4.2.dist-info/LICENSE` |
| pypng 0.20220715.0 | [PyPNG](https://gitlab.com/drj11/pypng)，MIT | `vendor/pypng-0.20220715.0.dist-info/LICENCE` |
| typing_extensions 4.7.1 | [typing_extensions](https://github.com/python/typing_extensions)，PSF | `vendor/typing_extensions-4.7.1.dist-info/LICENSE` |
| 农历、节气离线表 | 由 [lunar-python 1.4.8](https://github.com/6tail/lunar-python) 生成，MIT | `renderer.py` 数据表附近包含来源和完整 MIT 声明 |
| CA 根证书包 | [curl CA Extract / Mozilla](https://curl.se/docs/caextract.html)，Mozilla 证书数据，MPL 2.0 | `assets/cacert.pem` 内含来源说明 |

表格中的相对路径以 `kindle-standalone/` 为根。公开版字体下载来源为 Adobe 仓库 `release/SubsetOTF/CN/`，原始文件 SHA-256：

- Regular：`e2bc8a2e7f37474b774fff8db758681ece40bb6947a90d571bce9dd60671a8e4`
- Bold：`62383707c086a32f3afd5e293f34c7eff64c7fea31f579fdc6cbe34d920519a6`

天气使用 Open-Meteo；资讯读取华尔街见闻，备用源为东方财富。资讯正文不包含在发行包内，属于原发布方；本应用仅在设备上缓存并标明来源。上游服务可能调整接口或使用条件。

宣传图使用内置图像生成工具制作，并结合应用的虚构示例画面。图中设备场景为效果示意，不冒充实机摄影或 Amazon 官方宣传。
