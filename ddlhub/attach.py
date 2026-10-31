"""把群里的作业文件（.doc/.docx/.pdf/.pptx/.xlsx/.txt…）转成纯文本，
交给 LLM 抽取截止时间。

优先本地解析，都不需要额外安装：
  docx        zip + XML（stdlib）
  doc         Word COM（本机已装 Office）
  pdf         PyMuPDF
  pptx        python-pptx
  xlsx        openpyxl
  txt/md/csv  直接读
解析不了时才退回 claude 命令行读取文件（远端解析，走已有订阅，不需要额外 key）。
"""

from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

MAX_CHARS = 8000
MAX_BYTES = 8 * 1024 * 1024

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".html", ".htm"}


class UnsupportedFile(RuntimeError):
    pass


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text.strip()


def _xml_to_text(xml: str) -> str:
    xml = re.sub(r"<(w:p|a:p|w:br|/w:p|/a:p)[^>]*>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        parts = [n for n in z.namelist() if re.fullmatch(r"word/(document|header\d*|footer\d*)\.xml", n)]
        if not parts:
            raise UnsupportedFile("docx 里没找到 document.xml")
        return "\n".join(_xml_to_text(z.read(p).decode("utf-8", "ignore")) for p in parts)


def _doc_text(path: Path) -> str:
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        raise UnsupportedFile(f"没有 win32com，无法解析 .doc：{e}") from e

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(path), ReadOnly=True, AddToRecentFiles=False, Visible=False)
        try:
            return doc.Content.Text
        finally:
            doc.Close(False)
    except Exception as e:
        raise UnsupportedFile(f"Word 打不开 .doc：{e}") from e
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _pdf_text(path: Path) -> str:
    try:
        import pymupdf  # PyMuPDF ≥1.24
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError as e:
            raise UnsupportedFile(f"没有 PyMuPDF，无法解析 PDF：{e}") from e
    with pymupdf.open(str(path)) as doc:
        return "\n".join(page.get_text() for page in doc)


def _pptx_text(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as e:
        raise UnsupportedFile(f"没有 python-pptx：{e}") from e
    prs = Presentation(str(path))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"— 第 {i} 页 —")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text)
    return "\n".join(out)


def _xlsx_text(path: Path) -> str:
    try:
        import openpyxl
    except ImportError as e:
        raise UnsupportedFile(f"没有 openpyxl：{e}") from e
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        out = []
        for ws in wb.worksheets:
            out.append(f"— {ws.title} —")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out)
    finally:
        wb.close()


def _claude_read(path: Path, timeout: int = 180) -> str:
    if not shutil.which("claude"):
        raise UnsupportedFile("本机没有 claude 命令，无法远端解析")
    prompt = (
        f"读取本地文件：{path}\n"
        "把其中的文字内容原样输出成纯文本。不要总结、不要解释、不要加任何前后缀。"
        "如果文件里没有可读文字，只输出一行：EMPTY"
    )
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "text"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise UnsupportedFile(f"远端解析失败：{(proc.stderr or '')[:200]}")
    text = proc.stdout.strip()
    if text == "EMPTY":
        raise UnsupportedFile("远端解析没有取到文字")
    return text


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

IMAGE_PROMPT = (
    "逐行原样输出图里的所有文字，包括标题、标签、数字和日期。"
    "不要总结、不要解释、不要漏行。没有文字就只输出 EMPTY"
)


def _vision_provider():
    """图片解析用的模型。没配 vision_model 且主模型看着不是多模态，就返回 None。"""
    from . import llm, pipeline  # 延迟导入：attach 被 sources 引用，避免导入环

    cfg = pipeline.load_config()
    llm_cfg = dict(cfg.get("llm") or {})
    vision = str(llm_cfg.get("vision_model") or "").strip()
    if vision:
        llm_cfg["model"] = vision
    elif "vl" not in str(llm_cfg.get("model") or "").lower():
        return None
    return llm.build_provider(llm_cfg)


def _llm_read(path: Path, timeout: int = 180) -> str:
    """用 llm 模块里配置的多模态模型读图片。"""
    provider = _vision_provider()
    if provider is None:
        raise UnsupportedFile("没配 vision_model，也没有多模态主模型")
    if not hasattr(provider, "complete_image"):
        raise UnsupportedFile(f"{provider.describe()} 不支持读图片")
    text = (provider.complete_image(IMAGE_PROMPT, path, timeout) or "").strip()
    if not text or text.upper() == "EMPTY":
        raise UnsupportedFile("模型没从图里读出文字")
    return text


def _remote_read(path: Path, timeout: int = 180) -> tuple[str, str]:
    """远端解析：图片先试 llm 模块配置的模型，不行再退回本机 claude 命令。

    返回 (engine, text)；engine 会写进消息头，方便事后判断是哪条路解析出来的。
    """
    errors = []
    if path.suffix.lower() in IMAGE_EXT:
        try:
            return "llm-remote", _llm_read(path)
        except Exception as e:
            errors.append(f"llm: {e}")
    try:
        return "claude-remote", _claude_read(path, timeout)
    except Exception as e:
        errors.append(f"claude: {e}")
    raise UnsupportedFile("远端解析都失败了（" + "；".join(errors)[:250] + "）")


def _weak(text: str) -> bool:
    """解析结果是不是不可用：太短，或者有效字符占比过低（二进制乱码、扫描件空文本）。"""
    if len(text) < 20:
        return True
    good = sum(1 for c in text if c.isalnum() or "一" <= c <= "鿿")
    return good / len(text) < 0.35


def _dispatch(p: Path, suffix: str) -> tuple[str, str]:
    """只做本地解析，解析不了抛 UnsupportedFile。"""
    if suffix in TEXT_EXT:
        return "text", p.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".docx":
        return "docx", _docx_text(p)
    if suffix == ".doc":
        return "word-com", _doc_text(p)
    if suffix == ".pdf":
        return "pymupdf", _pdf_text(p)
    if suffix == ".pptx":
        return "python-pptx", _pptx_text(p)
    if suffix == ".xlsx":
        return "openpyxl", _xlsx_text(p)
    raise UnsupportedFile(f"本地解析不支持 {suffix or '(无扩展名)'}")


def extract_text(path: Path | str, allow_remote: bool = True) -> dict:
    """返回 {"text", "engine", "chars"}；解析失败抛 UnsupportedFile。

    作业文件格式很杂，所以分三层：
      1. 本地解析（快、免费）
      2. 本地结果不可用（空文本、二进制乱码、扫描件）→ 改走 claude 读文件
      3. 本地直接不认识这个格式 → 直接走 claude
    """
    p = Path(path)
    if not p.exists():
        raise UnsupportedFile(f"文件不存在：{p}")
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise UnsupportedFile(f"文件太大（{size / 1024 / 1024:.1f} MB），跳过")
    if size == 0:
        raise UnsupportedFile("空文件")

    suffix = p.suffix.lower()
    engine, text = "", ""
    local_error = None

    try:
        engine, text = _dispatch(p, suffix)
    except UnsupportedFile as e:
        local_error = e
        if not allow_remote:
            raise

    text = _clean(text)
    needs_remote = engine == "" or _weak(text)
    if needs_remote and allow_remote:
        try:
            alt_engine, alt_raw = _remote_read(p)
            alt = _clean(alt_raw)
            if engine == "" or len(alt) > len(text):
                engine, text = alt_engine, alt
        except Exception as e:
            if engine == "":
                raise UnsupportedFile(
                    f"本地解析失败（{local_error}），远端也失败（{e}）"
                ) from e

    if engine == "":
        raise UnsupportedFile(f"解析不了的格式 {suffix or '(无扩展名)'}")
    if not text.strip():
        raise UnsupportedFile("解析出来了，但没有文字内容（可能是扫描件或纯图）")
    return {"text": text[:MAX_CHARS], "engine": engine, "chars": len(text)}


def to_message(path: Path | str, source: str, chat: str, sender: str, sent_at: str, filename: str | None = None) -> dict | None:
    """把一个文件变成可直接入库的消息行。"""
    p = Path(path)
    name = filename or p.name
    try:
        got = extract_text(p)
    except UnsupportedFile as e:
        print(f"[attach] 跳过 {name}：{e}")
        return None
    header = f"【群文件】{name}（解析方式：{got['engine']}）"
    digest = hashlib.sha1(got["text"].encode("utf-8")).hexdigest()[:16]
    return {
        "source": source,
        "chat": chat,
        "sender": sender or "文件",
        "sent_at": sent_at,
        "content": f"{header}\n{got['text']}",
        "msg_uid": f"file:{source}:{chat}:{name}:{digest}",
    }


def save_bytes(data: bytes, name: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120] or "file"
    p = dest_dir / safe
    i = 1
    while p.exists():
        p = dest_dir / f"{Path(safe).stem}({i}){Path(safe).suffix}"
        i += 1
    p.write_bytes(data)
    return p
