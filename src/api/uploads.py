"""Вложения для search: шлюз кладёт файл в общий каталог, воркер отдаёт его в AI Mode.

Путь файла: <AIFARM_UPLOAD_DIR>/<арендатор>/<file_id>/<имя>. Имя сохраняется:
Google показывает его в карточке вложения, и по нему же воркер ждёт, что файл
принят. Каталог общий у шлюза и воркера и смонтирован у обоих по одному пути,
поэтому в задачу уходит готовый абсолютный путь.

Загрузка и отправка — разные вызовы, поэтому файл живёт UPLOAD_TTL_SECONDS, а
не до конца запроса; держать чужие файлы дольше одного хода беседы незачем.
"""

import os
import re
import shutil
import time
import uuid
from pathlib import Path

from src.contract import Attachment

UPLOAD_DIR = Path(os.getenv("AIFARM_UPLOAD_DIR", "./uploads")).resolve()
UPLOAD_MAX_BYTES = int(float(os.getenv("AIFARM_UPLOAD_MAX_MB", "20")) * 1024 * 1024)
UPLOAD_TTL_SECONDS = int(os.getenv("AIFARM_UPLOAD_TTL_S", "3600"))
MAX_ATTACHMENTS = int(os.getenv("AIFARM_MAX_ATTACHMENTS", "10"))
CHUNK_BYTES = 1 << 20

# Ровно то, что принимает input картинок у поля AI Mode (accept снят с живой
# страницы). Всё остальное уходит в input файлов.
IMAGE_EXTENSIONS = frozenset({".avif", ".bmp", ".jpg", ".jpeg", ".png", ".webp", ".heif", ".heic"})
_FILE_ID = re.compile(r"[0-9a-f]{32}")


class UploadError(Exception):
    """Загрузку или file_id отклонили; текст уходит клиенту."""


class UploadTooLarge(UploadError):
    """Файл больше UPLOAD_MAX_BYTES."""


def safe_name(name: str) -> str:
    """Имя без пути и управляющих символов — оно станет именем файла на диске."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip().strip(".")
    return base[:120] or "file"


def kind_of(name: str) -> str:
    """В какой input AI Mode отдавать файл: картинок или файлов."""
    return "image" if Path(name).suffix.lower() in IMAGE_EXTENSIONS else "file"


def _expire(now: float) -> None:
    """Удаляет загрузки старше TTL. Дёшево, вызывается на каждой загрузке."""
    if not UPLOAD_DIR.is_dir():
        return
    for user_dir in UPLOAD_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        for upload in user_dir.iterdir():
            try:
                if now - upload.stat().st_mtime > UPLOAD_TTL_SECONDS:
                    shutil.rmtree(upload, ignore_errors=True)
            except OSError:
                continue


async def save(user: str, name: str, reader) -> dict:
    """Сохраняет поток reader (async read(n)) и возвращает описание загрузки."""
    _expire(time.time())
    file_id = uuid.uuid4().hex
    name = safe_name(name)
    folder = UPLOAD_DIR / user / file_id
    folder.mkdir(parents=True)
    size = 0
    try:
        with open(folder / name, "wb") as out:
            while chunk := await reader.read(CHUNK_BYTES):
                size += len(chunk)
                if size > UPLOAD_MAX_BYTES:
                    raise UploadTooLarge(f"файл больше {UPLOAD_MAX_BYTES // (1024 * 1024)} МБ")
                out.write(chunk)
        if size == 0:
            raise UploadError("пустой файл")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"file_id": file_id, "name": name, "size": size, "kind": kind_of(name)}


def resolve(user: str, file_ids: list[str] | None) -> list[Attachment]:
    """file_id арендатора -> вложения для задачи воркера. Чужие и протухшие не проходят."""
    ids = list(dict.fromkeys(file_ids or []))
    if len(ids) > MAX_ATTACHMENTS:
        raise UploadError(f"не больше {MAX_ATTACHMENTS} вложений в одном сообщении")
    now = time.time()
    attachments = []
    for file_id in ids:
        folder = UPLOAD_DIR / user / file_id
        files = (
            [path for path in folder.iterdir() if path.is_file()]
            if _FILE_ID.fullmatch(file_id) and folder.is_dir()
            else []
        )
        if len(files) != 1 or now - folder.stat().st_mtime > UPLOAD_TTL_SECONDS:
            raise UploadError(f"вложение {file_id} не найдено или устарело — загрузите файл заново")
        attachments.append(
            Attachment(path=str(files[0]), name=files[0].name, kind=kind_of(files[0].name))
        )
    return attachments
