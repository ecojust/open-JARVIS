"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const input = $("message-input");
  const messages = $("messages");
  const conversation = $("conversation");
  const synth = window.speechSynthesis;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const desktopMode = new URLSearchParams(window.location.search).get("desktop") === "1";
  const state = {
    configured: false,
    connected: false,
    busy: false,
    dashboard: { notes: [], todos: [], files: [] },
    tab: "files",
    recognition: null,
    listening: false,
    request: null,
    speaking: null,
    noticeTimer: null,
    uploading: false,
    stickToBottom: true,
  };
  const toolNames = {
    add_note: "记录笔记", create_note: "记录笔记", save_note: "记录笔记", list_notes: "查看笔记",
    add_todo: "添加待办", create_todo: "添加待办", list_todos: "查看待办", complete_todo: "完成待办", update_todo: "更新待办",
    read_file: "读取文件", write_file: "保存文件", list_files: "查看工作文件", get_time: "查询时间", current_time: "查询时间",
    calculate: "计算", search_notes: "查找笔记",
    web_search: "检索资料", search_web: "检索资料", fetch_url: "读取网页", fetch_page: "读取网页", read_webpage: "读取网页", analyze_csv: "分析 CSV 数据",
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showNotice(text, persistent = false) {
    clearTimeout(state.noticeTimer);
    $("notice").textContent = text;
    $("notice").hidden = !text;
    if (text && !persistent) state.noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 10000);
  }

  function setActivity(text, busy = state.busy) {
    $("activity-text").textContent = text;
    $("activity-dot").classList.toggle("busy", busy);
  }

  function scrollToEnd(force = false) {
    if (force || state.stickToBottom) {
      conversation.scrollTop = conversation.scrollHeight;
      state.stickToBottom = true;
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    $("send-button").disabled = busy;
    $("clear-history").disabled = busy;
    $("microphone").disabled = busy || !Recognition;
    $("upload-file").disabled = busy || state.uploading;
    document.querySelectorAll(".suggestion").forEach((button) => { button.disabled = busy; });
    input.setAttribute("aria-busy", String(busy));
    if (!busy) setActivity(state.connected ? "准备就绪，等待你的指令" : "服务连接不可用，请检查后重试", false);
  }

  function stopSpeaking() {
    if (synth) synth.cancel();
    state.speaking = null;
    $("stop-speaking").hidden = true;
  }

  function speak(text) {
    if (!synth || !text.trim()) return;
    stopSpeaking();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "zh-CN";
    utterance.rate = 1;
    const voices = synth.getVoices();
    const voice = voices.find((item) => /^zh[-_]CN$/i.test(item.lang)) || voices.find((item) => /^zh/i.test(item.lang));
    if (voice) utterance.voice = voice;
    state.speaking = utterance;
    const finish = () => {
      if (state.speaking === utterance) {
        state.speaking = null;
        $("stop-speaking").hidden = true;
      }
    };
    utterance.onend = finish;
    utterance.onerror = (event) => {
      finish();
      if (event.error !== "interrupted" && event.error !== "canceled") showNotice("浏览器未能朗读这条回复。请检查设备声音或浏览器语音设置。");
    };
    $("stop-speaking").hidden = false;
    synth.speak(utterance);
  }

  function addMessage(role, content = "", pending = false) {
    $("welcome").classList.add("compact");
    const article = element("article", `message ${role}${pending ? " pending" : ""}`);
    const avatar = element("div", "message-avatar", role === "user" ? "你" : "J");
    avatar.setAttribute("aria-hidden", "true");
    const main = element("div", "message-main");
    const header = element("div", "message-header");
    header.append(element("span", "message-author", role === "user" ? "你" : "JARVIS"));
    if (role === "assistant") header.append(element("span", "message-meta", "PERSONAL ASSISTANT"));
    const tools = element("div", "message-tools");
    const body = element("div", "message-content", content);
    main.append(header, tools, body);
    article.append(avatar, main);
    messages.append(article);
    const result = { article, main, body, tools, toolQueue: [], hasAnswer: Boolean(content) };
    if (role === "assistant" && content) addMessageActions(result);
    return result;
  }

  function addMessageActions(message) {
    if (desktopMode || !synth || message.main.querySelector(".message-actions")) return;
    const actions = element("div", "message-actions");
    const button = element("button", "text-button", "朗读这条回复");
    button.type = "button";
    button.addEventListener("click", () => speak(message.body.textContent));
    actions.append(button);
    message.main.append(actions);
  }

  function startTool(message, data) {
    const card = element("details", "tool-card running");
    const summary = element("summary", "tool-summary");
    const icon = element("span", "tool-state", "◌");
    const label = element("span", "tool-label", toolNames[data.name] || data.name || "执行任务");
    const badge = element("span", "tool-badge", "执行中");
    summary.append(icon, label, badge, element("span", "tool-arrow", "›"));
    const detail = element("pre", "tool-detail", `操作参数\n${JSON.stringify(data.arguments || {}, null, 2)}`);
    card.append(summary, detail);
    message.tools.append(card);
    message.toolQueue.push({ name: data.name, card, icon, badge, detail, finished: false });
    setActivity(`${label.textContent}…`, true);
    scrollToEnd();
  }

  function finishTool(message, data) {
    let tool = message.toolQueue.find((item) => item.name === data.name && !item.finished);
    if (!tool) {
      startTool(message, { name: data.name });
      tool = message.toolQueue[message.toolQueue.length - 1];
    }
    const result = data.result ?? {};
    const failed = Boolean(result.error) || result.ok === false || result.success === false;
    tool.finished = true;
    tool.card.classList.remove("running");
    tool.icon.textContent = failed ? "!" : "✓";
    tool.badge.textContent = failed ? "未完成" : "已完成";
    tool.badge.classList.toggle("error", failed);
    tool.detail.textContent += `\n\n执行结果\n${typeof result === "string" ? result : JSON.stringify(result, null, 2)}`;
    scrollToEnd();
  }

  async function jsonRequest(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`服务请求失败（${response.status}）`);
    return response.json();
  }

  function formatSize(bytes) {
    if (!Number.isFinite(bytes)) return "文件";
    return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
  }

  function renderWorkspace() {
    const panel = $("workspace-panel");
    panel.replaceChildren();
    const items = state.dashboard[state.tab] || [];
    const empty = { todos: "还没有待办。告诉 JARVIS，你接下来要做什么。", notes: "还没有笔记。让 JARVIS 帮你记住一个想法。", files: "工作目录暂时为空。试着让 JARVIS 保存一份文档。" };
    if (!items.length) panel.append(element("p", "empty-workspace", empty[state.tab]));
    items.forEach((item) => {
      const row = element("div", `workspace-item${state.tab === "todos" && item.done ? " done" : ""}`);
      const symbol = state.tab === "todos" ? (item.done ? "☑" : "□") : state.tab === "notes" ? "✧" : "▤";
      const icon = element("span", "item-icon", symbol);
      icon.setAttribute("aria-hidden", "true");
      const text = element("div", "item-text");
      const title = element("p", "item-title", state.tab === "files" ? item.path : item.title);
      if (state.tab === "todos") title.setAttribute("aria-label", `${item.done ? "已完成" : "未完成"}：${item.title}`);
      text.append(title);
      if (state.tab === "notes" && item.content) text.append(element("p", "item-description", item.content));
      if (state.tab === "files") text.append(element("p", "item-description", formatSize(item.size)));
      row.append(icon, text);
      if (state.tab === "files") {
        const download = element("a", "file-download", "↓");
        download.href = `/api/files?path=${encodeURIComponent(item.path)}`;
        download.download = item.path.split("/").pop();
        download.title = `下载 ${item.path}`;
        download.setAttribute("aria-label", download.title);
        row.append(download);
      }
      panel.append(row);
    });
    ["todos", "notes", "files"].forEach((tab) => { $(`${tab}-count`).textContent = String((state.dashboard[tab] || []).length); });
  }

  async function refreshWorkspace() {
    $("refresh-workspace").disabled = true;
    try {
      state.dashboard = await jsonRequest("/api/dashboard");
      renderWorkspace();
    } catch (error) {
      $("workspace-panel").replaceChildren(element("p", "empty-workspace", "暂时无法读取工作空间，点击右上角刷新重试。"));
    } finally {
      $("refresh-workspace").disabled = false;
    }
  }

  function selectTab(tab, focus = false) {
    state.tab = tab;
    document.querySelectorAll(".workspace-tab").forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    $("workspace-panel").setAttribute("aria-labelledby", `tab-${tab}`);
    renderWorkspace();
  }

  async function loadStatus() {
    try {
      const status = await jsonRequest("/api/status");
      state.configured = Boolean(status.configured);
      state.connected = true;
      $("connection-dot").className = `status-dot ${state.configured ? "connected" : "demo"}`;
      $("connection-name").textContent = state.configured ? "模型已配置" : "演示模式";
      $("model-name").textContent = state.configured ? status.model : "连接模型后，开启自然语言任务";
      $("mode-banner").hidden = state.configured;
      $("mode-message").textContent = "当前为演示模式：可执行 /search、/analyze、/write 等指令。配置模型后，即可用自然语言完成检索、写作和分析。输入 /help 查看指令。";
      $("workspace-location").textContent = status.workspace ? `文件目录：${status.workspace}` : "";
      $("workspace-location").title = $("workspace-location").textContent;
      input.placeholder = state.configured ? "告诉 JARVIS，你想做什么…" : "试试 /analyze example-sales.csv，或输入 /help…";
      setActivity("准备就绪，等待你的指令", false);
    } catch (error) {
      state.connected = false;
      $("connection-dot").className = "status-dot offline";
      $("connection-name").textContent = "服务未连接";
      $("model-name").textContent = "请确认 JARVIS 服务已启动";
      showNotice("无法连接本地服务，请确认服务已启动，然后刷新页面。", true);
      setActivity("服务连接不可用", false);
    }
  }

  async function loadHistory() {
    try {
      const history = await jsonRequest("/api/history");
      (history.messages || []).forEach((message) => {
        if (["user", "assistant"].includes(message.role) && message.content) addMessage(message.role, message.content);
      });
      scrollToEnd(true);
    } catch (error) {
      showNotice("历史对话暂时无法读取。你仍可以尝试发送新消息。");
    }
  }

  async function consumeSSE(response, onEvent) {
    if (!response.body) throw new Error("当前浏览器无法接收流式回复，请更新浏览器后重试。");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    function dispatch(block) {
      let name = "message";
      const lines = [];
      block.split(/\r?\n/).forEach((line) => {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        if (line.startsWith("data:")) lines.push(line.slice(5).replace(/^ /, ""));
      });
      if (!lines.length) return;
      let data;
      try { data = JSON.parse(lines.join("\n")); }
      catch (error) { throw new Error("收到无法识别的回复，请重试。"); }
      onEvent(name, data);
    }
    function drain() {
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        dispatch(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary[0].length);
      }
    }
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        drain();
      }
      buffer += decoder.decode();
      drain();
      if (buffer.trim()) dispatch(buffer);
    } finally {
      reader.releaseLock();
    }
  }

  function stopRecognition() {
    if (state.recognition && state.listening) state.recognition.stop();
  }

  async function sendMessage(text) {
    text = text.trim();
    if (!text || state.busy) return;
    stopRecognition();
    stopSpeaking();
    showNotice("");
    setBusy(true);
    input.value = "";
    resizeInput();
    addMessage("user", text);
    const reply = addMessage("assistant", "", true);
    scrollToEnd(true);
    setActivity("正在理解你的指令…", true);
    state.request = new AbortController();
    let hadError = false;
    let done = false;
    try {
      const response = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" }, body: JSON.stringify({ message: text }), signal: state.request.signal });
      if (!response.ok) {
        let detail;
        try { detail = await response.json(); } catch (error) { /* A proxy may return HTML. */ }
        throw new Error(typeof detail?.detail === "string" ? detail.detail : `请求未完成（${response.status}），请稍后重试。`);
      }
      state.connected = true;
      await consumeSSE(response, (event, data) => {
        if (event === "status") setActivity(data.message || "正在处理…", true);
        if (event === "tool_start") startTool(reply, data);
        if (event === "tool_result") finishTool(reply, data);
        if (event === "answer") {
          reply.body.textContent = data.content || "";
          reply.hasAnswer = Boolean(data.content);
          reply.article.classList.remove("pending");
          addMessageActions(reply);
          scrollToEnd();
        }
        if (event === "error") {
          hadError = true;
          reply.body.textContent = data.message || "任务未能完成，请稍后重试。";
          reply.article.classList.add("error");
          reply.article.classList.remove("pending");
        }
        if (event === "done") done = true;
      });
      if (!done && !hadError) throw new Error("连接提前结束，部分操作可能已经完成。请先查看工作空间，再决定是否重试。");
      if (!reply.hasAnswer && !hadError) throw new Error("没有收到任务总结。请查看上方执行结果或工作空间。");
      if (reply.hasAnswer && !hadError && $("read-aloud").checked) speak(reply.body.textContent);
    } catch (error) {
      if (error.name !== "AbortError") {
        hadError = true;
        reply.article.classList.add("error");
        const message = error instanceof TypeError ? "连接中断。部分操作可能已经完成，请查看工作空间后再重试。" : error.message;
        reply.body.textContent = reply.hasAnswer ? `${reply.body.textContent}\n\n${message}` : message;
      }
    } finally {
      reply.article.classList.remove("pending");
      reply.toolQueue.filter((tool) => !tool.finished).forEach((tool) => {
        tool.card.classList.remove("running");
        tool.icon.textContent = "?";
        tool.badge.textContent = "结果未确认";
      });
      state.request = null;
      setBusy(false);
      await refreshWorkspace();
      scrollToEnd();
      if (window.matchMedia("(min-width: 641px)").matches) input.focus();
    }
  }

  function resizeInput() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
  }

  async function uploadFile(file) {
    if (!file || state.busy || state.uploading) return;
    if (!/\.(csv|txt|md|json)$/i.test(file.name)) {
      showNotice("请选择 CSV、TXT、Markdown 或 JSON 文件。");
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      showNotice("文件超过 2 MB，请缩小文件后再上传。");
      return;
    }
    state.uploading = true;
    setBusy(true);
    setActivity(`正在上传 ${file.name}…`, true);
    try {
      const response = await fetch(`/api/files?path=${encodeURIComponent(file.name)}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
      if (!response.ok) {
        let error;
        try { error = await response.json(); } catch (_) { /* Keep a readable fallback. */ }
        throw new Error(typeof error?.detail === "string" ? error.detail : `文件上传失败（${response.status}）`);
      }
      const result = await response.json();
      showNotice(`已上传 ${result.path}。可以让 JARVIS 读取或分析这个文件。`);
      selectTab("files");
      await refreshWorkspace();
      if (!input.value.trim()) {
        input.value = state.configured ? `读取 ${result.path}，帮我分析并总结主要发现` : /\.csv$/i.test(result.path) ? `/analyze ${result.path}` : `/read ${result.path}`;
        resizeInput();
      }
      input.focus();
    } catch (error) {
      showNotice(error instanceof TypeError ? "文件上传连接失败，请检查服务后重试。" : error.message);
    } finally {
      state.uploading = false;
      setBusy(false);
      $("file-input").value = "";
    }
  }

  function setupVoice() {
    if (desktopMode) {
      $("microphone").hidden = true;
      $("read-aloud").closest("label").hidden = true;
      $("read-aloud").checked = false;
      document.querySelectorAll(".option-divider").forEach((node) => { node.hidden = true; });
      $("voice-hint").textContent = "语音由桌面客户端提供";
      return;
    }
    if (!synth) {
      $("read-aloud").disabled = true;
      $("read-aloud").closest("label").title = "当前浏览器不支持语音朗读";
    }
    if (!Recognition) {
      $("microphone").disabled = true;
      $("microphone").title = "此浏览器不支持语音识别，请使用 Chrome 或 Edge";
      $("microphone-label").textContent = "语音不可用";
      $("voice-hint").textContent = "语音输入需浏览器支持；建议使用 Chrome 或 Edge";
      return;
    }
    const recognition = new Recognition();
    recognition.lang = "zh-CN";
    recognition.continuous = false;
    recognition.interimResults = true;
    state.recognition = recognition;
    let baseText = "";
    recognition.onstart = () => {
      state.listening = true;
      $("microphone").classList.add("listening");
      $("microphone").setAttribute("aria-pressed", "true");
      $("microphone").title = "停止语音输入";
      $("microphone-label").textContent = "正在聆听";
      setActivity("正在聆听… 识别后可编辑并发送", true);
    };
    recognition.onresult = (event) => {
      let transcript = "";
      for (let index = 0; index < event.results.length; index++) transcript += event.results[index][0].transcript;
      if (state.busy) return;
      input.value = `${baseText}${baseText && transcript ? " " : ""}${transcript}`.slice(0, input.maxLength);
      resizeInput();
    };
    recognition.onend = () => {
      state.listening = false;
      $("microphone").classList.remove("listening");
      $("microphone").setAttribute("aria-pressed", "false");
      $("microphone").title = "点击开始语音输入";
      $("microphone-label").textContent = "语音输入";
      if (!state.busy) setActivity(input.value.trim() ? "识别结束，确认内容后点击发送" : "准备就绪，等待你的指令", false);
    };
    recognition.onerror = (event) => {
      const errors = {
        "not-allowed": "麦克风权限未获允许。请在浏览器地址栏中允许麦克风访问，并使用 localhost 或 HTTPS 打开页面。",
        "service-not-allowed": "浏览器的语音识别服务不可用，请检查浏览器设置或使用文字输入。",
        "audio-capture": "没有找到可用的麦克风，请检查设备连接。",
        "network": "浏览器语音识别网络连接失败，请检查网络或使用文字输入。",
        "no-speech": "没有听到声音，请点击麦克风再试一次。",
      };
      if (event.error !== "aborted") showNotice(errors[event.error] || "语音识别暂时不可用，请使用文字输入。");
    };
    $("microphone").addEventListener("click", () => {
      if (state.busy) return;
      if (state.listening) { recognition.stop(); return; }
      stopSpeaking();
      showNotice("");
      baseText = input.value.trim();
      try { recognition.start(); }
      catch (error) { showNotice("麦克风暂时无法启动，请稍后重试。"); }
    });
    $("voice-hint").textContent = "点击麦克风开始，识别后确认发送 · 语音可能由浏览器在线处理";
  }

  $("chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage(input.value); });
  input.addEventListener("input", resizeInput);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      sendMessage(input.value);
    }
  });
  document.querySelectorAll(".suggestion").forEach((button) => {
    button.addEventListener("click", () => {
      input.value = state.configured ? button.dataset.prompt : button.dataset.demo.replace(/\\n/g, "\n");
      resizeInput();
      input.focus();
    });
  });
  document.querySelectorAll(".workspace-tab").forEach((button) => {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      const tabs = ["todos", "notes", "files"];
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        selectTab(tabs[(tabs.indexOf(state.tab) + (event.key === "ArrowRight" ? 1 : 2)) % tabs.length], true);
      }
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        selectTab(event.key === "Home" ? tabs[0] : tabs[2], true);
      }
    });
  });
  $("refresh-workspace").addEventListener("click", refreshWorkspace);
  $("upload-file").addEventListener("click", () => { if (!state.busy) $("file-input").click(); });
  $("file-input").addEventListener("change", () => uploadFile($("file-input").files[0]));
  $("stop-speaking").addEventListener("click", stopSpeaking);
  $("read-aloud").addEventListener("change", () => { if (!$("read-aloud").checked) stopSpeaking(); });
  $("clear-history").addEventListener("click", async () => {
    if (state.busy || !messages.children.length) return;
    if (!window.confirm("清空聊天记录？笔记、待办和工作文件会保留。")) return;
    stopRecognition();
    setBusy(true);
    try {
      const response = await fetch("/api/history", { method: "DELETE" });
      if (!response.ok) throw new Error("清空失败");
      stopSpeaking();
      messages.replaceChildren();
      $("welcome").classList.remove("compact");
      showNotice("");
      setActivity("对话已清空，开始新的想法吧", false);
    } catch (error) { showNotice("暂时无法清空对话，请稍后重试。"); }
    finally { setBusy(false); }
  });
  window.addEventListener("pagehide", () => {
    if (state.recognition) state.recognition.abort();
    stopSpeaking();
    if (state.request) state.request.abort();
    clearTimeout(state.noticeTimer);
  });

  conversation.addEventListener("scroll", () => {
    state.stickToBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 220;
  }, { passive: true });
  window.jarvisRefresh = async () => {
    if (state.busy) return;
    setBusy(true);
    try {
      messages.replaceChildren();
      $("welcome").classList.remove("compact");
      await Promise.allSettled([loadStatus(), loadHistory(), refreshWorkspace()]);
      scrollToEnd(true);
    } finally { setBusy(false); }
  };
  setupVoice();
  setBusy(true);
  Promise.allSettled([loadStatus(), loadHistory(), refreshWorkspace()]).then(() => setBusy(false));
})();
