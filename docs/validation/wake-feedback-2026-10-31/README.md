# 唤醒提示与语音自动提交验收

2026-10-31，在 Linux x86_64、Python 3.12、Qt offscreen 环境完成。默认仍需先说“贾维斯”。声浪代表麦克风收到声音，不代表已经唤醒。

本次通过真实桌面控制器、独立音频 QProcess、生产 `AudioController.main()` 的标准输入/输出 JSON-lines、WebRTC VAD、SenseVoice、后端 HTTP/SSE 和 SQLite，验证录音到输入框再到 run 的完整路径。只在独立测试 worker 中将 `sounddevice.RawInputStream` 替换为按实时节奏回放 WAV 的设备；没有人工注入 STT 事件，也没有绕过启动时的 `discard_input` 请求与 ACK。

## 结果

| 录音 | partial | final | 持久化 run | 界面结果 |
| --- | ---: | ---: | ---: | --- |
| 不含唤醒词 | 0 | 0 | 0 | 语音条显示“等待唤醒”；输入框显示“先说‘贾维斯’唤醒”；输入框下显示“语音需先唤醒”；收音结束后持续显示“已收音，但还未唤醒…” |
| “贾维斯，帮我写一份本周工作总结，说明已经完成的任务和下一步计划。” | 4 | 1 | 1 | 真实输入框随 partial 更新；final 自动提交；run 保留在原 session，没有为 partial 或本轮语音另建 session |

两组都收到匹配真实请求 ID 的 `stt.discarded` 和 `audio.ack`，没有未完成的重置边界，也没有语音错误。无唤醒录音产生 58 个有效声浪事件，带唤醒录音产生 72 个。

- [无唤醒完整记录](without-wake/result.json)、[工作区截图](without-wake/waiting-wake.png)、[语音条截图](without-wake/waiting-wake-tile.png)。
- [带唤醒完整记录](with-wake/result.json)、[partial 输入截图](with-wake/voice-input.png)、[自动提交后的截图](with-wake/voice-run.png)。
- [旧行为基线](old-result.json)：音频链路取自 `59631c3d509cd2f4d68b6f5ff3ffd4565b9291eb`；无唤醒组使用该提交的两个音频模块源码快照。旧版无唤醒同样为 0 partial / 0 final / 0 run，`stt.cancelled` 原因为 `not_addressed`，且启动 ACK 正常。这支持问题来自未唤醒时缺少说明，而非本次设备回放中的 IPC 挂起。该基线不是旧版安装包测试。

每组 JSON 记录音频和执行开始时源码的 SHA-256。并行开发期间无关的配置模块可能变化，因此这些 hash 标识各次执行的来源，不等同于最终发布提交。

## 复现

需要已安装项目依赖、本地 SenseVoice 模型，以及单声道 16 kHz、16 bit PCM WAV；模型和录音不随本目录提交。工具默认唤醒词为“贾维斯”，可用 `--wake-word` 指定设置值，空字符串仍可测试用户显式关闭唤醒词的兼容路径。

```bash
python scripts/acceptance_voice_delivery.py \
  --audio-file /path/without-wake.wav \
  --stt-model-dir /path/sensevoice \
  --expected wake_required \
  --output artifacts/voice-without-wake

python scripts/acceptance_voice_delivery.py \
  --audio-file /path/with-wake.wav \
  --stt-model-dir /path/sensevoice \
  --expected accepted --expect-text 工作总结 \
  --output artifacts/voice-with-wake
```

本次带唤醒 WAV 由本地 Kokoro bilingual v1.1 合成，含静音共 8.52 秒；无唤醒 WAV 为同份录音去掉开头唤醒词后补静音，共 7.17 秒。实际音频 SHA-256 记录在各组 JSON 中。脚本使用隔离配置、临时数据和后端端口，固定不启用结果播报及自动弹窗，不使用生产任务模型账号。带唤醒 run 在演示模式返回需要配置模型的 `awaiting_input`，只证明自动提交及持久化成功，不声称实际工作总结已经生成。

## 性能与边界

[30 次语音条启动测量](performance.json)：启动 P95 为 238.49 ms，RSS P95 为 75.84 MiB。相较 alpha.10 同环境启动 P95 228.90 ms，约增加 4.2%。测量仅包含常驻语音条；没有加载后端、语音模型或 WebEngine，也不是 macOS/Windows 安装包性能验收。

本次未验证物理麦克风、扬声器、真实用户声学环境、生产任务模型 API，以及打包后的 macOS/Windows 客户端。回放验证补齐真实音频子进程与桌面输入/任务提交链路，不替代这些设备及发行包验收。
