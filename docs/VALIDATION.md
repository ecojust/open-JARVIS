# 开发预览验证记录

日期：2026-10-31。此记录区分已经执行的检查与发布前仍需完成的验收。

## 桌面语音条与模型升级兼容

`88cdca6` 将入口改为 320×72 的山羊语音条，声浪来自真实 PCM/VAD 电平；点击展开使用 `showMaximized()`，显式退出已有全屏状态。增加设置中的下载/校验按钮、进度、取消和重试；已下载模型按固定模型 revision/运行时契约复用，升级默认模型不会误认旧格式，原子安装中断可恢复。`3b3153e` 补充验证：与收音无关的下载或服务警告不会遮住正在接收的真实声浪。

本地 **247 项测试通过**，包括原生按钮/拖动、静音无动画、窗口最大化且非全屏、下载进程协议、升级模型复用、未知 ABI 拒绝，以及强杀发布前后的恢复。[实际 GUI 验收与截图](validation/desktop-ux-2026-10-31/README.md) 使用真实中文音频样例、实际已有模型哈希核验进程及实际任务后端；模型未重复联网下载，麦克风/扬声器与 macOS 真机 Spaces 未验收。

30 次 Linux offscreen 语音条启动 P95 261.3 ms、RSS P95 75.6 MiB，不含后端和语音模型。[完整原始测量](validation/desktop-ux-2026-10-31/performance.json) 保留每次独立启动的数据。

`3b3153e4a574056e0b12bc68a360995535f5dd61` 的 [三端 CI](https://github.com/ecojust/open-JARVIS/actions/runs/41186757582) 与 [alpha.3 候选构建](https://github.com/ecojust/open-JARVIS/actions/runs/41186795823) 均首次通过，无重试。每端执行同套测试、实际桌面/后端冒烟及冻结程序自检，然后生成未签名 DMG/EXE。冻结程序自检包含实际 Office 文件生成/重新打开、FFmpeg 与中文字体检查。Windows 无权限的符号链接用例仍明确跳过。

| alpha.3 目标 | Actions artifact | ZIP 大小 |
| --- | --- | --- |
| macOS Intel | [candidate-macos-x64](https://github.com/ecojust/open-JARVIS/actions/runs/41186795823/artifacts/13037057388) | 139,942,838 bytes |
| macOS Apple Silicon | [candidate-macos-arm64](https://github.com/ecojust/open-JARVIS/actions/runs/41186795823/artifacts/13037446030) | 127,816,156 bytes |
| Windows x64 | [candidate-windows-x64](https://github.com/ecojust/open-JARVIS/actions/runs/41186795823/artifacts/13038020100) | 109,314,567 bytes |

产物保留 14 天，未包含语音模型及用户数据，未创建或发布 GitHub Release。当前环境无法从 Actions 存储域下载产物，本地未重新打开 DMG/EXE；原生干净系统安装、真实音频和签名验收仍待完成。以下记录保留此前版本的实际证据。

## 本轮 Office、排班、视频与预览

实现提交 `b59fbbb` 新增真实 Office 文件、排班约束审计、PPT 中文配音视频、实际文件预览与跨任务产物引用。本地完整 **204 项测试通过**，ruff 通过；真实桌面/后端生命周期冒烟通过。新增 MAF 协议测试验证嵌套 Office 参数和 Media 委派，仍使用可控模型 HTTP 服务。

[六项实际任务验收](acceptance/2026-10-31/README.md) 全部通过：35 次真实工具调用、24 次官方页面读取、11 个唯一来源 URL；六次都自动打开对应任务的全屏实际文件预览。MP4 有约 100 秒的真实 Kokoro 配音，排班后续任务正确引用前面的 PPT。初轮发现的小屏视频布局问题已经修复，失败记录与全量重跑报告均保留。

这次验收明确为 `scripted_tool_workflow`，资料经事先检索核对；没有生产任务模型，不能代替自由自然语言规划验收。Linux offscreen 也不替代三端显示器、真实音频设备或系统播放器验收。

新增 Office/视频能力后的 [30 次方块启动复测](validation/linux-office-preview-2026-10-31.json)：P95 **249.1 ms**、方块 RSS P95 **73.7 MiB**，与此前约 248 ms/73.6 MiB 相近。测量仍只覆盖 Linux offscreen 方块，不含后端、语音模型或结果窗口，不能代替全软件性能验收。

首次新版 Windows CI 发现迁移测试自身未关闭手动创建的 SQLite 连接，导致临时目录清理出现文件占用；生产 Store 已显式关闭连接。`dbee278` 修复测试资源释放，18 项任务专项测试通过，随后在三端重新执行全部检查与候选构建。

`dbee278` 的独立 [三端 CI](https://github.com/ecojust/open-JARVIS/actions/runs/41185441738) 最终全部通过。Windows 首次运行另有一项原有线程取消测试超过其 3 秒等待时限；同提交的候选构建已通过该用例，独立 CI 仅重试失败的 Windows 一次后通过，未删减断言或放宽生产超时。这一测试时序抖动保留记录，不能算作真实任务性能基准。

`dbee278` 的 [alpha.2 未签名候选构建](https://github.com/ecojust/open-JARVIS/actions/runs/41185456490) 三端均成功：每端执行同套 204 项测试及桌面/后端冒烟，再运行冻结程序，实际生成并重新打开 Word/Excel/PPT、检查包内 FFmpeg、校验并绘制中文字体，然后生成 DMG/EXE。Windows 无权限的符号链接用例仍会明确跳过；没有把语音模型或用户数据打入安装包。该构建没有创建或发布 GitHub Release。

| alpha.2 目标 | Actions artifact | ZIP 大小 |
| --- | --- | --- |
| macOS Intel | [candidate-macos-x64](https://github.com/ecojust/open-JARVIS/actions/runs/41185456490/artifacts/13036139595) | 139,887,302 bytes |
| macOS Apple Silicon | [candidate-macos-arm64](https://github.com/ecojust/open-JARVIS/actions/runs/41185456490/artifacts/13036553880) | 127,120,998 bytes |
| Windows x64 | [candidate-windows-x64](https://github.com/ecojust/open-JARVIS/actions/runs/41185456490/artifacts/13036788550) | 109,238,241 bytes |

体积增加来自新增 Office 依赖、目标平台 FFmpeg 和中文字体；这些组件在功能使用时加载，方块启动复测见上文。原生安装与签名验收仍未完成；以下保留此前 alpha.1 的基线记录。

## 功能与协议

本地环境为 Linux x86_64、Python 3.12.14，Qt 使用 offscreen 插件，无可用麦克风/扬声器，系统缺少 PortAudio。它不属于本项目首版安装包发布目标。

此前 alpha.1 完整测试共 **132 项通过**，ruff 检查通过。自动化测试覆盖：

- 工作目录边界、符号链接、上传下载、CSV 统计、网络目标校验与真实文件写入。
- 幂等提交、有界队列、取消、超时、补充问题、跨轮预算、任务恢复与 SSE 重放。
- 真实 Microsoft Agent Framework 通过本地可控 OpenAI-compatible HTTP 服务，委派 Writer、保存文档、处理失败和取消。这不是生产模型质量或可用性测试。
- 原生 Qt 方块与按需结果窗口、流式事件拆包、通知与播报对应关系、设置、视图回收、首次退出。
- VAD 分段、唤醒前缀、免唤醒补充回答、音频 IPC、半双工状态、过期回调、TTS 缓存和取消。设备与播放测试使用明确的测试替身。
- 图标母版哈希、ICO/ICNS 导出、原生构建目标与产物清单校验。

`scripts/smoke_desktop.py` 另行启动真实 QApplication、桌面控制器与自有本地后端，通过 HTTP/SSE 提交命令，断言真实文件生成、结果窗口全屏和退出后子进程停止。它使用无模型的确定性命令演示模式，禁用朗读，不替代麦克风到扬声器的整链验收。

## 真实模型测量

固定依赖为 sherpa-onnx 1.13.8、SenseVoice INT8 与官方 Kokoro v1.1 多语言版，CPU 推理默认 2 线程。下载源、版本和 SHA-256 在两个模型下载模块中固定。

Kokoro 已生成有效、非空且采样均为有限值的中文音频；将其直接送入 SenseVoice，回识别为「你好，我是贾维斯。」。另一次较长样例回识别为「你好，我是贾维斯，报告已经准备好了。」。这是模型之间的文件/内存闭环，没有经过真实采集和播放，也不足以评价口音、噪声或语音自然度。

[原始 30 次启动与模型采样](validation/linux-development-2026-10-31.json) 的测量条件：Linux 6.18.57、x86_64、3 个逻辑 CPU、9.73 GiB 内存、Python 3.12.14、Qt offscreen，共享开发环境。

| 项目 | 实测 | 范围与限制 |
| --- | --- | --- |
| 方块启动至处理完一轮 UI 事件 | P50 211.6 ms / P95 247.8 ms | 30 次独立进程，仅方块，无后端/模型；不是三端屏幕交互基准 |
| 方块 RSS | P50 72.6 / P95 73.6 MiB | 单进程；不等于完整监听开销 |
| Kokoro 加载 | 1382 ms | 单次，不是冷磁盘 P95 |
| Kokoro 短句合成 | 1302 / 1506 / 1796 ms | 3 次约 2.07 秒音频；整段合成耗时，不是真实首声 |
| SenseVoice 加载 / 解码 | 789 / 101 ms | 单次，对上述合成音频解码；不含 VAD |
| 两模型同进程 RSS | 922.4 MiB | 加载与执行后的单次值，不含 GUI/后端，不是进程树峰值 |

**TTS 的整段热合成已超过设计中的热首声 1 秒目标。** 原生 Kokoro 回调存在进一步提前播放的空间，当前播放链仍在每个文本片段合成完成后开始播放。首段优先按自然停顿拆短，后续保持有界分段；短句不为追求数字插入无意义语音。必须在真实输出设备上重新测量提交到首声的时间，不能用模型回调或状态事件代替实际声音。

模型原生内存释放、持续监听 CPU、背景人声占空比、三进程总内存、真实 UI 延迟及 8 小时稳定性尚无合格基准。TTS 空闲释放引用未证明操作系统内存已回收；暂停监听时仍保留 STT 实例。当前数据不支持宣称达到全部性能预算。

## 三端与分发

已配置 macOS Intel、macOS arm64、Windows x64 的原生 CI、PyInstaller 目录式打包、DMG/EXE 生成和可选签名流程。PyPI 二进制依赖预检显示固定版本有对应 Python 3.12 wheel；Qt 6.12 的 Mac wheel 最低为 macOS 14，Windows/macOS 的 sounddevice wheel 包含 PortAudio。实现提交 `7c0fb76` 的 [三端 CI](https://github.com/ecojust/open-JARVIS/actions/runs/41183550016) 与 [候选安装包构建](https://github.com/ecojust/open-JARVIS/actions/runs/41183552457) 均已成功。三端完成同一套 132 项测试、原生依赖自检、真实桌面/后端冒烟；候选构建还运行了冻结后的程序，确认 Qt、音频、MAF 等原生导入与资源，之后才生成 DMG/EXE。Windows 无符号链接权限时，对应两项符号链接测试会明确跳过，其余路径边界测试继续执行。

| 目标 | 构建运行器 | 已上传 artifact | Actions ZIP 体积 |
| --- | --- | --- | --- |
| macOS Intel | macos-15-intel / x86_64 | candidate-macos-x64 | 88,617,437 bytes |
| macOS Apple Silicon | macos-15 / arm64 | candidate-macos-arm64 | 81,940,690 bytes |
| Windows x64 | windows-2022 / AMD64 | candidate-windows-x64 | 70,188,525 bytes |

包版本为 `0.1.0-alpha.1`，明确选择未签名构建；创建 Release 的步骤跳过。Actions artifact 保留 14 天，包含安装器、校验与清单，并单独保留桌面 smoke 和冻结自检证据。上述体积是 artifact ZIP，不是解压后安装占用。当前环境无法从 Actions 的存储域下载产物，因此这里只确认原生构建/自检与上传成功，未在本地重新打开 DMG/EXE。

原生验证过程中修复了 macOS `/var` 与 `/private/var`、Windows 短路径与长路径的测试期望差异，修正了音频尾音测试依赖固定等待的问题；打包增加目标平台 PortAudio 筛选和 `webrtcvad-wheels` 元数据 hook，并将 macOS 最低版本明确写为 14.0。

Linux 上的 `python -m jarvis --role self-test` 如实报告 sounddevice 原生导入失败，因为系统没有 PortAudio；其他原生模块成功导入。该自检不会隐藏设备依赖失败，也不会标记 `hardware_tested=true`。

发布前仍须完成三端安装、签名/公证、实际系统权限、显示缩放与全屏、真实音频设备、睡眠恢复、升级数据保留、生产模型和联网检索、第三方分发许可及性能验收。当前工作流默认不会发布正式版本。

## 复现

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m ruff check jarvis tests scripts packaging/entrypoint.py
python scripts/smoke_desktop.py
python scripts/measure_runtime.py --samples 30 --voice --output artifacts/performance.json
```

性能测量的 `--voice` 需要先下载两个模型。无显示服务的 Linux 上执行 UI 测试时设置 `QT_QPA_PLATFORM=offscreen`。原始测量输出默认不提交；需要保存基线时明确标注环境与测量范围后放入本目录。
