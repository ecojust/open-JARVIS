# 开发预览验证记录

日期：2026-10-31。此记录区分已经执行的检查与发布前仍需完成的验收。

## 功能与协议

本地环境为 Linux x86_64、Python 3.12.14，Qt 使用 offscreen 插件，无可用麦克风/扬声器，系统缺少 PortAudio。它不属于本项目首版安装包发布目标。

本轮完整测试共 **130 项通过**，ruff 检查通过。自动化测试覆盖：

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

已配置 macOS Intel、macOS arm64、Windows x64 的原生 CI、PyInstaller 目录式打包、DMG/EXE 生成和可选签名流程。PyPI 二进制依赖预检显示固定版本有对应 Python 3.12 wheel；Qt 6.12 的 Mac wheel 最低为 macOS 14，Windows/macOS 的 sounddevice wheel 包含 PortAudio。实际流水线结果以 [GitHub Actions](https://github.com/ecojust/open-JARVIS/actions) 为准。

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
