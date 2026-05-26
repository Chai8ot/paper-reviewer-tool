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
import time
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
ZOTERO_STORAGE = Path(os.environ.get("ZOTERO_STORAGE", "/Users/junchai/Zotero/storage"))
ZOTERO_INDEX = WORK / "zotero_index.json"
PYTHON = sys.executable
CODEX_BIN = shutil.which("codex") or "/Applications/Codex.app/Contents/Resources/codex"
CODEX_MODEL = os.environ.get("REVIEWER_TOOL_MODEL", "gpt-5.4")
USE_MODEL = os.environ.get("REVIEWER_TOOL_USE_MODEL", "1") != "0"


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
    for path in (WORK, UPLOADS, JOBS):
        path.mkdir(parents=True, exist_ok=True)


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


def codex_text(prompt, source_text, timeout=300):
    if not USE_MODEL or not CODEX_BIN or not Path(CODEX_BIN).exists():
        raise RuntimeError("Codex 模型翻译未启用或不可用。")
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".txt", delete=False) as out:
        out_path = out.name
    try:
        proc = subprocess.run(
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
            input=source_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
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


def translate_figure_texts(figures):
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
        translated = parse_json_response(codex_text(prompt, json.dumps(items, ensure_ascii=False), timeout=420))
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


def build_summary_and_innovations(article_text):
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
        data = parse_json_response(codex_text(prompt, article_text, timeout=420))
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


def assess_literature_overlap(innovations):
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
        checks = parse_json_response(codex_text(prompt, json.dumps(payload, ensure_ascii=False), timeout=420))
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


def build_review_comments(article_text, synthesis, literature, figures):
    fallback = {
        "recommendation": "Major revision",
        "manuscript_summary": "",
        "overall_assessment": "",
        "major_comments": [],
        "minor_comments": [],
        "figure_comments": [],
        "confidential_comments": "",
        "review_text": "",
        "source": "empty",
    }
    if not article_text.strip():
        fallback["review_text"] = "The manuscript text could not be extracted reliably. Please inspect the PDF/Word file and rerun the analysis before preparing formal review comments."
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
        "\"manuscript_summary\":\"\","
        "\"overall_assessment\":\"\","
        "\"major_comments\":[\"\"],"
        "\"minor_comments\":[\"\"],"
        "\"figure_comments\":[\"\"],"
        "\"confidential_comments\":\"\","
        "\"review_text\":\"\"}. "
        "Standards: comments must match international journal review norms; be professional and evidence-based; "
        "start with a concise manuscript summary; evaluate significance, novelty, methodological soundness, data support, "
        "clarity, reproducibility, and literature positioning; identify whether novelty is weakened by the local literature-check results; "
        "write major comments as numbered, actionable revision requests; write minor comments for clarity, terminology, references, figures, and formatting. "
        "Do not invent details not present in the manuscript. If evidence is insufficient, say what must be provided. "
        "Keep formulas, variables, phase names, chemical symbols, units, and references unchanged. "
        "The review_text field must be a ready-to-submit review with sections: Recommendation, Summary, Overall assessment, Major comments, Minor comments, Figures and presentation, Confidential comments to the editor."
    )
    try:
        data = parse_json_response(codex_text(prompt, json.dumps(payload, ensure_ascii=False), timeout=480))
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
            "manuscript_summary": summary.get("one_sentence", ""),
            "overall_assessment": "The manuscript requires clearer novelty positioning and stronger evidence before publication can be recommended.",
            "major_comments": major,
            "minor_comments": minor,
            "figure_comments": ["Check figure readability, captions, scale bars, panel labels, and consistency with the claims in the text."],
            "confidential_comments": f"Model drafting failed; fallback template used. Error: {e}",
            "review_text": review_text,
            "source": f"fallback: {e}",
        })
        return fallback


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


def analyze(src):
    ensure_dirs()
    job_id = f"{int(time.time())}-{file_hash(src)}"
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf = convert_to_pdf(src, job_dir)
    pages = pdf_page_count(pdf)
    page_texts = extract_page_texts(pdf, job_dir)

    figures = []
    for idx in range(1, pages + 1):
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
    figures = translate_figure_texts(figures)
    article_text = clean_article_text(page_texts)
    synthesis = build_summary_and_innovations(article_text)
    literature = assess_literature_overlap(synthesis.get("innovations", []))
    review = build_review_comments(article_text, synthesis, literature, figures)

    result = {
        "job_id": job_id,
        "file_name": src.name,
        "page_count": pages,
        "figure_count": len(figures),
        "figures": figures,
        "summary": synthesis.get("summary", {}),
        "innovations": synthesis.get("innovations", []),
        "literature_checks": literature.get("checks", []),
        "review_comments": review,
        "zotero_index_count": literature.get("index_count", 0),
        "literature_source": literature.get("source", ""),
        "model": CODEX_MODEL if USE_MODEL else "offline",
        "synthesis_source": synthesis.get("source", ""),
        "review_source": review.get("source", ""),
    }
    (job_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


class Handler(SimpleHTTPRequestHandler):
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
        if self.path != "/api/analyze":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            })
            item = form["file"]
            filename = Path(item.filename or "upload.pdf").name
            dest = UPLOADS / filename
            with open(dest, "wb") as f:
                shutil.copyfileobj(item.file, f)
            result = analyze(dest)
            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_GET(self):
        if self.path == "/api/latest":
            try:
                results = sorted(JOBS.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not results:
                    self.send_json({"error": "还没有解析结果。"}, 404)
                    return
                self.send_json(json.loads(results[0].read_text(encoding="utf-8")))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
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
