/* Same-origin UI. Source paths and server messages are always rendered as text. */
(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined && text !== null) result.textContent = String(text);
    return result;
  };
  const replace = (target, children) => target.replaceChildren(...children);
  const show = (target, visible) => { target.hidden = !visible; };
  const put = (id, text) => { byId(id).textContent = text ?? ""; };
  const pretty = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const number = (value) => Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN") : "—";
  const basename = (path) => String(path ?? "").replace(/\/+$/, "").split("/").pop();
  const joinPath = (parent, child) => `${String(parent).replace(/\/+$/, "")}/${child}`;
  const duration = (value) => {
    if (value === undefined || value === null || !Number.isFinite(Number(value))) return "—";
    const seconds = Math.max(0, Number(value));
    if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
    return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
  };
  const dateTime = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
  };

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }
  const errorMessage = (payload, fallback) => {
    const detail = payload?.detail ?? payload?.error;
    if (Array.isArray(detail)) return detail.map((entry) => {
      const path = Array.isArray(entry.loc) ? entry.loc.filter((key) => key !== "body").join(" / ") : "";
      return `${path ? `${path}：` : ""}${entry.msg ?? pretty(entry)}`;
    }).join("；");
    return typeof detail === "string" ? detail : fallback;
  };
  async function api(path, { method = "GET", body } = {}) {
    let response;
    try {
      response = await fetch(path, {
        method, credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new ApiError("无法连接服务器，请检查网络连接后重试。", 0);
    }
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { /* Non-JSON errors use a safe generic message. */ }
    if (!response.ok) throw new ApiError(errorMessage(payload, `请求失败（HTTP ${response.status}），请稍后重试。`), response.status);
    if (text && payload === null) throw new ApiError("服务器返回了无法识别的响应。", response.status);
    return payload;
  }

  const STORAGE_KEY = "data-web-platform.preferences.v1";
  const preferenceIds = ["provider", "source-root", "destination", "task", "fps", "wrench-fps", "target-version", "max-gap-ms", "command-max-age-ms", "gripper-max-gap-ms", "state-resampling", "trim-stationary", "motion-joint-threshold-rad", "motion-gripper-threshold", "workers", "video-workers", "provider-workers", "video-encoder", "deep-video"];
  const STATUS = { queued: "排队中", running: "运行中", succeeded: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
  const STAGE = { queued: "等待执行", preflight: "检查输入", editor_plan: "编辑计划", editor_apply: "数据集编辑", publish: "发布输出", check: "检查数据", check_prepare: "检查与准备", prepare: "对齐与准备", convert: "生成数据集", validate: "验证输出", doctor: "Doctor 诊断", score: "质量评分", gate: "训练准入", fix: "修复副本", trim: "裁剪副本", "merge-check": "合并预检" };
  const ACTIVE = new Set(["queued", "running"]);
  const conversionSummary = (job) => job.conversion_summary ?? job.result?.conversion_summary;
  const hasSkipped = (job) => job.kind === "convert" && (conversionSummary(job)?.skipped?.episodes ?? 0) > 0;
  const statusLabel = (job) => job.status === "succeeded" && hasSkipped(job) ? "已完成 · 跳过异常" : STATUS[job.status] ?? job.status;
  const state = {
    environment: null, preferences: {}, recordings: [], selection: new Set(), page: 0,
    loadedRoot: "", loadedProvider: "", loading: false, recordRequest: 0, submitting: false,
    jobs: [], activeJob: null, job: null, pollTimer: null, refreshingJobs: false,
    jobListSignature: "", resultSignature: "", recordResults: new Map(), datasets: [],
    selectedJobs: new Set(), selectedDatasets: new Set(), clearingJobs: false, removingDatasets: false,
    browse: { target: null, path: "", parent: null, trigger: null, sequence: 0, loading: false },
  };
  let toastTimer;
  function toast(message) {
    clearTimeout(toastTimer);
    put("toast", message);
    show(byId("toast"), true);
    toastTimer = setTimeout(() => show(byId("toast"), false), 4500);
  }
  function globalError(error) {
    put("global-error", error?.message ?? error ?? "");
    show(byId("global-error"), Boolean(error));
  }
  function setConnection(connected, message) {
    const target = byId("connection-status");
    target.classList.toggle("connected", connected);
    target.classList.toggle("disconnected", !connected);
    target.lastElementChild.textContent = message ?? (connected ? "已连接服务器" : "连接中断");
  }
  function badge(text, kind = "neutral") {
    return node("span", `badge ${["neutral", "blue", "running", "succeeded", "success", "failed", "error", "cancelled", "queued", "warning"].includes(kind) ? kind : "neutral"}`, text);
  }
  function empty(title, description, compact = false) {
    const container = node("div", `empty-state${compact ? " compact" : ""}`);
    container.append(node("h3", "", title), node("p", "", description));
    return container;
  }
  function savePreferences() {
    const values = {};
    for (const id of preferenceIds) {
      const input = byId(id);
      values[id] = input.type === "checkbox" ? input.checked : input.value;
    }
    state.preferences = values;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(values)); } catch { /* Private browsing/storage limits do not block work. */ }
  }
  function restorePreferences() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) state.preferences = saved;
    } catch { state.preferences = {}; }
    for (const id of preferenceIds) {
      if (id === "provider" || !Object.hasOwn(state.preferences, id)) continue;
      const input = byId(id);
      const value = state.preferences[id];
      if (input.type === "checkbox") input.checked = typeof value === "boolean" ? value : input.checked;
      else if (typeof value === "string" && value.length <= 8192 && (input.tagName !== "SELECT" || [...input.options].some((option) => option.value === value))) input.value = value;
    }
  }
  function setFieldError(id, message) {
    const target = byId(`${id}-error`);
    if (target) { target.textContent = message; show(target, Boolean(message)); }
    const input = byId(id);
    if (input) {
      if (message) {
        input.setAttribute("aria-invalid", "true");
        if (target) {
          const ids = new Set((input.getAttribute("aria-describedby") ?? "").split(" ").filter(Boolean));
          ids.add(target.id);
          input.setAttribute("aria-describedby", [...ids].join(" "));
        }
        const details = input.closest("details");
        if (details) details.open = true;
      } else input.removeAttribute("aria-invalid");
    }
  }
  const currentSource = () => byId("source-root").value.trim().replace(/\/+$/, "") || "/";
  const currentProvider = () => byId("provider").value;
  const providerSupported = (provider) => provider?.web_supported === true || (provider?.web_supported === undefined && provider?.name === "tianji");
  function updateProviderHelp() {
    const provider = state.environment?.providers?.find((item) => item.name === currentProvider());
    put("provider-help", provider?.description || "选择此环境已安装、且支持网页参数接口的 provider。");
  }
  function makeDefaultDestination(base) {
    const now = new Date();
    const stamp = [now.getFullYear(), now.getMonth() + 1, now.getDate()].map((value, index) => index ? String(value).padStart(2, "0") : value).join("") + "_" + [now.getHours(), now.getMinutes(), now.getSeconds()].map((value) => String(value).padStart(2, "0")).join("");
    return joinPath(base, `tianji_${stamp}`);
  }
  function renderEnvironment(environment) {
    state.environment = environment;
    const providers = Array.isArray(environment.providers) ? environment.providers : [];
    const chosen = currentProvider() || state.preferences.provider;
    const supported = providers.filter(providerSupported);
    const options = providers.map((provider) => {
      const option = node("option", "", `${provider.name}${provider.version ? ` · ${provider.version}` : ""}${providerSupported(provider) ? "" : " · 网页暂不支持"}`);
      option.value = provider.name;
      option.disabled = !providerSupported(provider);
      return option;
    });
    if (!options.length) options.push(node("option", "", "未发现可用 provider"));
    replace(byId("provider"), options);
    byId("provider").disabled = !supported.length;
    byId("provider").value = supported.some((item) => item.name === chosen) ? chosen : supported[0]?.name ?? "";
    if (!byId("source-root").value && environment.default_source) byId("source-root").value = environment.default_source;
    if (!byId("destination").value && environment.default_output) byId("destination").value = makeDefaultDestination(environment.default_output);
    updateProviderHelp();
    const fields = [["LeTools 源码", environment.letools_root], ["Python 解释器", environment.python], ["允许访问的根目录", (environment.roots ?? []).join("\n")], ["原始数据默认目录", environment.default_source], ["输出目录基准", environment.default_output], ["安全策略", "源数据只读；不删除、不拆分；只向新目录输出"]];
    replace(byId("environment-details"), fields.flatMap(([label, value]) => [node("dt", "", label), node("dd", "path-text", value || "未配置")]));
    const cards = providers.map((provider) => {
      const card = node("div", "provider-card");
      const heading = node("h3", "", provider.name);
      heading.append(badge(providerSupported(provider) ? "支持网页配置" : "已发现 · 尚未适配", providerSupported(provider) ? "blue" : "neutral"));
      card.append(heading, node("p", "field-help", provider.description || "未提供描述"), node("p", "path-text", `来源：${provider.origin || "未提供"}${provider.version ? ` · 版本 ${provider.version}` : ""}`));
      return card;
    });
    replace(byId("provider-list"), cards.length ? cards : [empty("没有发现 provider", "请确认服务器使用的 LeTools 环境与 provider 安装配置。", true)]);
    if (environment.error) globalError(new Error(`运行环境尚未就绪：${environment.error}`));
    setConnection(true, environment.error ? "已连接 · 环境待修复" : undefined);
    updateSelection();
  }
  async function refreshEnvironment() {
    byId("refresh-environment").disabled = true;
    try { renderEnvironment(await api("/api/environment")); }
    catch (error) { globalError(error); setConnection(false); }
    finally { byId("refresh-environment").disabled = false; }
  }

  function filteredRecordings() {
    const query = byId("recording-filter").value.trim().toLocaleLowerCase();
    return state.recordings.filter((recording) => !query || String(recording.name).toLocaleLowerCase().includes(query));
  }
  function updateSelection() {
    const count = state.selection.size;
    put("selection-summary", `已选择 ${number(count)} / ${number(state.recordings.length)} 条轨迹`);
    put("submit-summary", count ? `处理 ${number(count)} 条已选轨迹` : "尚未选择轨迹");
    byId("select-all").disabled = !filteredRecordings().length || state.loading;
    byId("select-none").disabled = !count || state.loading;
    const disabled = !count || state.loading || state.submitting || byId("provider").disabled || Boolean(state.environment?.error);
    byId("check-selected").disabled = disabled;
    byId("convert-selected").disabled = disabled;
  }
  function resultForRecording(recording) {
    return state.recordResults.get(recording.path) ?? state.recordResults.get(recording.name);
  }
  function renderRecordings() {
    const filtered = filteredRecordings();
    const perPage = 40;
    const pages = Math.max(1, Math.ceil(filtered.length / perPage));
    state.page = Math.min(state.page, pages - 1);
    const visible = filtered.slice(state.page * perPage, (state.page + 1) * perPage);
    const rows = visible.map((recording) => {
      const row = node("div", `record-row${state.selection.has(recording.path) ? " selected" : ""}`);
      const label = node("label", "record-label");
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selection.has(recording.path);
      checkbox.setAttribute("aria-label", `选择 ${recording.name}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selection.add(recording.path); else state.selection.delete(recording.path);
        row.classList.toggle("selected", checkbox.checked);
        updateSelection();
      });
      const text = node("span");
      text.append(node("span", "record-name", recording.name), node("span", "record-path", recording.path));
      label.append(checkbox, text);
      const actions = node("div", "record-actions");
      const result = resultForRecording(recording);
      const resultBadge = result ? badge(result.valid ? "检查通过" : "发现异常", result.valid ? "succeeded" : "failed") : badge("未检查", "neutral");
      if (result) resultBadge.title = "对应历史任务的结果；转换前仍会重新检查。";
      const check = node("button", "text-button", "检查");
      check.type = "button";
      check.disabled = state.submitting || Boolean(state.environment?.error);
      check.setAttribute("aria-label", `仅检查 ${recording.name}`);
      check.addEventListener("click", () => submitJob("check", [recording.path]));
      actions.append(resultBadge, check);
      row.append(label, actions);
      return row;
    });
    replace(byId("recordings"), rows.length ? rows : [empty(state.loadedRoot ? "没有匹配的录制" : "从一个录制目录开始", state.loadedRoot ? (state.recordings.length ? "尝试其他筛选关键词。" : "此目录未发现支持的录制。可重新选择目录。") : "选择原始数据目录并加载，然后勾选需要处理的轨迹。")]);
    put("recording-count", state.loadedRoot ? `${number(state.recordings.length)} 条录制` : "尚未加载");
    show(byId("recording-pagination"), pages > 1);
    put("recording-page-label", `第 ${state.page + 1} / ${pages} 页 · ${number(filtered.length)} 条`);
    byId("recording-prev").disabled = state.page === 0;
    byId("recording-next").disabled = state.page >= pages - 1;
    updateSelection();
  }
  function invalidateRecordings() {
    state.recordRequest += 1;
    state.loading = false;
    byId("load-recordings").disabled = false;
    byId("load-recordings").textContent = "加载录制";
    state.recordings = [];
    state.selection.clear();
    state.loadedRoot = "";
    state.loadedProvider = "";
    state.page = 0;
    renderRecordings();
  }
  async function loadRecordings() {
    const root = byId("source-root").value.trim();
    setFieldError("source-root", "");
    setFieldError("recordings", "");
    if (!root.startsWith("/")) { setFieldError("source-root", "请填写服务器上的绝对路径（以 / 开头）。"); byId("source-root").focus(); return; }
    if (byId("provider").disabled || !currentProvider()) { setFieldError("provider", "当前没有可用的网页 provider。"); return; }
    const request = ++state.recordRequest;
    const provider = currentProvider();
    state.loading = true;
    byId("load-recordings").disabled = true;
    byId("load-recordings").textContent = "正在读取…";
    updateSelection();
    try {
      const response = await api(`/api/recordings?${new URLSearchParams({ path: root, provider })}`);
      if (request !== state.recordRequest) return;
      state.recordings = Array.isArray(response?.recordings) ? response.recordings : [];
      state.loadedRoot = response.root || root;
      state.loadedProvider = provider;
      byId("source-root").value = state.loadedRoot;
      state.selection.clear();
      state.page = 0;
      savePreferences();
      renderRecordings();
      toast(`已加载 ${state.recordings.length} 条录制，请勾选需要处理的轨迹。`);
    } catch (error) {
      if (request === state.recordRequest) {
        state.recordings = [];
        state.selection.clear();
        state.loadedRoot = "";
        renderRecordings();
        setFieldError("source-root", error.message);
      }
    } finally {
      if (request === state.recordRequest) {
        state.loading = false;
        byId("load-recordings").disabled = false;
        byId("load-recordings").textContent = "加载录制";
      }
      updateSelection();
    }
  }

  const numericFields = [
    ["fps", "fps", "目标帧率", 1, 1000, true],
    ["wrench-fps", "wrench_fps", "Wrench采样频率", 1, 1000, true],
    ["max-gap-ms", "max_gap_ms", "观测最大间隔", 0, 1000, false],
    ["command-max-age-ms", "command_max_age_ms", "关节命令保持阈值", 0, 1000, false],
    ["gripper-max-gap-ms", "gripper_max_gap_ms", "夹爪命令保持阈值", 0, 1000, false],
    ["motion-joint-threshold-rad", "motion_joint_threshold_rad", "关节运动阈值", 0, 1, false],
    ["motion-gripper-threshold", "motion_gripper_threshold", "夹爪运动阈值", 0, 1, false],
  ];
  function buildRequest(kind, selected) {
    const errors = [];
    const fail = (id, message) => { errors.push({ id, message }); setFieldError(id, message); };
    for (const id of ["provider", "source-root", "recordings", "task", "destination", ...numericFields.map(([id]) => id), "workers", "video-workers", "provider-workers"]) setFieldError(id, "");
    show(byId("form-errors"), false);
    const task = byId("task").value.trim();
    const source = currentSource();
    const provider = currentProvider();
    if (!provider || byId("provider").disabled) fail("provider", "请选择支持网页配置的 provider。");
    if (!byId("source-root").value.trim().startsWith("/")) fail("source-root", "源目录必须是服务器上的绝对路径。");
    if (!state.loadedRoot || state.loadedRoot.replace(/\/+$/, "") !== source.replace(/\/+$/, "") || state.loadedProvider !== provider) fail("source-root", "源目录或 provider 已变化，请重新加载录制。");
    if (!selected.length) fail("recordings", "请至少选择一条录制。");
    if (!task || task.length > 4096) fail("task", "请填写任务指令（1–4096 个字符）。");
    const params = { task, state_resampling: byId("state-resampling").value, trim_stationary: byId("trim-stationary").checked, gap_policy: "error", preset: "thor-v1" };
    for (const [id, key, label, min, max, integer] of numericFields) {
      const raw = byId(id).value.trim();
      const value = Number(raw);
      if (!raw || !Number.isFinite(value) || (min === 0 ? value <= 0 : value < min) || value > max || (integer && !Number.isInteger(value))) fail(id, `${label}需${integer ? "为整数，" : ""}${min === 0 ? "大于 0" : `不小于 ${min}`}且不大于 ${max}。`);
      params[key] = value;
    }
    if (params.wrench_fps < params.fps || params.wrench_fps % params.fps) fail("wrench-fps", "Wrench频率必须≥主FPS，且为其整数倍（如50/200）。");
    const providerRaw = byId("provider-workers").value.trim();
    const providerWorkers = providerRaw ? Number(providerRaw) : null;
    if (providerWorkers !== null && (!Number.isInteger(providerWorkers) || providerWorkers < 1 || providerWorkers > 64)) fail("provider-workers", "留空自动，或填写1–64的整数。");
    const workers = Number(byId("workers").value);
    const videoWorkers = Number(byId("video-workers").value);
    for (const [id, value] of [["workers", workers], ["video-workers", videoWorkers]]) if (!Number.isInteger(value) || value < 1 || value > 8) fail(id, "workers 必须是 1–8 之间的整数。");
    const destination = byId("destination").value.trim().replace(/\/+$/, "");
    if (kind === "convert") {
      if (!destination.startsWith("/")) fail("destination", "请填写输出数据集的新绝对路径。");
      else if (destination === source || destination === "/") fail("destination", "输出目录不能是源目录或文件系统根目录，请使用新目录。");
    }
    if (errors.length) {
      const summary = byId("form-errors");
      replace(summary, [node("strong", "", `请修正 ${errors.length} 项输入后重试。`)]);
      const list = node("ul");
      for (const { id, message } of errors) {
        const item = node("li");
        const link = node("a", "", message);
        link.href = `#${id}`;
        link.addEventListener("click", (event) => { event.preventDefault(); (byId(id) ?? byId(`${id}-error`))?.focus(); });
        item.append(link);
        list.append(item);
      }
      summary.append(list);
      show(summary, true);
      summary.focus();
      return null;
    }
    return { kind, provider, source_root: state.loadedRoot, recordings: selected, destination: kind === "convert" ? destination : null, target_version: byId("target-version").value, params, workers, video_workers: videoWorkers, provider_workers: providerWorkers, video_encoder: byId("video-encoder").value, deep_video: byId("deep-video").checked };
  }
  async function submitJob(kind, selected = [...state.selection]) {
    if (state.submitting) return;
    const request = buildRequest(kind, selected);
    if (!request) return;
    state.submitting = true;
    updateSelection();
    savePreferences();
    try {
      const job = await api("/api/jobs", { method: "POST", body: request });
      state.activeJob = job.id;
      state.resultSignature = "";
      state.jobListSignature = "";
      toast(kind === "convert" ? "转换任务已提交：检查全部所选轨迹，跳过异常，只转换通过项。" : `已提交 ${selected.length} 条轨迹的检查。`);
      await refreshJobs();
      byId("jobs-title").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    } catch (error) {
      byId("form-errors").textContent = error.message;
      show(byId("form-errors"), true);
      byId("form-errors").focus();
    } finally { state.submitting = false; updateSelection(); }
  }

  const kindLabel = (job) => job.kind === "editor" ? (job.request?.operation === "plan" ? "编辑计划" : "编辑任务") : job.kind === "convert" ? "转换任务" : job.kind === "doctor" ? "Doctor 任务" : "检查任务";
  function renderJobList() {
    const signature = JSON.stringify(state.jobs.map((job) => [job.id, job.kind, job.status, hasSkipped(job), job.created_at, job.request?.recordings?.length])) + state.activeJob + [...state.selectedJobs].join();
    if (signature === state.jobListSignature) return;
    state.jobListSignature = signature;
    const items = state.jobs.map((job) => {
      const button = node("button", `job-item${job.id === state.activeJob ? " active" : ""}`);
      button.type = "button";
      if (job.id === state.activeJob) button.setAttribute("aria-current", "true");
      const heading = node("span", "job-item-heading");
      heading.append(node("strong", "", kindLabel(job)), badge(statusLabel(job), job.status === "succeeded" && hasSkipped(job) ? "warning" : job.status));
      const count = job.request?.recordings?.length;
      button.append(heading, node("small", "", `${count !== undefined ? `${count} 条轨迹 · ` : ""}${dateTime(job.created_at)}`), node("small", "path-text", String(job.id).slice(0, 12)));
      button.addEventListener("click", async () => {
        state.activeJob = job.id;
        state.resultSignature = "";
        renderJobList();
        try { await loadJobDetail(job.id); } catch (error) { globalError(error); }
      });
      const row = node("div", "job-select-row"), select = node("input");
      select.type = "checkbox"; select.checked = state.selectedJobs.has(job.id);
      select.disabled = ACTIVE.has(job.status) || state.clearingJobs;
      select.setAttribute("aria-label", `选择任务 ${String(job.id).slice(0, 12)}`);
      select.addEventListener("change", () => { if (select.checked) state.selectedJobs.add(job.id); else state.selectedJobs.delete(job.id); updateBulk(); });
      row.append(select, button); return row;
    });
    replace(byId("job-list"), items);
    show(byId("jobs-content"), Boolean(items.length));
    show(byId("jobs-empty"), !items.length);
    updateBulk();
  }
  const jobResults = (job) => Array.isArray(job.check_results) && job.check_results.length ? job.check_results : Array.isArray(job.check_report?.results) ? job.check_report.results : [];
  function updateRecordingResults(job) {
    let changed = false;
    const requestPaths = job.request?.recordings ?? [];
    for (const result of jobResults(job)) {
      const name = basename(result.bag ?? result.path ?? result.name);
      const original = requestPaths.find((path) => basename(path) === name || String(result.bag ?? "").endsWith(basename(path)));
      const key = original ?? result.path ?? result.bag ?? result.name;
      if (!key) continue;
      const old = state.recordResults.get(key);
      if (!old || old.valid !== result.valid || pretty(old.issues) !== pretty(result.issues)) { state.recordResults.set(key, result); changed = true; }
    }
    if (changed) renderRecordings();
  }
  function renderChecks(job) {
    const results = jobResults(job);
    const skipsInvalid = conversionSummary(job)?.policy === "valid_only";
    const signature = JSON.stringify(results) + job.id + skipsInvalid;
    if (state.resultSignature === signature) return;
    state.resultSignature = signature;
    const open = new Set([...byId("check-results").querySelectorAll("details[open]")].map((item) => item.dataset.path));
    const content = [];
    if (results.length) {
      const heading = node("div", "checks-heading");
      const passed = results.filter((result) => result.valid).length;
      heading.append(node("h4", "", "逐条检查结果"), badge(`${passed} 通过 · ${results.length - passed} ${skipsInvalid ? "异常跳过" : "异常"}`, passed === results.length ? "succeeded" : "warning"));
      content.push(heading);
      for (const [index, result] of results.entries()) {
        const item = node("details", "check-item");
        const path = String(result.bag ?? result.path ?? result.name ?? `轨迹 ${index + 1}`);
        item.dataset.path = path;
        item.open = open.has(path);
        const heading = node("summary");
        heading.append(badge(result.valid ? "通过" : skipsInvalid ? "跳过 · 异常" : "异常", result.valid ? "succeeded" : "failed"), node("span", "check-name", basename(path)));
        const issues = Array.isArray(result.issues) ? result.issues : [];
        const infoCount = issues.filter((issue) => issue.severity === "info").length;
        heading.append(node("span", "field-help", `${issues.length} 项记录${infoCount ? ` · ${infoCount} 条说明` : ""}`));
        const body = node("div", "check-body");
        body.append(node("p", "path-text", path));
        const summary = result.summary ?? result.alignment ?? {};
        const frames = result.frames ?? result.num_frames ?? summary.frames ?? summary.num_frames;
        if (frames !== undefined) body.append(node("p", "check-facts", `对齐后 ${number(frames)} 帧`));
        if (!issues.length) body.append(node("p", "field-help", result.valid ? "本次检查未报告异常。" : "检查未通过；详细原因请查看运行日志或下载报告。"));
        for (const issue of issues) {
          const severity = ["error", "warning", "info"].includes(issue.severity) ? issue.severity : "info";
          const row = node("div", `issue ${severity}`);
          const issueHeading = node("div", "issue-heading");
          issueHeading.append(badge({ error: "错误", warning: "警告", info: "说明" }[severity], severity === "info" ? "blue" : severity), node("code", "", issue.code ?? "检查信息"));
          row.append(issueHeading);
          if (issue.explanation && window.PlatformHelp) row.append(window.PlatformHelp.explanation(issue.explanation));
          const original = node("details"), originalTitle = node("summary", "", "原始诊断信息");
          original.append(originalTitle, node("p", "", issue.message ?? issue.description ?? "")); row.append(original);
          const details = Object.fromEntries(Object.entries(issue).filter(([key]) => !["severity", "code", "message", "description", "explanation"].includes(key)));
          if (Object.keys(details).length) row.append(node("pre", "issue-data", JSON.stringify(details, null, 2)));
          body.append(row);
        }
        const extra = Object.fromEntries(Object.entries(result).filter(([key]) => !["bag", "name", "path", "valid", "issues"].includes(key)));
        if (Object.keys(extra).length) {
          const facts = node("details", "check-metadata");
          facts.append(node("summary", "", "完整统计与对齐信息"), node("pre", "issue-data", JSON.stringify(extra, null, 2)));
          body.append(facts);
        }
        item.append(heading, body);
        content.push(item);
      }
    }
    replace(byId("check-results"), content);
  }
  function resultStat(label, value) {
    const item = node("div");
    item.append(node("strong", "", value), node("span", "", label));
    return item;
  }
  function renderConversionSummary(job) {
    const target = byId("conversion-summary");
    const summary = conversionSummary(job);
    show(target, job.kind === "convert" && Boolean(summary));
    if (job.kind !== "convert" || !summary) { target.replaceChildren(); return; }
    const table = node("table", "conversion-counts");
    table.append(node("caption", "", "转换前后数据量"));
    const head = node("tr");
    for (const text of ["范围", "Episodes", "累计轨迹时长"]) {
      const cell = node("th", "", text); cell.scope = "col"; head.append(cell);
    }
    const thead = node("thead"); thead.append(head); table.append(thead);
    const body = node("tbody");
    const rows = [["转换前 · 所选原始录制", summary.input],
      ...(summary.skipped ? [["跳过 · 检查异常", summary.skipped]] : []),
      ...(summary.accepted ? [["通过项 · 裁剪前", summary.accepted]] : []),
      ["转换后 · 已发布输出", summary.output]];
    for (const [label, group] of rows) {
      const row = node("tr"), heading = node("th", "", label); heading.scope = "row";
      const length = !group ? "尚未发布" : group.unknown_duration_episodes
        ? `已知 ${duration(group.known_duration_seconds)}；${group.unknown_duration_episodes} 条未知`
        : duration(group.duration_seconds);
      row.append(heading, node("td", "", group ? number(group.episodes) : "—"), node("td", "", length));
      body.append(row);
    }
    table.append(body);
    const note = node("p", "field-help", "原始时长按各录制 data/metadata.yaml 累计；输出按实际帧数 / FPS。差异包含跳过异常、共同区间对齐和首尾裁剪；任务处理耗时另列。未知时长不会当作 0。旧任务不补算这些统计。");
    target.replaceChildren(table, note);
  }
  function renderJob(job) {
    state.job = job;
    put("job-title", `${kindLabel(job)} · ${String(job.id).slice(0, 8)}`);
    const status = byId("job-status");
    status.textContent = statusLabel(job);
    status.className = `badge ${job.status === "succeeded" && hasSkipped(job) ? "warning" : Object.hasOwn(STATUS, job.status) ? job.status : "neutral"}`;
    const params = job.request?.params;
    const description = [dateTime(job.created_at), `耗时 ${duration(job.elapsed_seconds)}`];
    if (params) description.push(`${params.fps} FPS`, job.request.target_version ?? "v2.1");
    put("job-meta", description.join(" · "));
    // Current conversions fuse check and preparation. Keep legacy history
    // readable, but never invent two pending stages for an already completed
    // check_prepare event. Queued jobs use the current four-stage workflow.
    const observedStages = [...new Set((job.stages ?? []).map((stage) => stage.name))];
    const conversionStages = !observedStages.length || observedStages.includes("check_prepare")
      ? ["check_prepare", "convert", "validate", "publish"]
      : ["check", "prepare", "convert", "validate", ...observedStages.includes("publish") ? ["publish"] : []];
    const stageNames = job.kind === "convert" ? conversionStages : ["doctor", "editor"].includes(job.kind) ? observedStages : ["check"];
    if (!stageNames.length) stageNames.push(job.request?.operation ?? "doctor");
    byId("job-stages").classList.toggle("single-stage", stageNames.length === 1);
    replace(byId("job-stages"), stageNames.map((name, index) => {
      const stage = job.stages?.find((item) => item.name === name);
      const status = stage?.status ?? "queued";
      const step = node("div", `job-stage ${Object.hasOwn(STATUS, status) ? status : "queued"}`);
      step.append(node("span", "job-stage-name", `${String(index + 1).padStart(2, "0")} ${STAGE[name] ?? name}`), node("span", "job-stage-state", stage ? `${STATUS[status] ?? status}${stage.seconds !== undefined ? ` · ${duration(stage.seconds)}` : ""}` : job.status === "failed" || job.status === "cancelled" ? "未执行" : "等待执行"));
      return step;
    }));
    const progress = job.progress ?? {};
    const counted = ACTIVE.has(job.status) && ["check", "check_prepare", "prepare"].includes(job.stage) && Number.isFinite(progress.completed) && Number.isFinite(progress.total) && progress.total > 0;
    show(byId("job-progress-bar"), counted);
    if (counted) {
      byId("job-progress-bar").max = progress.total;
      byId("job-progress-bar").value = Math.min(progress.completed, progress.total);
      byId("job-progress-bar").setAttribute("aria-label", `${STAGE[job.stage]}进度`);
    }
    let progressText = job.status === "queued" ? "任务已进入队列，等待服务器执行。" : job.status === "running" ? `${STAGE[job.stage] ?? job.stage}进行中${counted ? `：${progress.completed} / ${progress.total} 条轨迹已完成` : "，当前阶段暂无可计数进度"}。` : job.status === "succeeded" ? "任务已完成。详细耗时与检查结果见下方。" : job.status === "cancelled" ? "任务已取消；原始数据未修改。" : "任务已停止，请查看错误与检查报告。";
    if (ACTIVE.has(job.status) && progress.current) progressText += ` 当前：${progress.current}`;
    if (job.status === "succeeded" && hasSkipped(job)) progressText = `转换已完成：跳过 ${conversionSummary(job).skipped.episodes} 条异常轨迹，仅发布通过项。跳过原因见逐条检查结果；源文件保留。`;
    put("job-progress", progressText);
    put("job-error", job.error ? (typeof job.error === "string" ? job.error : pretty(job.error)) : "");
    show(byId("job-error"), Boolean(job.error));
    const explanations = byId("job-explanations"); explanations.replaceChildren();
    if (job.kind === "check" && job.check_report?.deep_video === false) explanations.append(node("p", "notice", "本次为快速检查：未完整解码视频，不能据此确认所有视频帧健康；正式转换仍执行完整视频检查。"));
    if (window.PlatformHelp) {
      if (job.error_explanation) explanations.append(window.PlatformHelp.explanation(job.error_explanation));
      for (const item of job.validation_explanations || []) {
        const detail = node("details"), summary = node("summary", "", `${item.severity} · 输出验证说明`);
        detail.append(summary, window.PlatformHelp.explanation(item.explanation), node("pre", "issue-data", pretty(item.message))); explanations.append(detail);
      }
      const report = job.result?.report || job.check_report;
      if (report?.checks) { const block = node("div"); window.PlatformHelp.renderReport(block, report); explanations.append(block); }
    }
    show(byId("cancel-job"), ACTIVE.has(job.status));
    byId("cancel-job").disabled = false;
    const report = byId("download-report");
    report.href = `/api/jobs/${encodeURIComponent(job.id)}/report`;
    report.download = `${job.id}-report.json`;
    show(report, true);
    const result = job.result ?? {};
    const published = job.status === "succeeded" && Boolean(result.destination || result.dataset);
    const preview = job.kind === "editor" && job.request?.operation === "plan";
    // A failed mixed selection can have valid recordings but no output at all.
    // Label those counts as check results, never as published dataset contents.
    const episodes = published ? result.total_episodes ?? result.episodes
      : preview ? result.plan?.episodes : job.check_report?.valid_recordings;
    const frames = published ? result.total_frames ?? result.frames
      : preview ? result.plan?.frames : job.check_report?.valid_frames;
    const facts = [];
    if (typeof episodes === "number") facts.push(resultStat(published ? "输出轨迹" : preview ? "计划保留轨迹" : "通过检查的轨迹", number(episodes)));
    if (typeof frames === "number") facts.push(resultStat(published ? "输出帧数" : preview ? "计划保留帧数" : "有效对齐帧数", number(frames)));
    if (job.status === "succeeded") facts.push(resultStat("任务总耗时", duration(job.elapsed_seconds)));
    replace(byId("job-result"), facts);
    show(byId("job-result"), Boolean(facts.length));
    renderConversionSummary(job);
    show(byId("register-output"), ["convert", "editor"].includes(job.kind) && job.status === "succeeded" && Boolean(job.request?.destination));
    renderChecks(job);
    if (job.kind === "editor" && job.result) {
      const details = node("details", "check-metadata");
      details.append(node("summary", "", "编辑计划、警告与结果"), node("pre", "issue-data", JSON.stringify(job.result, null, 2)));
      byId("check-results").replaceChildren(details);
    }
    const logs = Array.isArray(job.logs) ? job.logs.map((line) => typeof line === "string" ? line : pretty(line)).join("\n") : String(job.logs ?? "");
    if (byId("job-logs").textContent !== logs) {
      const pane = byId("job-logs");
      const atBottom = pane.scrollHeight - pane.clientHeight - pane.scrollTop < 40;
      pane.textContent = logs || "暂无日志。";
      if (atBottom) pane.scrollTop = pane.scrollHeight;
    }
    updateRecordingResults(job);
  }
  async function loadJobDetail(id) {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (id === state.activeJob) renderJob(job);
  }
  function schedulePolling() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!document.hidden && state.jobs.some((job) => ACTIVE.has(job.status))) state.pollTimer = setTimeout(() => refreshJobs(true), 1000);
  }
  async function refreshJobs(silent = false) {
    if (state.refreshingJobs) return;
    state.refreshingJobs = true;
    byId("refresh-jobs").disabled = true;
    try {
      const result = await api("/api/jobs");
      state.jobs = Array.isArray(result) ? result : result?.jobs ?? [];
      state.selectedJobs = new Set([...state.selectedJobs].filter(id => state.jobs.some(job => job.id === id && !ACTIVE.has(job.status))));
      if (!state.jobs.some((job) => job.id === state.activeJob)) state.activeJob = state.jobs[0]?.id ?? null;
      renderJobList();
      if (state.activeJob) await loadJobDetail(state.activeJob);
      setConnection(true, state.environment?.error ? "已连接 · 环境待修复" : undefined);
    } catch (error) {
      if (!silent) globalError(error);
      setConnection(false);
    } finally {
      state.refreshingJobs = false;
      byId("refresh-jobs").disabled = false;
      schedulePolling();
    }
  }
  async function cancelJob() {
    const id = state.activeJob;
    if (!id || !ACTIVE.has(state.job?.status)) return;
    if (!window.confirm("确定取消此任务？原始数据不会被修改，已产生的任务缓存会保留。")) return;
    byId("cancel-job").disabled = true;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      if (state.activeJob === id) renderJob(job);
      await refreshJobs();
      toast("已发送取消请求。任务缓存保留，便于排查。");
    } catch (error) { globalError(error); byId("cancel-job").disabled = false; }
  }

  function renderDatasets() {
    put("dataset-count", number(state.datasets.length));
    const cards = state.datasets.map((dataset) => {
      const info = dataset.info ?? {};
      const card = node("article", "dataset-card");
      const selection = node("label", "dataset-select"), checkbox = node("input");
      checkbox.type = "checkbox"; checkbox.checked = state.selectedDatasets.has(dataset.id); checkbox.disabled = state.removingDatasets;
      checkbox.setAttribute("aria-label", `选择数据集 ${dataset.name} ${dataset.id}`);
      checkbox.addEventListener("change", () => { if (checkbox.checked) state.selectedDatasets.add(dataset.id); else state.selectedDatasets.delete(dataset.id); updateBulk(); });
      selection.append(checkbox, node("span", "", "选择此数据集")); card.append(selection);
      card.append(badge(info.codebase_version ?? "LeRobot", "blue"), node("h2", "", dataset.name || basename(dataset.path)), node("p", "dataset-path", dataset.path));
      const stats = node("div", "dataset-stats");
      stats.append(resultStat("轨迹", number(info.total_episodes)), resultStat("帧", number(info.total_frames)), resultStat("FPS", number(info.fps)));
      const actions = node("div", "dataset-actions");
      const viewer = node("a", "button primary", "查看数据集");
      viewer.href = `/viewer/local/${encodeURIComponent(dataset.id)}/0`;
      const doctor = node("a", "button secondary", "Doctor 诊断");
      doctor.href = `/doctor?${new URLSearchParams({ dataset: `local/${dataset.id}` })}`;
      const editor = node("a", "button secondary", "编辑数据集");
      editor.href = `/editor?${new URLSearchParams({ dataset: dataset.id })}`;
      const remove = node("button", "button danger", "移除登记"); remove.type = "button"; remove.disabled = state.removingDatasets;
      remove.addEventListener("click", () => removeDatasets([dataset.id]));
      actions.append(viewer, editor, doctor, remove);
      card.append(stats, actions);
      return card;
    });
    replace(byId("datasets-list"), cards.length ? cards : [empty("尚未登记数据集", "完成转换后添加输出目录，或登记已有的 LeRobot v2.1 / v3.0 数据集。")]);
    updateBulk();
  }
  async function refreshDatasets() {
    byId("refresh-datasets").disabled = true;
    try {
      const result = await api("/api/datasets");
      state.datasets = Array.isArray(result) ? result : result?.datasets ?? [];
      state.selectedDatasets = new Set([...state.selectedDatasets].filter(id => state.datasets.some(item => item.id === id)));
      renderDatasets();
    } catch (error) { globalError(error); }
    finally { byId("refresh-datasets").disabled = false; }
  }
  async function registerDataset(path, button) {
    setFieldError("dataset-path", "");
    if (!path.startsWith("/")) { setFieldError("dataset-path", "请输入服务器上的数据集绝对路径。"); byId("dataset-path").focus(); return; }
    if (button) button.disabled = true;
    try {
      await api("/api/datasets", { method: "POST", body: { path } });
      await refreshDatasets();
      toast("数据集已登记，可在“数据集”页面打开 Viewer 或 Doctor。");
    } catch (error) {
      if (button?.id === "add-dataset") setFieldError("dataset-path", error.message);
      else globalError(error);
    } finally { if (button) button.disabled = false; }
  }
  function updateBulk() {
    put("dataset-selection-count", `已选 ${state.selectedDatasets.size} 个数据集`);
    put("job-selection-count", `已选 ${state.selectedJobs.size} 条任务记录`);
    byId("datasets-remove").disabled = !state.selectedDatasets.size || state.removingDatasets;
    byId("jobs-clear").disabled = !state.selectedJobs.size || state.clearingJobs;
    byId("datasets-select-all").disabled = !state.datasets.length || state.removingDatasets;
    byId("jobs-select-all").disabled = !state.jobs.some(job => !ACTIVE.has(job.status)) || state.clearingJobs;
  }
  async function removeDatasets(ids = [...state.selectedDatasets]) {
    if (!ids.length || state.removingDatasets) return;
    state.removingDatasets = true; renderDatasets();
    try {
      const result = await api("/api/datasets/remove", {method:"POST",body:{ids}});
      ids.forEach(id => state.selectedDatasets.delete(id)); await refreshDatasets();
      toast(`已移除 ${result.removed.length} 个登记，数据文件保留，可按原路径重新添加。`);
    } catch (error) { globalError(error); }
    finally { state.removingDatasets = false; renderDatasets(); }
  }
  async function clearJobs() {
    const ids = [...state.selectedJobs]; if (!ids.length || state.clearingJobs) return;
    state.clearingJobs = true; updateBulk();
    try {
      const result = await api("/api/jobs/clear", {method:"POST",body:{ids}});
      state.selectedJobs.clear(); state.jobListSignature = ""; await refreshJobs();
      toast(`已清除 ${result.cleared.length} 条列表记录，报告、缓存和数据集文件保留。`);
    } catch (error) { globalError(error); }
    finally { state.clearingJobs = false; updateBulk(); }
  }
  async function loadDoctorCapabilities() {
    try {
      const result = await api("/api/doctor/options");
      const labels = { check: ["数据检查", "元数据、时间轴、动作、视频及训练就绪度"], score: ["质量评分", "读取数据质量评分与说明"], gate: ["训练准入", "按训练策略检查数据是否满足要求"], "merge-check": ["合并预检", "检查多数据集在合并前的兼容性"], fix: ["副本修复", "明确确认后写入新目录，不修改原始数据集"], trim: ["副本裁剪", "裁剪结果写入新目录，保留原始数据集"] };
      const operations = Array.isArray(result?.operations) ? result.operations : Array.isArray(result?.capabilities) ? result.capabilities : [];
      const cards = operations.map((entry) => {
        const name = typeof entry === "string" ? entry : entry.name ?? entry.id;
        const [title, description] = labels[name] ?? [name ?? "诊断工具", typeof entry === "object" ? entry.description ?? "" : ""];
        const card = node("div", "capability");
        card.append(node("strong", "", title), node("small", "", description));
        return card;
      });
      replace(byId("doctor-capabilities"), cards.length ? cards : [node("p", "field-help", "进入数据集的 Doctor 页面查看可用工具。")]);
      put("doctor-status", cards.length ? `${cards.length} 项工具` : "已连接");
      byId("doctor-status").className = "badge blue";
    } catch (error) {
      put("doctor-status", "暂不可用");
      replace(byId("doctor-capabilities"), [node("p", "field-help", error.message)]);
    }
  }

  function renderFolderSelection() {
    const folder = state.browse;
    put("folder-selection", folder.target === "destination" ? joinPath(folder.path, byId("folder-name").value.trim()) : folder.path);
  }
  let browseTimer;
  async function browse(path, typing = false) {
    clearTimeout(browseTimer);
    const sequence = ++state.browse.sequence;
    state.browse.loading = true;
    show(byId("folder-error"), false);
    byId("folder-select").disabled = true;
    byId("folder-parent").disabled = true;
    byId("folder-create-button").disabled = true;
    put("folder-count", "正在读取…");
    try {
      const response = await api(`/api/directories/suggest?${new URLSearchParams({ path })}`);
      if (sequence !== state.browse.sequence || !byId("folder-dialog").open) return;
      state.browse.path = response.path;
      state.browse.parent = response.parent;
      state.browse.exact = response.exact;
      if (!typing && response.exact) byId("folder-path").value = response.path;
      const directories = Array.isArray(response.directories) ? response.directories : response.dirs ?? [];
      const entries = directories.map((directory) => {
        const button = node("button", "folder-entry");
        button.type = "button";
        button.append(node("span", "", directory.name), badge(directory.is_dataset ? "LeRobot" : directory.is_recording ? "录制" : "目录", directory.is_dataset || directory.is_recording ? "blue" : "neutral"));
        button.addEventListener("click", () => browse(directory.path));
        return button;
      });
      replace(byId("folder-list"), entries.length ? entries : [empty(response.exact ? "没有子目录" : "没有匹配目录", response.exact ? "可以选择当前目录，或返回上一级。" : "继续输入或选择一个已有父目录；最终输出名在下方填写。", true)]);
      put("folder-count", `${directories.length} 个${response.exact ? "子目录" : "前缀匹配"}`);
      byId("folder-parent").disabled = !response.parent;
      byId("folder-select").disabled = !response.exact;
      byId("folder-create-button").disabled = !response.exact;
      renderFolderSelection();
    } catch (error) {
      if (sequence !== state.browse.sequence) return;
      put("folder-error", error.message);
      show(byId("folder-error"), true);
      put("folder-count", "读取失败");
      replace(byId("folder-list"), []);
    } finally { if (sequence === state.browse.sequence) state.browse.loading = false; }
  }
  function openFolder(target, trigger) {
    const roots = Array.isArray(state.environment?.roots) ? state.environment.roots : [];
    state.browse = { target, path: "", parent: null, trigger, sequence: state.browse.sequence + 1, loading: true };
    show(byId("folder-name-field"), target === "destination");
    show(byId("folder-create"), target === "destination");
    setFieldError("folder-name", "");
    put("folder-title", target === "destination" ? "选择输出父目录" : target === "dataset-path" ? "选择 LeRobot 数据集" : "选择原始录制目录");
    put("folder-help", target === "destination" ? "选择可写父目录，并输入一个尚不存在的数据集目录名称。" : "浏览服务器允许访问的目录；此操作不会修改源文件。");
    put("folder-select", target === "destination" ? "使用此输出路径" : "使用此目录");
    replace(byId("folder-roots"), roots.map((path) => {
      const button = node("button", "text-button", path);
      button.type = "button";
      button.addEventListener("click", () => browse(path));
      return button;
    }));
    let initial = byId(target).value.trim();
    if (target === "destination" && initial.startsWith("/")) {
      initial = initial.replace(/\/+$/, "");
      byId("folder-name").value = basename(initial) || "lerobot_v21";
      initial = initial.slice(0, initial.lastIndexOf("/")) || "/";
    } else if (target === "destination") byId("folder-name").value = byId("target-version").value === "v3.0" ? "lerobot_v30" : "lerobot_v21";
    if (!initial.startsWith("/")) initial = roots[0] || state.environment?.default_source || "/";
    byId("folder-path").value = initial;
    byId("folder-dialog").showModal();
    byId("folder-path").focus();
    browse(initial);
  }
  async function makeFolder() {
    if (state.browse.loading || !state.browse.exact) return;
    byId("folder-create-button").disabled = true;
    try {
      const result = await api("/api/directories", {method:"POST",body:{parent:state.browse.path,name:byId("folder-create-name").value}});
      byId("folder-create-name").value = ""; await browse(result.path);
      toast("分类目录已创建；请填写下方的全新数据集名称。");
    } catch (error) { put("folder-error", error.message); show(byId("folder-error"), true); }
    finally { byId("folder-create-button").disabled = !state.browse.exact; }
  }
  function setupPathSuggestions(id) {
    const input = byId(id), list = node("div", "path-suggestions");
    list.hidden = true; list.setAttribute("aria-label", "匹配的服务器目录");
    input.closest(".path-input").after(list);
    let timer, generation = 0;
    input.addEventListener("input", () => {
      clearTimeout(timer); const current = ++generation, path = input.value.trim();
      list.hidden = true;
      if (!path.startsWith("/")) return;
      timer = setTimeout(async () => {
        try {
          const result = await api(`/api/directories/suggest?${new URLSearchParams({path})}`);
          if (generation !== current || document.activeElement !== input) return;
          const choices = result.directories.slice(0, 40).map(dir => {
            const button = node("button", "text-button", dir.path); button.type = "button";
            button.addEventListener("click", () => { input.value = dir.path + "/"; input.focus(); input.dispatchEvent(new Event("input")); savePreferences(); });
            return button;
          });
          if (!choices.length) choices.push(node("p", "field-help", id === "destination" ? "没有匹配的已有目录；新输出名可直接保留，发布时创建。" : "没有匹配的已有目录。"));
          if (result.directories.length > 40) choices.push(node("p", "field-help", "仅显示前40项，请继续输入缩小范围。"));
          list.replaceChildren(...choices); list.hidden = false;
        } catch (error) {
          if (generation !== current || document.activeElement !== input) return;
          list.replaceChildren(node("p", "field-help", error.message)); list.hidden = false;
        }
      }, 220);
    });
    input.addEventListener("keydown", event => { if (event.key === "Escape") { generation++; clearTimeout(timer); list.hidden = true; } });
    document.addEventListener("pointerdown", event => { if (event.target !== input && !list.contains(event.target)) { generation++; list.hidden = true; } });
  }
  function updateWrenchWindow() {
    const fps = Number(byId("fps").value), force = Number(byId("wrench-fps").value);
    put("wrench-window-help", fps > 0 && force >= fps && force % fps === 0
      ? `每个主帧保存 ${force/fps} 个力样本，输出 shape=[${force/fps},12]；不滤波。`
      : "Wrench频率必须≥主FPS，且是它的整数倍。");
  }
  function updateEncodingChoice() {
    const messages = {auto:"探测四路NVENC；硬件不可用或运行中codec失败时，整批使用CPU x264（最多重试一次）。",libx264:"已选择CPU libx264：CRF18 / veryfast，不探测NVENC。",h264_nvenc:"已要求NVENC硬件编码：CQ16 / p4 / HQ，不可用时失败。"};
    put("encoding-choice-help", `${messages[byId("video-encoder").value]} 保留原尺寸，准备后不重复有损编码。`);
  }
  function acceptFolder() {
    const folder = state.browse;
    if (folder.loading || !folder.path || byId("folder-select").disabled) return;
    let path = folder.path;
    if (folder.target === "destination") {
      const name = byId("folder-name").value.trim();
      if (!name || name === "." || name === ".." || /[\/\\\u0000-\u001f]/.test(name)) {
        setFieldError("folder-name", "请输入一个目录名称，不可包含路径分隔符、控制字符或 . / ..。");
        byId("folder-name").focus();
        return;
      }
      path = joinPath(path, name);
    }
    byId(folder.target).value = path;
    setFieldError(folder.target, "");
    savePreferences();
    byId("folder-dialog").close();
    if (folder.target === "source-root") { invalidateRecordings(); loadRecordings(); }
  }
  function setView(name) {
    if (!["workbench", "datasets", "environment"].includes(name)) return;
    for (const view of document.querySelectorAll(".view")) view.hidden = view.id !== `view-${name}`;
    for (const button of document.querySelectorAll("[data-view]")) {
      const active = button.dataset.view === name;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    }
    if (name === "datasets") refreshDatasets();
  }

  restorePreferences();
  updateWrenchWindow();
  updateEncodingChoice();
  byId("video-encoder").addEventListener("change", updateEncodingChoice);
  for (const id of ["fps", "wrench-fps"]) byId(id).addEventListener("input", updateWrenchWindow);
  for (const id of ["source-root", "destination", "dataset-path"]) setupPathSuggestions(id);
  byId("datasets-select-all").addEventListener("click", () => { state.datasets.forEach(item => state.selectedDatasets.add(item.id)); renderDatasets(); });
  byId("datasets-select-none").addEventListener("click", () => { state.selectedDatasets.clear(); renderDatasets(); });
  byId("datasets-remove").addEventListener("click", () => removeDatasets());
  byId("jobs-select-all").addEventListener("click", () => { state.jobs.filter(item => !ACTIVE.has(item.status)).forEach(item => state.selectedJobs.add(item.id)); renderJobList(); });
  byId("jobs-select-none").addEventListener("click", () => { state.selectedJobs.clear(); renderJobList(); });
  byId("jobs-clear").addEventListener("click", clearJobs);
  for (const id of preferenceIds) byId(id).addEventListener("change", savePreferences);
  byId("source-root").addEventListener("input", () => { if (state.loadedRoot || state.loading) invalidateRecordings(); });
  byId("provider").addEventListener("change", () => { updateProviderHelp(); invalidateRecordings(); });
  byId("load-recordings").addEventListener("click", loadRecordings);
  byId("source-root").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadRecordings(); } });
  byId("recording-filter").addEventListener("input", () => { state.page = 0; renderRecordings(); });
  byId("select-all").addEventListener("click", () => { for (const recording of filteredRecordings()) state.selection.add(recording.path); renderRecordings(); });
  byId("select-none").addEventListener("click", () => { state.selection.clear(); renderRecordings(); });
  byId("recording-prev").addEventListener("click", () => { state.page -= 1; renderRecordings(); });
  byId("recording-next").addEventListener("click", () => { state.page += 1; renderRecordings(); });
  byId("conversion-form").addEventListener("submit", (event) => { event.preventDefault(); submitJob("convert"); });
  byId("check-selected").addEventListener("click", () => submitJob("check"));
  byId("refresh-jobs").addEventListener("click", () => refreshJobs());
  byId("cancel-job").addEventListener("click", cancelJob);
  byId("refresh-environment").addEventListener("click", refreshEnvironment);
  byId("refresh-datasets").addEventListener("click", refreshDatasets);
  byId("dataset-form").addEventListener("submit", (event) => { event.preventDefault(); registerDataset(byId("dataset-path").value.trim(), byId("add-dataset")); });
  byId("register-output").addEventListener("click", () => { if (state.job?.request?.destination) registerDataset(state.job.request.destination, byId("register-output")); });
  for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => setView(button.dataset.view));
  for (const button of document.querySelectorAll("[data-browse]")) button.addEventListener("click", () => openFolder(button.dataset.browse, button));
  byId("folder-path-form").addEventListener("submit", (event) => { event.preventDefault(); browse(byId("folder-path").value.trim()); });
  byId("folder-path").addEventListener("input", () => {
    clearTimeout(browseTimer); state.browse.sequence++; state.browse.loading = true; state.browse.exact = false;
    byId("folder-select").disabled = true; byId("folder-create-button").disabled = true;
    browseTimer = setTimeout(() => browse(byId("folder-path").value.trim(), true), 220);
  });
  byId("folder-create-button").addEventListener("click", makeFolder);
  byId("folder-parent").addEventListener("click", () => { if (state.browse.parent) browse(state.browse.parent); });
  byId("folder-name").addEventListener("input", renderFolderSelection);
  byId("folder-select").addEventListener("click", acceptFolder);
  byId("folder-cancel").addEventListener("click", () => byId("folder-dialog").close());
  byId("folder-dialog").addEventListener("close", () => { clearTimeout(browseTimer); state.browse.sequence += 1; state.browse.trigger?.focus(); });
  document.addEventListener("platform-help-ready", () => { if (state.job) { state.resultSignature = ""; renderJob(state.job); } });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearTimeout(state.pollTimer); state.pollTimer = null; }
    else if (state.jobs.some((job) => ACTIVE.has(job.status))) refreshJobs(true);
  });
  window.addEventListener("beforeunload", () => clearTimeout(state.pollTimer));
  Promise.allSettled([refreshEnvironment(), refreshJobs(), refreshDatasets(), loadDoctorCapabilities()]);
})();
