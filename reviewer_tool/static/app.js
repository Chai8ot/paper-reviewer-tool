const fileInput = document.querySelector("#fileInput");
const analyzeBtn = document.querySelector("#analyzeBtn");
const statusEl = document.querySelector("#status");
const metaEl = document.querySelector("#meta");
const figureList = document.querySelector("#figureList");
const detail = document.querySelector("#detail");
const workspace = document.querySelector("#workspace");
const tabs = Array.from(document.querySelectorAll(".tab[data-tab]"));
const historyList = document.querySelector("#historyList");
const lightbox = document.querySelector("#lightbox");
const lightboxImg = document.querySelector("#lightboxImg");
const closeLightbox = document.querySelector("#closeLightbox");

let result = null;
let selectedId = null;
let activeTab = "figures";
let historyItems = [];

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    activeTab = tab.dataset.tab;
    renderActiveTab();
  });
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files?.[0];
  analyzeBtn.disabled = !file;
  statusEl.textContent = file ? `已选择：${file.name}` : "等待上传文件";
});

analyzeBtn.addEventListener("click", async () => {
  const file = fileInput.files?.[0];
  if (!file) return;
  const data = new FormData();
  data.append("file", file);
  analyzeBtn.disabled = true;
  statusEl.textContent = "正在解析论文、抽取附图、生成全文对照和审稿意见。这一步可能需要几分钟。";
  metaEl.textContent = "";
  figureList.innerHTML = `<div class="empty"><h2>解析中</h2><p>正在渲染页面并定位图注。</p></div>`;
  detail.innerHTML = `<div class="empty detail-empty"><h2>请稍候</h2><p>结果生成后会自动显示第一张图。</p></div>`;

  try {
    const response = await fetch("/api/analyze", { method: "POST", body: data });
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || "解析失败");
    setResult(payload);
    statusEl.textContent = result.figure_count ? "解析完成" : "没有识别到 Figure/Fig. 图注";
    loadHistory();
  } catch (error) {
    statusEl.textContent = `解析失败：${error.message}`;
    figureList.innerHTML = `<div class="empty"><h2>解析失败</h2><p>${escapeHtml(error.message)}</p></div>`;
  } finally {
    analyzeBtn.disabled = false;
  }
});

function setResult(payload) {
  result = payload;
  selectedId = result.figures?.[0]?.id || null;
  renderMeta();
  renderActiveTab();
  renderHistory();
}

function renderList() {
  if (!result?.figures?.length) {
    figureList.innerHTML = `<div class="empty"><h2>没有识别到附图</h2><p>请确认图注以 Figure 1: 或 Fig. 1. 开头。</p></div>`;
    return;
  }
  figureList.innerHTML = "";
  for (const fig of result.figures) {
    const card = document.createElement("button");
    card.className = `figure-card ${fig.id === selectedId ? "active" : ""}`;
    card.type = "button";
    card.innerHTML = `
      <img src="${fig.thumb_url || fig.image_url}" alt="Figure ${escapeHtml(fig.figure_no)}" />
      <div>
        <h3>Figure ${escapeHtml(fig.figure_no)}</h3>
        <p>${escapeHtml(fig.caption_zh || fig.caption)}</p>
      </div>
    `;
    card.addEventListener("click", () => {
      selectedId = fig.id;
      renderList();
      renderDetail();
    });
    figureList.appendChild(card);
  }
}

function renderMeta() {
  if (!result) {
    metaEl.textContent = "";
    return;
  }
  const zotero = result.zotero_index_count ? `<br>Zotero：${result.zotero_index_count} 篇` : "";
  const paragraphs = Array.isArray(result.full_translation) ? `<br>全文段落：${result.full_translation.length}` : "";
  metaEl.innerHTML = `文件：${escapeHtml(result.file_name)}<br>页数：${result.page_count}<br>附图：${result.figure_count}${paragraphs}<br>模型：${escapeHtml(result.model || "offline")}${zotero}`;
}

function renderActiveTab() {
  tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === activeTab));
  if (activeTab === "figures") {
    workspace.className = "workspace";
    renderList();
    renderDetail();
    return;
  }
  workspace.className = "workspace single";
  figureList.innerHTML = "";
  if (activeTab === "summary") {
    renderSummary();
  } else if (activeTab === "fulltext") {
    renderFullText();
  } else if (activeTab === "innovations") {
    renderInnovations();
  } else if (activeTab === "literature") {
    renderLiteratureChecks();
  } else if (activeTab === "review") {
    renderReviewComments();
  }
}

function renderDetail() {
  const fig = result?.figures?.find((item) => item.id === selectedId);
  if (!fig) {
    detail.innerHTML = `<div class="empty detail-empty"><h2>选择一张图查看详情</h2><p>点击左侧图片后，会在这里显示清晰大图和关联文本。</p></div>`;
    return;
  }
  detail.innerHTML = `
    <div class="detail-head">
      <h2>Figure ${escapeHtml(fig.figure_no)}</h2>
      <span class="badge">第 ${fig.page} 页</span>
    </div>
    <img class="hero-image" src="${fig.image_url}" alt="Figure ${escapeHtml(fig.figure_no)} 大图" />
    <div class="text-block">
      <h3>图注</h3>
      <p>${escapeHtml(fig.caption)}</p>
    </div>
    <div class="text-block">
      <h3>图注翻译</h3>
      <p class="zh">${escapeHtml(fig.caption_zh || "暂无翻译")}</p>
    </div>
    <div class="text-block">
      <h3>相关正文段落</h3>
      <p>${escapeHtml(fig.paragraph || "未定位到相关正文段落")}</p>
    </div>
    <div class="text-block">
      <h3>段落翻译</h3>
      <p class="zh">${escapeHtml(fig.paragraph_zh || "暂无翻译")}</p>
    </div>
  `;
  detail.querySelector(".hero-image").addEventListener("click", () => {
    lightboxImg.src = fig.image_url;
    lightbox.hidden = false;
  });
}

function renderFullText() {
  const paragraphs = Array.isArray(result?.full_translation) ? result.full_translation : [];
  if (!paragraphs.length) {
    detail.innerHTML = `<div class="empty"><h2>暂无全文对照</h2><p>新解析的稿件会生成全文原文和中文译文对照；旧历史结果可重新解析以补齐此功能。</p></div>`;
    return;
  }
  const text = paragraphs.map((item) => `[Page ${item.page}] ${item.text}\n${item.text_zh || ""}`).join("\n\n");
  detail.innerHTML = `
    <section class="reading fulltext-reading">
      <div class="reading-head">
        <div>
          <h2>全文翻译对照</h2>
          <p class="subtle">左侧为原文，右侧为中文译文；译文始终显示，方便逐段核对。</p>
        </div>
        <span class="badge">${paragraphs.length} 段 · ${escapeHtml(result?.full_translation_source || "")}</span>
      </div>
      <div class="review-actions">
        <button class="secondary" id="downloadFullTextBtn" type="button">下载对照 TXT</button>
      </div>
      <div class="translation-grid">
        ${paragraphs.map((item, index) => `
          <article class="translation-row">
            <div class="translation-meta">P${escapeHtml(item.page)} · ${index + 1}</div>
            <div class="translation-original">${escapeHtml(item.text)}</div>
            <div class="translation-zh">${escapeHtml(item.text_zh || "暂无译文")}</div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
  document.querySelector("#downloadFullTextBtn").addEventListener("click", () => {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stem = (result?.file_name || "fulltext").replace(/\.[^.]+$/, "");
    link.href = url;
    link.download = `${stem}_全文翻译对照.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  });
}

function renderSummary() {
  const summary = result?.summary;
  if (!summary) {
    detail.innerHTML = `<div class="empty"><h2>暂无摘要</h2><p>上传并解析论文后会显示摘要。</p></div>`;
    return;
  }
  const findings = Array.isArray(summary.key_findings) ? summary.key_findings : [];
  detail.innerHTML = `
    <section class="reading">
      <div class="reading-head">
        <h2>论文摘要</h2>
        <span class="badge">${escapeHtml(result?.synthesis_source || "generated")}</span>
      </div>
      ${summary.one_sentence ? block("一句话概括", summary.one_sentence, true) : ""}
      ${summary.background ? block("研究背景", summary.background) : ""}
      ${summary.methods ? block("方法与数据", summary.methods) : ""}
      ${findings.length ? `<div class="text-block"><h3>关键发现</h3><ul class="point-list">${findings.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>` : ""}
      ${summary.conclusion ? block("主要结论", summary.conclusion) : ""}
    </section>
  `;
}

function renderInnovations() {
  const innovations = Array.isArray(result?.innovations) ? result.innovations : [];
  if (!innovations.length) {
    detail.innerHTML = `<div class="empty"><h2>暂无创新点</h2><p>上传并解析论文后会显示创新点分析。</p></div>`;
    return;
  }
  detail.innerHTML = `
    <section class="reading">
      <div class="reading-head">
        <h2>创新点分析</h2>
        <span class="badge">${innovations.length} 条</span>
      </div>
      <div class="innovation-list">
        ${innovations.map((item, index) => `
          <article class="innovation-item">
            <div class="innovation-index">${index + 1}</div>
            <div>
              <h3>${escapeHtml(item.title || `创新点 ${index + 1}`)}</h3>
              ${item.evidence ? `<p><strong>证据：</strong>${escapeHtml(item.evidence)}</p>` : ""}
              ${item.why_it_matters ? `<p><strong>意义：</strong>${escapeHtml(item.why_it_matters)}</p>` : ""}
              ${item.confidence ? `<span class="confidence">可信度：${escapeHtml(item.confidence)}</span>` : ""}
            </div>
          </article>
        `).join("")}
      </div>
    </section>
  `;
}

function renderLiteratureChecks() {
  const checks = Array.isArray(result?.literature_checks) ? result.literature_checks : [];
  if (!checks.length) {
    detail.innerHTML = `<div class="empty"><h2>暂无文献核查</h2><p>重新解析论文后会基于创新点检索 Zotero 本地库。</p></div>`;
    return;
  }
  detail.innerHTML = `
    <section class="reading">
      <div class="reading-head">
        <h2>文献核查</h2>
        <span class="badge">Zotero ${result?.zotero_index_count || 0} 篇 · ${escapeHtml(result?.literature_source || "")}</span>
      </div>
      <div class="innovation-list">
        ${checks.map((item, index) => `
          <article class="literature-item">
            <div class="literature-top">
              <h3>${index + 1}. ${escapeHtml(item.innovation_title || `创新点 ${index + 1}`)}</h3>
              <span class="status-pill ${statusClass(item.status)}">${escapeHtml(item.status || "证据不足")}</span>
            </div>
            ${item.verdict ? `<p class="verdict">${escapeHtml(item.verdict)}</p>` : ""}
            ${renderMatchedLiterature(item.matched_literature || item.candidates || [])}
            ${item.suggested_review_note ? `<div class="review-note"><strong>可写入审稿意见：</strong>${escapeHtml(item.suggested_review_note)}</div>` : ""}
          </article>
        `).join("")}
      </div>
    </section>
  `;
}

function renderReviewComments() {
  const review = result?.review_comments;
  if (!review) {
    detail.innerHTML = `<div class="empty"><h2>暂无审稿意见</h2><p>上传并解析论文后会生成符合国际期刊规范的英文审稿意见。</p></div>`;
    return;
  }
  const text = review.review_text || composeReviewText(review);
  detail.innerHTML = `
    <section class="reading review-reading">
      <div class="reading-head">
        <div>
          <h2>审稿意见</h2>
          <p class="subtle">英文稿可直接粘贴到国际期刊审稿系统，提交前请结合你的专业判断复核。</p>
        </div>
        <span class="badge">${escapeHtml(review.recommendation || "Recommendation")}</span>
      </div>
      <div class="review-actions">
        <button class="secondary" id="copyReviewBtn" type="button">复制全文</button>
        <button class="secondary" id="downloadReviewBtn" type="button">下载 TXT</button>
      </div>
      ${review.overall_assessment ? block("总体评价", review.overall_assessment) : ""}
      ${renderReviewList("主要问题", review.major_comments)}
      ${renderReviewList("次要问题", review.minor_comments)}
      ${renderReviewList("图表与表达", review.figure_comments)}
      ${review.confidential_comments ? block("给编辑的保密意见", review.confidential_comments) : ""}
      <div class="text-block">
        <h3>可提交英文全文</h3>
        <pre class="review-text">${escapeHtml(text)}</pre>
      </div>
    </section>
  `;
  document.querySelector("#copyReviewBtn").addEventListener("click", async () => {
    await navigator.clipboard.writeText(text);
    statusEl.textContent = "审稿意见已复制到剪贴板";
  });
  document.querySelector("#downloadReviewBtn").addEventListener("click", () => {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stem = (result?.file_name || "review").replace(/\.[^.]+$/, "");
    link.href = url;
    link.download = `${stem}_review_comments.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  });
}

function renderReviewList(title, items) {
  if (!Array.isArray(items) || !items.length) return "";
  return `
    <div class="text-block">
      <h3>${escapeHtml(title)}</h3>
      <ol class="point-list">
        ${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
      </ol>
    </div>
  `;
}

function composeReviewText(review) {
  const section = (title, body) => body ? `${title}\n${body}` : "";
  const list = (title, items) => Array.isArray(items) && items.length
    ? `${title}\n${items.map((item, index) => `${index + 1}. ${item}`).join("\n")}`
    : "";
  return [
    section("Recommendation", review.recommendation),
    section("Summary", review.manuscript_summary),
    section("Overall assessment", review.overall_assessment),
    list("Major comments", review.major_comments),
    list("Minor comments", review.minor_comments),
    list("Figures and presentation", review.figure_comments),
    section("Confidential comments to the editor", review.confidential_comments),
  ].filter(Boolean).join("\n\n");
}

function renderMatchedLiterature(items) {
  if (!Array.isArray(items) || !items.length) {
    return `<p class="muted-line">未检索到足够相关的本地文献片段。</p>`;
  }
  return `
    <div class="matched-list">
      ${items.slice(0, 4).map((lit) => `
        <div class="matched-item">
          <h4>${escapeHtml(lit.title || "未命名文献")}</h4>
          ${lit.path ? `<code>${escapeHtml(lit.path)}</code>` : ""}
          <p>${escapeHtml(lit.evidence || lit.snippet || "")}</p>
        </div>
      `).join("")}
    </div>
  `;
}

function statusClass(status) {
  if (String(status).includes("已报道")) return "reported";
  if (String(status).includes("部分")) return "partial";
  if (String(status).includes("未见")) return "clear";
  return "unknown";
}

function block(title, body, highlight = false) {
  return `<div class="text-block"><h3>${escapeHtml(title)}</h3><p class="${highlight ? "zh" : ""}">${escapeHtml(body)}</p></div>`;
}

closeLightbox.addEventListener("click", () => {
  lightbox.hidden = true;
  lightboxImg.removeAttribute("src");
});

lightbox.addEventListener("click", (event) => {
  if (event.target === lightbox) closeLightbox.click();
});

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function loadHistory() {
  try {
    const response = await fetch("/api/history");
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || "加载历史失败");
    historyItems = Array.isArray(payload.items) ? payload.items : [];
    renderHistory();
  } catch (error) {
    historyList.innerHTML = `<p class="muted-line">历史加载失败：${escapeHtml(error.message)}</p>`;
  }
}

function renderHistory() {
  if (!historyList) return;
  if (!historyItems.length) {
    historyList.innerHTML = `<p class="muted-line">暂无历史结果</p>`;
    return;
  }
  historyList.innerHTML = historyItems.map((item) => `
    <button class="history-item ${item.job_id === result?.job_id ? "active" : ""}" type="button" data-job-id="${escapeHtml(item.job_id)}">
      <span>${escapeHtml(item.file_name)}</span>
      <small>${formatHistoryMeta(item)}</small>
    </button>
  `).join("");
  historyList.querySelectorAll(".history-item").forEach((button) => {
    button.addEventListener("click", () => loadResult(button.dataset.jobId));
  });
}

function formatHistoryMeta(item) {
  const updated = item.updated_at ? new Date(item.updated_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
  const para = item.paragraph_count ? ` · ${item.paragraph_count} 段` : "";
  const rec = item.recommendation ? ` · ${item.recommendation}` : "";
  return `${updated} · ${item.page_count || 0} 页 · ${item.figure_count || 0} 图${para}${rec}`;
}

async function loadResult(jobId) {
  if (!jobId) return;
  statusEl.textContent = "正在加载历史解析结果";
  try {
    const response = await fetch(`/api/result?job_id=${encodeURIComponent(jobId)}`);
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || "加载失败");
    setResult(payload);
    statusEl.textContent = "已加载历史解析结果";
  } catch (error) {
    statusEl.textContent = `加载失败：${error.message}`;
  }
}

async function loadLatestIfRequested() {
  if (!new URLSearchParams(location.search).has("latest")) return;
  statusEl.textContent = "正在加载最近一次解析结果";
  try {
    const response = await fetch("/api/latest");
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || "加载失败");
    setResult(payload);
    statusEl.textContent = "已加载最近一次解析结果";
  } catch (error) {
    statusEl.textContent = `加载失败：${error.message}`;
  }
}

loadHistory();
loadLatestIfRequested();
