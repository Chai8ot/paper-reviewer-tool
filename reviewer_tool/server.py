#!/usr/bin/env python3
import cgi
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

try:
    import argostranslate.translate
except Exception:
    argostranslate = None


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
WORK = ROOT / "work"
UPLOADS = WORK / "uploads"
JOBS = WORK / "jobs"
PROGRESS = WORK / "progress"
ZOTERO_STORAGE = Path(os.environ.get("ZOTERO_STORAGE", "/Users/junchai/Zotero/storage"))
ZOTERO_INDEX = WORK / "zotero_index.json"
PYTHON = sys.executable
CODEX_BIN = shutil.which("codex") or "/Applications/Codex.app/Contents/Resources/codex"
CODEX_MODEL = os.environ.get("REVIEWER_TOOL_MODEL", "gpt-5.4")
USE_MODEL = os.environ.get("REVIEWER_TOOL_USE_MODEL", "1") != "0"
ANALYSIS_LOCK = threading.Lock()


def run(cmd, cwd=None, timeout=120):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{proc.stderr}")
    return proc.stdout


def ensure_dirs():
    for path in (WORK, UPLOADS, JOBS, PROGRESS):
        path.mkdir(parents=True, exist_ok=True)


def safe_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        return ""
    return value


def progress_path(progress_id):
    progress_id = safe_id(progress_id)
    if not progress_id:
        return None
    return PROGRESS / f"{progress_id}.json"


def read_progress(progress_id):
    path = progress_path(progress_id)
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "id": safe_id(progress_id),
        "status": "pending",
        "percent": 0,
        "stage": "等待开始",
        "message": "",
        "logs": [],
        "updated_at": time.time(),
    }


def update_progress(progress_id, percent, stage, message="", status="running"):
    path = progress_path(progress_id)
    if not path:
        return
    ensure_dirs()
    data = read_progress(progress_id)
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {stage}" + (f"：{message}" if message else "")
    logs = data.get("logs", [])
    logs.append(line)
    data.update({
        "id": safe_id(progress_id),
        "status": status,
        "percent": max(0, min(100, int(percent))),
        "stage": stage,
        "message": message,
        "logs": logs[-240:],
        "updated_at": time.time(),
    })
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def heartbeat_progress(progress_id, stage, message):
    if not progress_id:
        return
    data = read_progress(progress_id)
    update_progress(progress_id, data.get("percent", 0), stage, message, data.get("status", "running"))


def translate_zh(text):
    text = text.strip()
    if not text:
        return ""
    # Preserve citation spacing a bit better around the offline translator.
    try:
        if argostranslate is not None:
            return argostranslate.translate.translate(text, "en", "zh")
    except Exception:
        pass
    return text


def codex_text(prompt, source_text, timeout=300, progress_id=None, stage="模型调用"):
    if not USE_MODEL or not CODEX_BIN or not Path(CODEX_BIN).exists():
        raise RuntimeError("Codex 模型翻译未启用或不可用。")
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt", delete=False) as out:
        out_path = out.name
    try:
        started = time.time()
        proc = subprocess.Popen(
            [
                CODEX_BIN,
                "exec",
                "-m",
                CODEX_MODEL,
                "--skip-git-repo-check",
                "--cd",
                str(ROOT),
                "--output-last-message",
                out_path,
                prompt,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(source_text)
        proc.stdin.close()
        last_heartbeat = 0
        while proc.poll() is None:
            elapsed = int(time.time() - started)
            if elapsed > timeout:
                proc.kill()
                raise RuntimeError(f"{stage} 超时：超过 {timeout} 秒。")
            if progress_id and elapsed - last_heartbeat >= 10:
                last_heartbeat = elapsed
                heartbeat_progress(progress_id, stage, f"模型仍在生成，已等待 {elapsed} 秒")
            time.sleep(1)
        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        if proc.returncode != 0:
            raise RuntimeError(stderr or stdout)
        return Path(out_path).read_text(encoding="utf-8").strip()
    finally:
        try:
            Path(out_path).unlink()
        except OSError:
            pass


def parse_json_response(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not m:
            raise
        return json.loads(m.group(1))


def normalize_model_translation(text):
    return text.translate(str.maketrans({"\uf061": "α", "\uf067": "γ", "\uf06d": "μ", "\uf02d": "−"})).strip()


def translate_figure_texts(figures, progress_id=None):
    items = []
    for fig in figures:
        items.append({
            "id": fig["id"],
            "caption": fig.get("caption", ""),
            "paragraph": fig.get("paragraph", ""),
        })
    if not items:
        return figures
    prompt = (
        "你是材料科学论文审稿助手。请把 stdin 中 JSON 数组里的 caption 和 paragraph 翻译为高质量简体中文，"
        "面向中文审稿阅读。要求：只输出 JSON 数组；每项保留 id，并给出 caption_zh、paragraph_zh。"
        "保留变量、公式、化学式、单位、晶体学符号、Figure/Table 编号和文献引用如 [1,2]。"
        "术语：hydrogen embrittlement=氢脆；austenite=奥氏体；ferrite=铁素体；martensite=马氏体；"
        "glissile interface=可滑移界面；disconnection=位错台阶；misfit dislocation=错配位错；"
        "transformation induced plasticity=相变诱导塑性；deformation-induced=变形诱导。"
    )
    try:
        translated = parse_json_response(codex_text(
            prompt,
            json.dumps(items, ensure_ascii=False),
            timeout=420,
            progress_id=progress_id,
            stage="附图翻译",
        ))
        by_id = {item.get("id"): item for item in translated if isinstance(item, dict)}
        for fig in figures:
            item = by_id.get(fig["id"], {})
            fig["caption_zh"] = normalize_model_translation(item.get("caption_zh", "")) or translate_zh(fig.get("caption", ""))
            fig["paragraph_zh"] = normalize_model_translation(item.get("paragraph_zh", "")) or translate_zh(fig.get("paragraph", ""))
            fig["translation_source"] = "codex"
        return figures
    except Exception as e:
        for fig in figures:
            fig["caption_zh"] = translate_zh(fig.get("caption", ""))
            fig["paragraph_zh"] = translate_zh(fig.get("paragraph", ""))
            fig["translation_source"] = f"offline_fallback: {e}"
        return figures


def clean_article_text(page_texts, max_chars=42000):
    paras = []
    for page in page_texts:
        for para in paragraph_candidates(page):
            if re.match(r"^(Figure|Fig\.?|Table)\s+", para, re.I):
                continue
            if "Powered by Editorial Manager" in para:
                continue
            paras.append(para)
    text = "\n\n".join(paras)
    text = text.translate(str.maketrans({"\uf061": "α", "\uf067": "γ", "\uf06d": "μ", "\uf02d": "−"}))
    return text[:max_chars]


def article_paragraphs(page_texts):
    paragraphs = []
    seen = set()
    for page_no, page in enumerate(page_texts, 1):
        for para in paragraph_candidates(page):
            if re.match(r"^(Figure|Fig\.?|Table)\s+", para, re.I):
                continue
            if "Powered by Editorial Manager" in para:
                continue
            normalized = para.translate(str.maketrans({"\uf061": "α", "\uf067": "γ", "\uf06d": "μ", "\uf02d": "−"})).strip()
            key = re.sub(r"\W+", "", normalized.lower())[:220]
            if not normalized or key in seen:
                continue
            seen.add(key)
            paragraphs.append({
                "id": f"p-{len(paragraphs)+1}",
                "page": page_no,
                "text": normalized,
            })
    return paragraphs


def translate_article_paragraphs(page_texts, progress_id=None):
    paragraphs = article_paragraphs(page_texts)
    if not paragraphs:
        return {"paragraphs": [], "source": "empty"}
    translated = []
    source = "codex"
    prompt = (
        "你是材料科学论文全文翻译助手。请将 stdin JSON 数组中每个段落的 text 翻译为高质量简体中文。"
        "只输出 JSON 数组；每项保留 id，并给出 text_zh。"
        "要求：忠实原文，不增删科学结论；保留公式、变量、相名、化学式、单位、晶体学符号、图表编号和文献引用格式；"
        "若遇到疑似公式或残缺段落，尽量保持原有格式并翻译周围自然语言。"
    )
    try:
        for start in range(0, len(paragraphs), 18):
            chunk = paragraphs[start:start + 18]
            update_progress(
                progress_id,
                58 + int((start / max(1, len(paragraphs))) * 12),
                "全文翻译",
                f"正在翻译第 {start + 1}-{min(start + len(chunk), len(paragraphs))} 段，共 {len(paragraphs)} 段",
            )
            data = parse_json_response(codex_text(
                prompt,
                json.dumps(chunk, ensure_ascii=False),
                timeout=480,
                progress_id=progress_id,
                stage="全文翻译",
            ))
            if not isinstance(data, list):
                raise ValueError("模型输出不是 JSON array")
            by_id = {item.get("id"): item for item in data if isinstance(item, dict)}
            for para in chunk:
                item = by_id.get(para["id"], {})
                translated.append({
                    **para,
                    "text_zh": normalize_model_translation(item.get("text_zh", "")) or translate_zh(para["text"]),
                })
    except Exception as e:
        source = f"offline_fallback: {e}"
        translated = [{**para, "text_zh": translate_zh(para["text"])} for para in paragraphs]
    return {"paragraphs": translated, "source": source}


def build_summary_and_innovations(article_text, progress_id=None):
    fallback = {
        "summary": {
            "title": "摘要",
            "one_sentence": "",
            "background": "",
            "methods": "",
            "key_findings": [],
            "conclusion": "",
        },
        "innovations": [],
        "source": "empty",
    }
    if not article_text.strip():
        return fallback
    prompt = (
        "你是材料科学期刊审稿助手。请基于 stdin 中的论文正文，生成中文审稿辅助信息。"
        "只输出 JSON，格式为："
        "{\"summary\":{\"title\":\"\",\"one_sentence\":\"\",\"background\":\"\",\"methods\":\"\","
        "\"key_findings\":[\"\"],\"conclusion\":\"\"},"
        "\"innovations\":[{\"title\":\"\",\"evidence\":\"\",\"why_it_matters\":\"\",\"confidence\":\"高/中/低\","
        "\"search_terms\":[\"English technical term\"]}]}。"
        "要求：不要编造；尽量引用论文中的具体实验/模拟证据；保留专业术语、变量、相名和引用编号。"
        "创新点列 3-6 条，重点写相对于已有认识的新增贡献。search_terms 用英文，适合检索已有英文文献。"
    )
    try:
        data = parse_json_response(codex_text(
            prompt,
            article_text,
            timeout=420,
            progress_id=progress_id,
            stage="摘要与创新点",
        ))
        if not isinstance(data, dict):
            raise ValueError("模型输出不是 JSON object")
        data["source"] = "codex"
        return data
    except Exception as e:
        fallback["source"] = f"fallback: {e}"
        # Lightweight extractive fallback.
        first = article_text.split("\n\n")[:4]
        fallback["summary"]["one_sentence"] = translate_zh(" ".join(first[:1]))[:600]
        fallback["summary"]["background"] = translate_zh(" ".join(first[1:2]))[:800]
        fallback["summary"]["methods"] = translate_zh(" ".join(first[2:3]))[:800]
        fallback["summary"]["conclusion"] = translate_zh(" ".join(first[3:4]))[:800]
        return fallback


def extract_pdf_text(pdf_path, max_chars=80000):
    try:
        out = run(["pdftotext", "-layout", str(pdf_path), "-"], timeout=90)
        out = out.translate(str.maketrans({"\uf061": "α", "\uf067": "γ", "\uf06d": "μ", "\uf02d": "−"}))
        return out[:max_chars]
    except Exception:
        return ""


def load_zotero_index():
    if ZOTERO_INDEX.exists():
        try:
            return json.loads(ZOTERO_INDEX.read_text(encoding="utf-8"))
        except Exception:
            return {"items": {}}
    return {"items": {}}


def save_zotero_index(index):
    ZOTERO_INDEX.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def build_zotero_index():
    ensure_dirs()
    index = load_zotero_index()
    items = index.setdefault("items", {})
    pdfs = sorted(ZOTERO_STORAGE.glob("*/*.pdf"))
    seen = set()
    for pdf in pdfs:
        key = str(pdf)
        seen.add(key)
        try:
            stat = pdf.stat()
        except OSError:
            continue
        old = items.get(key)
        if old and old.get("mtime") == stat.st_mtime and old.get("size") == stat.st_size and old.get("text"):
            continue
        text = extract_pdf_text(pdf)
        items[key] = {
            "path": key,
            "title": pdf.stem,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "text": text,
        }
    for key in list(items):
        if key not in seen:
            items.pop(key, None)
    index["updated_at"] = time.time()
    save_zotero_index(index)
    return index


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "are", "was", "were", "has", "have",
    "hydrogen", "steel", "effect", "role", "study", "results", "using", "based", "phase", "interface",
}


def tokenize_query(text):
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9′'\\-]{2,}|[α-ωΑ-Ω][A-Za-z0-9′'\\-]*", text)
    out = []
    for token in tokens:
        t = token.lower().strip("-")
        if len(t) < 3 or t in STOPWORDS:
            continue
        out.append(t)
    return list(dict.fromkeys(out))


def innovation_terms(item):
    terms = []
    for term in item.get("search_terms", []) if isinstance(item.get("search_terms"), list) else []:
        terms.extend(tokenize_query(str(term)))
    terms.extend(tokenize_query(" ".join([item.get("title", ""), item.get("evidence", ""), item.get("why_it_matters", "")])))
    preferred = [
        "glissile", "disconnection", "disconnections", "martensite", "martensitic", "trip", "embrittlement",
        "stacking", "fault", "faults", "bcc", "fcc", "austenite", "ferrite", "mobility", "interface", "hydrogen",
        "segregation", "misfit", "molecular", "dynamics", "density", "functional", "theory",
    ]
    ranked = [x for x in preferred if x in terms] + [x for x in terms if x not in preferred]
    return ranked[:14]


def snippet_for_terms(text, terms, width=520):
    lower = text.lower()
    positions = [lower.find(term.lower()) for term in terms if term and lower.find(term.lower()) >= 0]
    if not positions:
        return re.sub(r"\s+", " ", text[:width]).strip()
    pos = min(positions)
    start = max(0, pos - width // 2)
    end = min(len(text), pos + width // 2)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def search_zotero_candidates(index, innovation, limit=6):
    terms = innovation_terms(innovation)
    candidates = []
    for item in index.get("items", {}).values():
        text = (item.get("title", "") + "\n" + item.get("text", "")).lower()
        if not text.strip():
            continue
        score = 0
        matched = []
        for term in terms:
            count = text.count(term.lower())
            if count:
                matched.append(term)
                score += min(count, 8) * (3 if term in item.get("title", "").lower() else 1)
        if score:
            candidates.append({
                "title": item.get("title", ""),
                "path": item.get("path", ""),
                "score": score,
                "matched_terms": matched[:10],
                "snippet": snippet_for_terms(item.get("text", ""), matched[:8]),
            })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:limit]


def assess_literature_overlap(innovations, progress_id=None):
    if not innovations:
        return {"checks": [], "index_count": 0, "source": "empty"}
    index = build_zotero_index()
    payload = []
    for i, innovation in enumerate(innovations, 1):
        candidates = search_zotero_candidates(index, innovation)
        payload.append({
            "innovation_id": i,
            "innovation": innovation,
            "candidates": candidates,
        })
    prompt = (
        "你是材料科学审稿助手。请判断论文创新点是否已在用户本地 Zotero 文献库中被已有文献报道。"
        "stdin 是 JSON 数组，每项含创新点和候选文献片段。请只输出 JSON："
        "[{\"innovation_id\":1,\"status\":\"可能已报道/部分相关/未见明显报道/证据不足\","
        "\"verdict\":\"中文判断，说明是否削弱创新性\","
        "\"matched_literature\":[{\"title\":\"\",\"path\":\"\",\"evidence\":\"候选片段如何支持判断\"}],"
        "\"suggested_review_note\":\"可直接用于审稿意见的一句话\"}]。"
        "要求：只能依据候选文献片段判断；不要把只共享普通背景术语的文献误判为已报道；"
        "如果候选片段只是主题相近但没有同一机制/结论，标为部分相关或证据不足。"
    )
    try:
        checks = parse_json_response(codex_text(
            prompt,
            json.dumps(payload, ensure_ascii=False),
            timeout=420,
            progress_id=progress_id,
            stage="文献核查",
        ))
        if not isinstance(checks, list):
            raise ValueError("模型输出不是 JSON array")
        by_id = {item.get("innovation_id"): item for item in checks if isinstance(item, dict)}
        normalized = []
        for i, item in enumerate(payload, 1):
            check = by_id.get(i, {})
            check.setdefault("innovation_id", i)
            check.setdefault("innovation_title", item["innovation"].get("title", f"创新点 {i}"))
            check.setdefault("candidates", item["candidates"][:3])
            normalized.append(check)
        return {"checks": normalized, "index_count": len(index.get("items", {})), "source": "codex"}
    except Exception as e:
        checks = []
        for i, item in enumerate(payload, 1):
            candidates = item["candidates"][:3]
            checks.append({
                "innovation_id": i,
                "innovation_title": item["innovation"].get("title", f"创新点 {i}"),
                "status": "证据不足" if not candidates else "部分相关",
                "verdict": "模型判断失败，以下仅为关键词检索结果，需要人工核读。",
                "matched_literature": [
                    {"title": c["title"], "path": c["path"], "evidence": c["snippet"]} for c in candidates
                ],
                "suggested_review_note": "",
                "candidates": candidates,
            })
        return {"checks": checks, "index_count": len(index.get("items", {})), "source": f"fallback: {e}"}


def build_review_comments(article_text, synthesis, literature, figures, progress_id=None):
    fallback = {
        "recommendation": "Major revision",
        "recommendation_zh": "大修",
        "manuscript_summary": "",
        "manuscript_summary_zh": "",
        "overall_assessment": "",
        "overall_assessment_zh": "",
        "major_comments": [],
        "major_comments_zh": [],
        "minor_comments": [],
        "minor_comments_zh": [],
        "figure_comments": [],
        "figure_comments_zh": [],
        "confidential_comments": "",
        "confidential_comments_zh": "",
        "review_text": "",
        "review_text_zh": "",
        "source": "empty",
    }
    if not article_text.strip():
        fallback["review_text"] = "The manuscript text could not be extracted reliably. Please inspect the PDF/Word file and rerun the analysis before preparing formal review comments."
        fallback["review_text_zh"] = "无法可靠抽取论文正文。请检查 PDF/Word 文件后重新解析，再准备正式审稿意见。"
        return fallback

    payload = {
        "article_text": article_text[:36000],
        "summary": synthesis.get("summary", {}),
        "innovations": synthesis.get("innovations", []),
        "literature_checks": literature.get("checks", []),
        "figures": [
            {
                "figure_no": fig.get("figure_no", ""),
                "caption": fig.get("caption", ""),
                "related_paragraph": fig.get("paragraph", ""),
            }
            for fig in figures[:12]
        ],
    }
    prompt = (
        "You are an experienced reviewer for an international materials science journal. "
        "Use stdin JSON to draft rigorous, fair, actionable peer-review comments in English. "
        "Return JSON only with this schema: "
        "{\"recommendation\":\"Accept/Minor revision/Major revision/Reject\","
        "\"recommendation_zh\":\"接受/小修/大修/拒稿\","
        "\"manuscript_summary\":\"\","
        "\"manuscript_summary_zh\":\"\","
        "\"overall_assessment\":\"\","
        "\"overall_assessment_zh\":\"\","
        "\"major_comments\":[\"\"],"
        "\"major_comments_zh\":[\"\"],"
        "\"minor_comments\":[\"\"],"
        "\"minor_comments_zh\":[\"\"],"
        "\"figure_comments\":[\"\"],"
        "\"figure_comments_zh\":[\"\"],"
        "\"confidential_comments\":\"\","
        "\"confidential_comments_zh\":\"\","
        "\"review_text\":\"\","
        "\"review_text_zh\":\"\"}. "
        "Standards: comments must match international journal review norms; be professional and evidence-based; "
        "start with a concise manuscript summary; evaluate significance, novelty, methodological soundness, data support, "
        "clarity, reproducibility, and literature positioning; identify whether novelty is weakened by the local literature-check results; "
        "write major comments as numbered, actionable revision requests; write minor comments for clarity, terminology, references, figures, and formatting. "
        "Do not invent details not present in the manuscript. If evidence is insufficient, say what must be provided. "
        "Keep formulas, variables, phase names, chemical symbols, units, and references unchanged. "
        "The review_text field must be a ready-to-submit English review with sections: Recommendation, Summary, Overall assessment, Major comments, Minor comments, Figures and presentation, Confidential comments to the editor. "
        "All *_zh fields must be faithful Simplified Chinese counterparts for side-by-side comparison, preserving formulas, variables, symbols, units, and references."
    )
    try:
        data = parse_json_response(codex_text(
            prompt,
            json.dumps(payload, ensure_ascii=False),
            timeout=480,
            progress_id=progress_id,
            stage="审稿意见",
        ))
        if not isinstance(data, dict):
            raise ValueError("模型输出不是 JSON object")
        for key, value in fallback.items():
            data.setdefault(key, value)
        data["source"] = "codex"
        return data
    except Exception as e:
        summary = synthesis.get("summary", {}) if isinstance(synthesis, dict) else {}
        checks = literature.get("checks", []) if isinstance(literature, dict) else []
        novelty_notes = [
            c.get("suggested_review_note") or c.get("verdict", "")
            for c in checks
            if c.get("status") and not str(c.get("status")).startswith("未见")
        ]
        major = [
            "The authors should clarify the precise novelty of the work against the most relevant prior literature and explicitly state what mechanistic or methodological advance is provided.",
            "The manuscript should strengthen the evidence linking the reported observations to the central mechanistic interpretation, including clearer controls, quantitative comparisons, and uncertainty estimates where applicable.",
            "The reproducibility of the experimental/computational procedure should be improved by providing sufficient details on sample preparation, analysis parameters, and data-processing criteria.",
        ]
        if novelty_notes:
            major.insert(0, "The novelty claim requires careful revision because the local literature check identified potentially related prior work. The authors should compare directly with those studies and define the remaining advance.")
        minor = [
            "Please define all abbreviations at first use and keep terminology consistent throughout the manuscript.",
            "Please check figure labels, units, scale bars, and caption completeness so that each figure can be understood independently.",
            "The English expression should be polished for concision and journal style before resubmission.",
        ]
        review_text = "\n\n".join([
            "Recommendation: Major revision",
            f"Summary: {summary.get('one_sentence', 'The manuscript addresses a materials-science problem, but the extracted text was insufficient for a fully model-generated review.')}",
            "Overall assessment: The topic appears potentially suitable for an international journal, but the manuscript requires clearer positioning, stronger evidence, and improved presentation before it can be judged conclusively.",
            "Major comments:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(major, 1)),
            "Minor comments:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(minor, 1)),
            "Figures and presentation: The figures should be checked for readability, complete captions, scale bars, panel labels, and consistency with the claims in the text.",
            f"Confidential comments to the editor: Automated drafting fell back to a conservative template because the model call failed: {e}",
        ])
        fallback.update({
            "recommendation_zh": "大修",
            "manuscript_summary": summary.get("one_sentence", ""),
            "manuscript_summary_zh": translate_zh(summary.get("one_sentence", "")),
            "overall_assessment": "The manuscript requires clearer novelty positioning and stronger evidence before publication can be recommended.",
            "overall_assessment_zh": "在建议发表之前，稿件需要更清晰地界定创新性，并提供更有力的证据支持。",
            "major_comments": major,
            "major_comments_zh": [translate_zh(x) for x in major],
            "minor_comments": minor,
            "minor_comments_zh": [translate_zh(x) for x in minor],
            "figure_comments": ["Check figure readability, captions, scale bars, panel labels, and consistency with the claims in the text."],
            "figure_comments_zh": ["请检查图件可读性、图注、比例尺、分图标签，以及其与正文论断的一致性。"],
            "confidential_comments": f"Model drafting failed; fallback template used. Error: {e}",
            "confidential_comments_zh": f"模型生成失败，已使用保守模板。错误：{e}",
            "review_text": review_text,
            "review_text_zh": translate_zh(review_text),
            "source": f"fallback: {e}",
        })
        return fallback


def ensure_review_bilingual(result):
    review = result.get("review_comments")
    if not isinstance(review, dict) or not review.get("review_text") or review.get("review_text_zh"):
        return False
    payload = {
        "recommendation": review.get("recommendation", ""),
        "manuscript_summary": review.get("manuscript_summary", ""),
        "overall_assessment": review.get("overall_assessment", ""),
        "major_comments": review.get("major_comments", []),
        "minor_comments": review.get("minor_comments", []),
        "figure_comments": review.get("figure_comments", []),
        "confidential_comments": review.get("confidential_comments", ""),
        "review_text": review.get("review_text", ""),
    }
    prompt = (
        "你是国际期刊审稿意见翻译助手。请把 stdin 中 JSON 的英文审稿意见翻译为忠实、专业的简体中文。"
        "只输出 JSON object，字段为：recommendation_zh、manuscript_summary_zh、overall_assessment_zh、"
        "major_comments_zh、minor_comments_zh、figure_comments_zh、confidential_comments_zh、review_text_zh。"
        "要求：保留公式、变量、材料相名、化学式、单位、引用和编号；不要增删审稿意见含义。"
    )
    try:
        data = parse_json_response(codex_text(prompt, json.dumps(payload, ensure_ascii=False), timeout=360))
        if not isinstance(data, dict):
            raise ValueError("模型输出不是 JSON object")
        for key, value in data.items():
            if key.endswith("_zh"):
                review[key] = normalize_model_translation(value) if isinstance(value, str) else value
    except Exception:
        review["recommendation_zh"] = {
            "Accept": "接受",
            "Minor revision": "小修",
            "Major revision": "大修",
            "Reject": "拒稿",
        }.get(review.get("recommendation", ""), "")
        review["manuscript_summary_zh"] = translate_zh(review.get("manuscript_summary", ""))
        review["overall_assessment_zh"] = translate_zh(review.get("overall_assessment", ""))
        review["major_comments_zh"] = [translate_zh(x) for x in review.get("major_comments", [])]
        review["minor_comments_zh"] = [translate_zh(x) for x in review.get("minor_comments", [])]
        review["figure_comments_zh"] = [translate_zh(x) for x in review.get("figure_comments", [])]
        review["confidential_comments_zh"] = translate_zh(review.get("confidential_comments", ""))
        review["review_text_zh"] = translate_zh(review.get("review_text", ""))
    result["review_comments"] = review
    return True


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def convert_to_pdf(src, job_dir):
    suffix = src.suffix.lower()
    if suffix == ".pdf":
        pdf = job_dir / "document.pdf"
        shutil.copy2(src, pdf)
        return pdf
    if suffix in {".doc", ".docx"}:
        out_dir = job_dir / "converted"
        out_dir.mkdir(exist_ok=True)
        run([
            "soffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(src),
        ], timeout=180)
        pdfs = sorted(out_dir.glob("*.pdf"))
        if not pdfs:
            raise RuntimeError("Word 文档转换 PDF 失败：没有生成 PDF。")
        pdf = job_dir / "document.pdf"
        shutil.copy2(pdfs[0], pdf)
        return pdf
    raise RuntimeError("目前只支持 PDF、DOC、DOCX 文件。")


def pdf_page_count(pdf):
    info = run(["pdfinfo", str(pdf)])
    m = re.search(r"^Pages:\s+(\d+)", info, re.M)
    if not m:
        raise RuntimeError("无法读取 PDF 页数。")
    return int(m.group(1))


def extract_page_texts(pdf, job_dir):
    txt = job_dir / "layout.txt"
    run(["pdftotext", "-layout", str(pdf), str(txt)])
    return txt.read_text(encoding="utf-8", errors="ignore").split("\f")


def normalize_line(line):
    line = line.strip()
    line = re.sub(r"^\d{1,3}\s+", "", line)
    line = re.sub(r"\s+\d{1,3}\s+", " ", line)
    line = line.translate(str.maketrans({"\uf061": "α", "\uf067": "γ", "\uf06d": "μ", "\uf02d": "−"}))
    return re.sub(r"\s+", " ", line)


def parse_caption_from_page(page_text):
    lines = [normalize_line(x) for x in page_text.splitlines()]
    lines = [x for x in lines if x]
    captions = []
    i = 0
    start_re = re.compile(r"^(Figure|Fig\.?)\s+([S]?\d+[A-Za-z]?)\s*[:.]\s*(.*)", re.I)
    while i < len(lines):
        m = start_re.match(lines[i])
        if not m:
            i += 1
            continue
        fig_no = m.group(2)
        parts = [lines[i]]
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if start_re.match(nxt) or re.match(r"^(Table|References|\d+(\.\d+)*\.?\s+[A-Z])", nxt):
                break
            if len(" ".join(parts)) > 1400:
                break
            # Captions usually continue as sentence fragments; stop at obvious body paragraph starts.
            if len(parts) > 0 and re.match(r"^(In|The|This|For|To|A|An)\s", nxt) and len(nxt) > 80:
                break
            parts.append(nxt)
            if nxt.endswith(".") and len(" ".join(parts)) > 120:
                # Keep one complete caption paragraph, not the following body.
                break
            i += 1
        caption = clean_caption(" ".join(parts))
        captions.append({"figure_no": fig_no, "caption": caption})
    return captions


def continuation_lines(page_text):
    lines = [normalize_line(x) for x in page_text.splitlines()]
    lines = [x for x in lines if x]
    out = []
    for line in lines[:12]:
        if re.match(r"^(Figure|Fig\.?|Table)\s+", line, re.I):
            break
        if re.match(r"^(\d+(\.\d+)*\.?\s+[A-Z]|[A-Z][a-z]+[- ]size|In\s+|The\s+|This\s+study|Progressive\s+|Bulk\s+)", line):
            break
        if len(line) < 4:
            continue
        out.append(line)
        if line.endswith(".") and len(" ".join(out)) > 80:
            break
    return out


def clean_caption(caption):
    caption = re.sub(r"(?:\s+\b(?:[1-9]|[1-5]\d|6[0-5])\b){2,}", " ", caption)
    caption = re.sub(r"\s+\b([1-9]|[1-5]\d|6[0-5])\b\s+(?=(and|of|strain|shown|Patterns|microstructures|distributions|for|were|𝐼|𝐸|[A-Z]))", " ", caption)
    caption = re.sub(r"\s+\b([1-9]|[1-5]\d|6[0-5])\b\s*$", "", caption)
    return re.sub(r"\s+", " ", caption).strip()


def paragraph_candidates(page_text):
    clean = []
    buf = []
    for raw in page_text.splitlines():
        line = normalize_line(raw)
        if not line:
            if buf:
                clean.append(" ".join(buf))
                buf = []
            continue
        if re.fullmatch(r"\(?[a-z]\)?", line):
            continue
        if re.match(r"^(Figure|Fig\.?|Table)\s+", line, re.I):
            continue
        if len(line) < 12:
            continue
        buf.append(line)
    if buf:
        clean.append(" ".join(buf))
    return [re.sub(r"-\s+", "", p) for p in clean if len(p) > 80]


def relevant_paragraph(fig_no, page_text, prev_text="", next_text=""):
    # Prefer body paragraphs on the same page before the caption; fall back to neighboring pages.
    caption_pos = re.search(rf"(Figure|Fig\.?)\s+{re.escape(fig_no)}\s*[:.]", page_text, re.I)
    scope = page_text[: caption_pos.start()] if caption_pos else page_text
    paras = paragraph_candidates(scope)
    if paras:
        return paras[-1]
    for candidate_text in (prev_text, next_text):
        paras = paragraph_candidates(candidate_text)
        if paras:
            return paras[-1] if candidate_text == prev_text else paras[0]
    return ""


def caption_y_from_bbox(pdf, page_num, caption):
    bbox_file = pdf.parent / f"bbox-{page_num}.html"
    run(["pdftotext", "-bbox", "-f", str(page_num), "-l", str(page_num), str(pdf), str(bbox_file)])
    data = bbox_file.read_text(encoding="utf-8", errors="ignore")
    words = []
    for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', data):
        token = html.unescape(re.sub(r"<.*?>", "", m.group(5)))
        words.append((token, float(m.group(2))))
    cap_tokens = re.findall(r"[A-Za-z0-9.]+", caption[:80])
    if not cap_tokens:
        return None
    for i in range(len(words)):
        hay = " ".join(w[0] for w in words[i : i + min(5, len(cap_tokens))]).lower()
        if cap_tokens[0].lower().rstrip(".") in hay and ("figure" in hay or "fig" in hay):
            return words[i][1]
    return None


def panel_y_from_bbox(pdf, page_num):
    bbox_file = pdf.parent / f"bbox-{page_num}.html"
    if not bbox_file.exists():
        run(["pdftotext", "-bbox", "-f", str(page_num), "-l", str(page_num), str(pdf), str(bbox_file)])
    data = bbox_file.read_text(encoding="utf-8", errors="ignore")
    ys = []
    for m in re.finditer(r'<word xMin="[\d.]+" yMin="([\d.]+)" xMax="[\d.]+" yMax="[\d.]+">(.*?)</word>', data):
        token = html.unescape(re.sub(r"<.*?>", "", m.group(2))).strip()
        if re.fullmatch(r"\([a-z]\)", token):
            ys.append(float(m.group(1)))
    return min(ys) if ys else None


def render_page(pdf, page_num, job_dir, dpi=180):
    pages_dir = job_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    prefix = pages_dir / f"page-{page_num}"
    out = pages_dir / f"page-{page_num}.png"
    if not out.exists():
        run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page_num), "-l", str(page_num), str(pdf), str(prefix)])
        generated = pages_dir / f"page-{page_num}-{page_num}.png"
        if generated.exists():
            generated.rename(out)
        else:
            candidates = sorted(pages_dir.glob(f"page-{page_num}-*.png"))
            if candidates:
                candidates[0].rename(out)
    return out


def crop_figure(page_img, y_pdf, job_dir, figure_no):
    figures_dir = job_dir / "figures"
    figures_dir.mkdir(exist_ok=True)
    out = figures_dir / f"figure-{re.sub(r'[^A-Za-z0-9_-]+', '_', figure_no)}.png"
    img = Image.open(page_img)
    w, h = img.size
    if y_pdf is None:
        crop = img.crop((0, 0, w, h))
    else:
        # Letter pages in PDF are 792 pt high; render scale is pixel/point.
        scale = h / 792.0
        y = max(80, min(h - 120, int(y_pdf * scale) - 18))
        crop = img.crop((0, 0, w, y))
    crop.save(out)
    return out


def sparse_figure_page(page_text):
    text = "\n".join(normalize_line(x) for x in page_text.splitlines())
    if re.search(r"^(Figure|Fig\.?)\s+[S]?\d+", text, re.I | re.M):
        return False
    paras = paragraph_candidates(text)
    if paras:
        return False
    tokens = re.findall(r"[A-Za-z]{3,}", text)
    return len(tokens) < 35


def crop_page_content(page_img, y_pdf=None, current_caption_page=False, top_pdf=None):
    img = Image.open(page_img)
    w, h = img.size
    left = int(w * 0.075)
    right = int(w * 0.965)
    top = int(h * 0.035)
    bottom = int(h * 0.965)
    if top_pdf is not None:
        scale = h / 792.0
        top = max(top, min(bottom - 160, int(top_pdf * scale)))
    if current_caption_page and y_pdf is not None:
        scale = h / 792.0
        bottom = max(top + 160, min(bottom, int(y_pdf * scale) - 10))
    return img.crop((left, top, right, bottom))


def compose_figure_image(pdf, page_num, page_texts, y_pdf, job_dir, figure_no):
    figures_dir = job_dir / "figures"
    figures_dir.mkdir(exist_ok=True)
    out = figures_dir / f"figure-{re.sub(r'[^A-Za-z0-9_-]+', '_', figure_no)}.png"

    start = page_num
    top_hints = {}
    for candidate in range(page_num - 1, max(0, page_num - 4), -1):
        panel_y = None
        try:
            panel_y = panel_y_from_bbox(pdf, candidate)
        except Exception:
            panel_y = None
        if sparse_figure_page(page_texts[candidate - 1]) or panel_y is not None:
            start = candidate
            if panel_y is not None:
                top_hints[candidate] = max(0, panel_y - 320)
        else:
            break

    crops = []
    for p in range(start, page_num + 1):
        page_img = render_page(pdf, p, job_dir)
        crops.append(crop_page_content(page_img, y_pdf if p == page_num else None, p == page_num, top_hints.get(p)))

    max_w = max(img.width for img in crops)
    total_h = sum(img.height for img in crops) + 18 * (len(crops) - 1)
    canvas = Image.new("RGB", (max_w, total_h), "white")
    y = 0
    for img in crops:
        canvas.paste(img.convert("RGB"), ((max_w - img.width) // 2, y))
        y += img.height + 18
    canvas.save(out)
    return out


def make_thumbnail(image_path, job_dir, figure_no):
    thumbs_dir = job_dir / "thumbs"
    thumbs_dir.mkdir(exist_ok=True)
    out = thumbs_dir / f"figure-{re.sub(r'[^A-Za-z0-9_-]+', '_', figure_no)}.jpg"
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((360, 260), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (360, 260), "white")
    canvas.paste(img, ((360 - img.width) // 2, (260 - img.height) // 2))
    canvas.save(out, quality=82, optimize=True)
    return out


def result_summary(result_path):
    data = json.loads(result_path.read_text(encoding="utf-8"))
    stat = result_path.stat()
    return {
        "job_id": data.get("job_id", result_path.parent.name),
        "file_name": data.get("file_name", "未命名稿件"),
        "page_count": data.get("page_count", 0),
        "figure_count": data.get("figure_count", 0),
        "paragraph_count": len(data.get("full_translation", []) or []),
        "recommendation": (data.get("review_comments") or {}).get("recommendation", ""),
        "updated_at": stat.st_mtime,
    }


def history_results():
    results = sorted(JOBS.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for result_path in results:
        try:
            out.append(result_summary(result_path))
        except Exception:
            continue
    return out


def load_result(job_id):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id or ""):
        raise RuntimeError("无效的历史结果 ID。")
    result_path = JOBS / job_id / "result.json"
    if not result_path.exists():
        raise RuntimeError("没有找到该历史解析结果。")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if ensure_review_bilingual(result):
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def analyze(src, progress_id=None):
    ensure_dirs()
    update_progress(progress_id, 2, "初始化", f"开始解析 {src.name}")
    job_id = f"{int(time.time())}-{file_hash(src)}"
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    update_progress(progress_id, 6, "文档转换", "正在准备 PDF")
    pdf = convert_to_pdf(src, job_dir)
    update_progress(progress_id, 10, "读取页数", "正在读取 PDF 元数据")
    pages = pdf_page_count(pdf)
    update_progress(progress_id, 14, "抽取文本", f"正在抽取 {pages} 页正文")
    page_texts = extract_page_texts(pdf, job_dir)

    update_progress(progress_id, 18, "附图识别", "正在定位图注并渲染页面")
    figures = []
    for idx in range(1, pages + 1):
        update_progress(progress_id, 18 + int((idx / max(1, pages)) * 26), "附图识别", f"正在处理第 {idx}/{pages} 页")
        text = page_texts[idx - 1] if idx - 1 < len(page_texts) else ""
        for cap in parse_caption_from_page(text):
            fig_no = cap["figure_no"]
            caption = cap["caption"]
            next_text = page_texts[idx] if idx < len(page_texts) else ""
            if next_text and (not caption.endswith(".") or len(caption) < 260):
                extra = continuation_lines(next_text)
                if extra:
                    caption = clean_caption(caption + " " + " ".join(extra))
            page_img = render_page(pdf, idx, job_dir)
            try:
                y = caption_y_from_bbox(pdf, idx, caption)
            except Exception:
                y = None
            fig_img = compose_figure_image(pdf, idx, page_texts, y, job_dir, fig_no)
            thumb_img = make_thumbnail(fig_img, job_dir, fig_no)
            prev_text = page_texts[idx - 2] if idx - 2 >= 0 else ""
            para = relevant_paragraph(fig_no, text, prev_text, next_text)
            figures.append({
                "id": f"fig-{len(figures)+1}",
                "figure_no": fig_no,
                "page": idx,
                "image_url": f"/work/jobs/{job_id}/{fig_img.relative_to(job_dir)}",
                "thumb_url": f"/work/jobs/{job_id}/{thumb_img.relative_to(job_dir)}",
                "page_url": f"/work/jobs/{job_id}/{page_img.relative_to(job_dir)}",
                "caption": caption,
                "paragraph": para,
            })
    update_progress(progress_id, 46, "附图翻译", f"识别到 {len(figures)} 张附图，正在翻译图注和相关段落")
    figures = translate_figure_texts(figures, progress_id=progress_id)
    update_progress(progress_id, 56, "全文翻译", "正在生成全文原文-中文对照")
    full_translation = translate_article_paragraphs(page_texts, progress_id)
    update_progress(progress_id, 72, "摘要与创新点", "正在生成论文摘要和创新点")
    article_text = clean_article_text(page_texts)
    synthesis = build_summary_and_innovations(article_text, progress_id=progress_id)
    update_progress(progress_id, 82, "文献核查", "正在检索 Zotero 本地库并判断创新性")
    literature = assess_literature_overlap(synthesis.get("innovations", []), progress_id=progress_id)
    update_progress(progress_id, 92, "审稿意见", "正在生成中英文审稿意见")
    review = build_review_comments(article_text, synthesis, literature, figures, progress_id=progress_id)

    result = {
        "job_id": job_id,
        "file_name": src.name,
        "page_count": pages,
        "figure_count": len(figures),
        "figures": figures,
        "full_translation": full_translation.get("paragraphs", []),
        "full_translation_source": full_translation.get("source", ""),
        "summary": synthesis.get("summary", {}),
        "innovations": synthesis.get("innovations", []),
        "literature_checks": literature.get("checks", []),
        "review_comments": review,
        "zotero_index_count": literature.get("index_count", 0),
        "literature_source": literature.get("source", ""),
        "model": CODEX_MODEL if USE_MODEL else "offline",
        "synthesis_source": synthesis.get("source", ""),
        "review_source": review.get("source", ""),
        "debug_log": read_progress(progress_id).get("logs", []) if progress_id else [],
    }
    (job_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    update_progress(progress_id, 100, "完成", "解析完成", status="done")
    return result


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path):
        parsed = urlparse(path).path
        if parsed.startswith("/work/"):
            return str(ROOT / parsed.lstrip("/"))
        if parsed == "/":
            return str(STATIC / "index.html")
        return str(STATIC / parsed.lstrip("/"))

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/analyze":
            self.send_json({"error": "Not found"}, 404)
            return
        progress_id = ""
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            })
            progress_id = safe_id(form.getfirst("progress_id", ""))
            update_progress(progress_id, 1, "上传文件", "正在保存上传文件")
            item = form["file"]
            filename = Path(item.filename or "upload.pdf").name
            dest = UPLOADS / filename
            with open(dest, "wb") as f:
                shutil.copyfileobj(item.file, f)
            acquired = ANALYSIS_LOCK.acquire(blocking=False)
            if not acquired:
                update_progress(progress_id, 2, "等待中", "已有解析任务正在运行，正在排队等待")
                ANALYSIS_LOCK.acquire()
                update_progress(progress_id, 2, "等待结束", "已获得解析执行权")
            try:
                result = analyze(dest, progress_id=progress_id)
                self.send_json(result)
            finally:
                ANALYSIS_LOCK.release()
        except Exception as e:
            update_progress(progress_id, 100, "解析失败", str(e), status="error")
            self.send_json({"error": str(e)}, 500)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/latest":
            try:
                results = sorted(JOBS.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not results:
                    self.send_json({"error": "还没有解析结果。"}, 404)
                    return
                result = json.loads(results[0].read_text(encoding="utf-8"))
                if ensure_review_bilingual(result):
                    results[0].write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                self.send_json(result)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/history":
            try:
                self.send_json({"items": history_results()})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/result":
            try:
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                self.send_json(load_result(job_id))
            except Exception as e:
                self.send_json({"error": str(e)}, 404)
            return
        if parsed.path == "/api/progress":
            try:
                progress_id = parse_qs(parsed.query).get("id", [""])[0]
                self.send_json(read_progress(progress_id))
            except Exception as e:
                self.send_json({"error": str(e)}, 404)
            return
        return super().do_GET()


def main():
    ensure_dirs()
    port = int(os.environ.get("PORT", "8765"))
    os.chdir(STATIC)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Reviewer tool running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
