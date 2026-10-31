# 安装包 MAF provider 遗漏修复

日期：2026-10-31。用户反馈 alpha.8 安装后发送任务时提示“MAF 组件未安装”，并要求运行 `python -m pip install -r requirements-agent.txt`。这是安装包依赖遗漏；安装版应包含运行时和任务依赖，用户无需安装 Python 或执行 pip。

## 根因

`agent_framework.openai` 是延迟加载的代理命名空间。仅执行 `import agent_framework.openai` 不会加载 `OpenAIChatCompletionClient`；读取这个符号时才动态导入独立顶层包 `agent_framework_openai`。

旧打包配置收集了 `agent_framework` 子模块，并复制 `agent-framework-openai` 的发行元数据，但没有收集 `agent_framework_openai` 实现代码。元数据存在不等于实现已打包。旧冻结自检只导入代理命名空间，因此能够通过；实际任务解析 provider 符号时才触发 `ModuleNotFoundError`，随后被转换为“MAF 组件未安装”。alpha.8 既有自检成功记录不能证明安装包中的真实 MAF 任务链路可执行。

修复明确收集 `agent_framework_openai` 的子模块，运行时代码直接从实现包导入 `OpenAIChatCompletionClient`。安装版组件缺失提示改为更新或重新安装完整客户端。源码开发者仍可按仓库开发说明安装依赖。MAF 仍在任务执行时加载，自检不会加入正常桌面启动路径。

## 已保存的验证证据

| 范围 | 结果 | 证据与边界 |
| --- | --- | --- |
| Linux 真实 PyInstaller 冻结导入对照 | 旧配置失败，补收 provider 后成功 | [comparison.json](comparison.json)；只检查实际运行时符号导入，不是完整客户端、任务执行或安装验收 |
| Linux 源码 MAF 本地任务 | 通过 | [source-agent-roundtrip.json](source-agent-roundtrip.json)；真实 MAF、HTTP/SSE 和文件工具，模型回复为本地固定测试数据，未使用外部服务 |
| 本地测试与静态检查 | 312 项测试、Ruff 通过 | 本轮源码检查；不代替三平台冻结程序执行 |
| macOS Intel 冻结任务自检 | 通过 | [三端构建记录](native-build.json)，冻结 worker 实际执行 MAF 任务检查 |
| macOS Apple Silicon 冻结任务自检 | 通过 | [三端构建记录](native-build.json)，冻结 worker 实际执行 MAF 任务检查 |
| Windows x64 冻结任务自检 | 通过 | [三端构建记录](native-build.json)，冻结 worker 实际执行 MAF 任务检查 |

### Linux 冻结导入对照

环境为 Python 3.12.14、PyInstaller 6.22.3、Linux x86_64。两份独立冻结程序运行同一个[探针](frozen-import-probe.py.txt)，两者都收集 `agent_framework`，都复制 core 和 OpenAI provider 元数据；唯一依赖收集差异是新增 `--collect-submodules agent_framework_openai`。

| 检查 | 旧配置 | 补收 provider |
| --- | --- | --- |
| `sys.frozen` | `true` | `true` |
| 导入 `agent_framework.openai` 代理命名空间 | 成功 | 成功 |
| 导入实际任务使用的 MAF/provider 符号 | 失败 | 成功 |
| 实际 provider | 缺少 `agent_framework_openai` | `agent_framework_openai._chat_completion_client` |

[旧输出](old-result.json)保留异常链，[新输出](new-result.json)记录成功加载的实现模块。[对照记录](comparison.json)中的 `probe_sha256` 对应本目录探针的原始字节。探针沿用旧代码的代理导入方式，有意证明仅补齐实现包便能消除这项冻结导入失败；生产修复同时采用直接实现包导入。

维护者可在装有上述构建依赖的隔离开发环境运行以下命令复现。两次构建使用相同探针，输出放在临时构建目录；这些是开发验证命令，客户端用户不需要执行。

```sh
mkdir -p /tmp/maf-freeze
cp docs/validation/bundled-maf-2026-10-31/frozen-import-probe.py.txt /tmp/maf-freeze/probe.py
python -m PyInstaller --noconfirm --name maf-probe-old \
  --distpath /tmp/maf-freeze/dist-old --workpath /tmp/maf-freeze/work-old \
  --specpath /tmp/maf-freeze \
  --collect-submodules agent_framework \
  --copy-metadata agent-framework-core --copy-metadata agent-framework-openai \
  /tmp/maf-freeze/probe.py
/tmp/maf-freeze/dist-old/maf-probe-old/maf-probe-old

python -m PyInstaller --noconfirm --name maf-probe-new \
  --distpath /tmp/maf-freeze/dist-new --workpath /tmp/maf-freeze/work-new \
  --specpath /tmp/maf-freeze \
  --collect-submodules agent_framework --collect-submodules agent_framework_openai \
  --copy-metadata agent-framework-core --copy-metadata agent-framework-openai \
  /tmp/maf-freeze/probe.py
/tmp/maf-freeze/dist-new/maf-probe-new/maf-probe-new
```

### 源码本地任务与新的发布检查

[源码记录](source-agent-roundtrip.json)来自未冻结的 Python 进程，保存平台、依赖版本、修复所在工作树的基线提交和相关源码 SHA-256。它通过应用 `Agent.run` 的已配置 MAF 路径运行，使用临时数据库、工作目录和仅监听 `127.0.0.1` 的 HTTP 服务，依次完成：

1. 总控向 writer 委派中文任务。
2. writer 接收流式工具调用，执行真实 `write_file`。
3. 文件工具结果返回 writer，由 writer 输出中文完成回答。
4. 委派结果返回总控，产生成功状态、界面文本和播报文本。

检查核对了四次模型请求、两次流式请求、一次成功文件工具调用，以及真实 UTF-8 文件的完整内容。HTTP 客户端忽略环境代理，并在每次请求前限制目标为本地测试服务；临时客户端替换在结束后恢复。任务和自检有超时，数据库连接与 HTTP 客户端结束后关闭，服务线程也被停止。该检查未访问用户模型账户或修改用户工作目录。

同一检查已纳入冻结 worker 的 `--role self-test`。构建脚本要求 `agent_roundtrip.ok` 为真，否则停止生成安装器；发布提升脚本也要求三个构建清单均包含成功的任务自检，防止继续复用仅通过旧导入检查的构建。三端构建 `41208267210` 与原生 CI `41208266939` 均已成功，源码 `00020ea1e9048fd0627616502f968799d733c3a7`，首次运行无重试。[native-build.json](native-build.json) 保存 job 步骤和候选产物身份；安装器生成前均实际执行冻结 worker 的完整检查。

本地固定回复仅提供可重复的模型协议测试输入；委派、MAF 执行、网络传输和文件写入均实际运行。它不验证生产模型自主规划质量、资料检索时效、六类业务产物、桌面预览、麦克风输入或扬声器输出。

## 升级与仍需配置的内容

本次修复不修改用户数据路径、设置格式或语音模型安装协议，已有设置和已下载模型继续沿用，无需因 provider 修复重新下载模型。首次使用仍须在客户端设置中填写任务模型接口、模型名称和 API Key，并通过界面下载语音识别与语音合成模型；客户端不会提供云端模型账户。

目标安装包为 macOS Intel、macOS Apple Silicon 和 Windows x64 的未签名预览版。macOS 公证、三端干净系统安装、真实麦克风／扬声器和全部性能预算仍未验收，不能由本轮局部证据推定通过。
