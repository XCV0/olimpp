"""Чтение текста из документов, присланных в чат: txt/md/csv/json, docx, pdf."""

import io
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import Document

log = logging.getLogger(__name__)

MAX_FILE_SIZE = 10 * 1024 * 1024   # Bot API отдаёт файлы до 20 МБ, больше не берём
MAX_CHARS = 20_000                 # дальше обрезаем, чтобы не раздувать запрос к LLM
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".htm", ".rtf"}


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def _docx(data: bytes) -> str:
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def supported(doc: Document) -> bool:
    ext = Path(doc.file_name or "").suffix.lower()
    return ext in TEXT_EXT | {".pdf", ".docx"} or (doc.mime_type or "").startswith("text/")


async def read_document(bot: Bot, doc: Document) -> str | None:
    """Текст документа или None, если формат не поддержан / файл не скачался."""
    if not supported(doc):
        return None
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        return "(файл слишком большой, не прочитан)"
    try:
        buf = await bot.download(doc.file_id)
        data = buf.read()
        ext = Path(doc.file_name or "").suffix.lower()
        text = _pdf(data) if ext == ".pdf" else _docx(data) if ext == ".docx" else _decode(data)
    except Exception:
        log.exception("не смог прочитать файл %s", doc.file_name)
        return None
    text = text.strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…(документ обрезан)"
    return text or "(в файле нет текста — возможно, это скан)"
