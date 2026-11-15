"""Photos and PDFs the user sent in the current conversation.

Each file is saved under data/<slug>.attachments/ and the saved history keeps
a reference to it, so the model keeps seeing it on later messages (and after
a restart) until /new clears the conversation. Each request carries the
newest files, up to MAX_VISION_IMAGES of them and MAX_VISION_PAYLOAD_BYTES in
all; older ones become a note telling the model to ask for them again.
"""

from __future__ import annotations

import base64
import shutil
import uuid
from pathlib import Path

import config

# History part for a saved file; expand() swaps it for the file itself.
REF = "attachment"
_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}


def folder(slug: str) -> Path:
    return config.DATA_DIR / f"{slug}.attachments"


def image_part(raw: bytes, mime_type: str) -> dict:
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}


def pdf_part(raw: bytes, filename: str) -> dict:
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return {
        "type": "file",
        "file": {"filename": filename, "file_data": f"data:application/pdf;base64,{b64}"},
    }


def save(slug: str, raw: bytes, *, label: str, mime_type: str, file_name: str | None = None) -> dict:
    """Store one file and return the history part that refers to it."""
    directory = folder(slug)
    directory.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + _SUFFIXES.get(mime_type, "")
    (directory / name).write_bytes(raw)
    ref = {"type": REF, "file": name, "label": label, "mime_type": mime_type}
    if file_name:
        ref["file_name"] = file_name
    return ref


def clear(slug: str) -> None:
    """Delete every saved file (on /new)."""
    shutil.rmtree(folder(slug), ignore_errors=True)


def expand(slug: str, messages: list[dict], *, max_files: int, max_bytes: int) -> list[dict]:
    """A copy of the messages with each saved-file reference replaced by the file.

    The newest files go first; once max_files or max_bytes is reached, older
    ones become a note. The messages passed in are not changed.
    """
    directory = folder(slug)
    files = used = 0
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        content = out[i].get("content")
        if not isinstance(content, list) or not any(_is_ref(p) for p in content):
            continue
        parts = []
        for part in content:
            if not _is_ref(part):
                parts.append(part)
                continue
            label = part.get("label") or "An attachment"
            try:
                path = directory / Path(part["file"]).name
                if files >= max_files or used + path.stat().st_size > max_bytes:
                    parts.append(_note(f"{label} from this message is no longer attached; "
                                       "ask the user to send it again if you need it."))
                    continue
                raw = path.read_bytes()
            except (OSError, KeyError, TypeError):
                parts.append(_note(f"{label} from this message is no longer available; "
                                   "ask the user to send it again if you need it."))
                continue
            files += 1
            used += len(raw)
            if part.get("mime_type") == "application/pdf":
                parts.append(pdf_part(raw, part.get("file_name") or "document.pdf"))
            else:
                parts.append(image_part(raw, part.get("mime_type") or "image/jpeg"))
        out[i] = {**out[i], "content": parts}
    return out


def _is_ref(part: object) -> bool:
    return isinstance(part, dict) and part.get("type") == REF


def _note(text: str) -> dict:
    return {"type": "text", "text": f"[{text}]"}
