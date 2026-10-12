/* Shared contextual help. Text-only content, hover + focus + tap, Escape closes. */
(() => {
  "use strict";
  let catalog, active, closeTimer;
  const bubble = document.createElement("div");
  bubble.id = "parameter-help-popup"; bubble.role = "tooltip"; bubble.hidden = true;
  bubble.className = "parameter-help-popup";
  function hide() { clearTimeout(closeTimer); bubble.hidden = true; active = null; }
  function position() {
    if (!active) return;
    const box = active.getBoundingClientRect(), bounds = bubble.getBoundingClientRect();
    const left = Math.max(10, Math.min(box.left, innerWidth - bounds.width - 10));
    const top = box.bottom + bounds.height + 12 < innerHeight ? box.bottom + 8 : Math.max(8, box.top - bounds.height - 8);
    bubble.style.left = `${left}px`; bubble.style.top = `${top}px`;
  }
  function show(button, text) {
    clearTimeout(closeTimer); active = button;
    (button.closest("dialog") || document.body).append(bubble);
    bubble.textContent = text; bubble.hidden = false; position();
  }
  bubble.addEventListener("mouseenter", () => clearTimeout(closeTimer));
  bubble.addEventListener("mouseleave", () => { closeTimer = setTimeout(hide, 120); });
  document.addEventListener("keydown", event => { if (event.key === "Escape" && !bubble.hidden) { hide(); event.preventDefault(); event.stopPropagation(); } }, true);
  document.addEventListener("pointerdown", event => { if (active && !event.target.closest(".parameter-help,.parameter-help-popup")) hide(); });
  window.addEventListener("resize", position);
  document.addEventListener("scroll", () => { if (active) position(); }, true);
  function attach(root = document) {
    if (!catalog) return;
    const context = document.body.dataset.helpContext || "workbench";
    for (const input of root.querySelectorAll("input,select,textarea,[data-help-key]")) {
      if (input.dataset.helpAttached || input.type === "hidden") continue;
      let text = catalog[context]?.[input.dataset.helpKey || input.id || input.name];
      if (context === "doctor" && ["checks", "fixes"].includes(input.name)) text = catalog[input.name]?.[input.value];
      if (!text && context === "editor") {
        if (input.closest("#feature-options")) text = "勾选从新输出移除此字段，视频字段也会移除其视频和统计；系统索引不可删除。影响训练可读取的字段，原数据保留。";
        else if (input.closest("#camera-options")) text = "勾选将视频变换限制到该相机；一个都不勾选表示所有保留相机。不改变其他数值字段与时间轴。";
        else if (input.getAttribute("aria-label")?.includes("的新任务")) text = "为这个原episode ID修改任务文本，留空保留原任务。必须重新预览计划后执行；不改变帧数或信号。";
      }
      if (!text) continue;
      const label = input.closest("label") || (input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null);
      const title = label?.textContent?.trim() || input.getAttribute("aria-label") || input.dataset.helpKey || input.id || input.name;
      const button = document.createElement("button"); button.type = "button"; button.className = "parameter-help";
      button.textContent = "?"; button.setAttribute("aria-label", `参数说明：${title}`);
      button.setAttribute("aria-describedby", bubble.id);
      button.addEventListener("mouseenter", () => show(button, text));
      button.addEventListener("mouseleave", () => { closeTimer = setTimeout(hide, 180); });
      button.addEventListener("focus", () => show(button, text));
      button.addEventListener("blur", hide);
      button.addEventListener("click", event => { event.preventDefault(); show(button, text); });
      if (label) {
        const wrapper = document.createElement("span"); wrapper.className = "parameter-label";
        label.replaceWith(wrapper); wrapper.append(label, button);
      } else input.after(button);
      input.dataset.helpAttached = "true";
    }
  }
  function explanation(value) {
    const box = document.createElement("div"); box.className = "diagnostic-explanation";
    for (const [key, label] of [["title", ""], ["meaning", "含义"], ["evidence", "本条数据"], ["effect", "影响"], ["next_step", "建议"]]) {
      if (!value?.[key]) continue;
      const part = document.createElement(key === "title" ? "strong" : "p");
      part.textContent = `${label ? label + "：" : ""}${value[key]}`; box.append(part);
    }
    return box;
  }
  function renderReport(target, report) {
    target.replaceChildren();
    for (const check of report?.checks || []) {
      for (const item of check.messages || []) {
        if (!item.explanation) continue;
        const card = document.createElement("section");
        const heading = document.createElement("h3"); heading.textContent = `${item.severity} · ${check.name}`;
        const raw = document.createElement("details"), summary = document.createElement("summary"), text = document.createElement("pre");
        summary.textContent = "原始诊断信息"; text.textContent = item.message; raw.append(summary,text);
        card.append(heading, explanation(item.explanation), raw); target.append(card);
      }
    }
  }
  window.PlatformHelp = { attach, explanation, renderReport };
  fetch("/api/ui-help", {cache:"no-store"}).then(response => { if (!response.ok) throw Error(); return response.json(); })
    .then(data => { catalog = data; attach(); document.dispatchEvent(new Event("platform-help-ready")); })
    .catch(() => { /* Submission remains usable; surrounding labels still explain essentials. */ });
  let pending = false;
  new MutationObserver(() => { if (!pending) { pending = true; queueMicrotask(() => { pending = false; attach(); }); } })
    .observe(document.body, {childList:true, subtree:true});
})();
