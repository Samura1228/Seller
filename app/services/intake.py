"""Zip intake: one top-level folder in the archive = one item.

Accepted layouts:
    sony headphones/1.jpg            -> item "sony headphones"
    my items/sony headphones/1.jpg   -> single wrapping folder is stripped
    sony headphones/box/2.jpg        -> nested folders stay in the same item
Loose photos in the archive root are skipped (we can't tell which item they belong to).
"""

import asyncio
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from app.config import Settings
from app.db.enums import Actor, ItemStatus
from app.db.models import IntakeBatch, Item, ItemPhoto
from app.db.session import session_scope
from app.services import items as item_service
from app.services.photos import IMAGE_EXTS, VIDEO_EXTS, process_photo, sha256_file

log = logging.getLogger(__name__)

MAX_TOTAL_UNCOMPRESSED = 4 * 1024**3   # 4 GB, zip-bomb guard
MAX_FILE_UNCOMPRESSED = 150 * 1024**2  # 150 MB per photo
JUNK_PARTS = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}


@dataclass
class FolderGroup:
    hint: str
    members: list[zipfile.ZipInfo] = field(default_factory=list)


@dataclass
class ScanResult:
    groups: list[FolderGroup]
    loose_photos: int = 0
    videos: int = 0
    other_files: int = 0


def decode_name(info: zipfile.ZipInfo) -> str:
    """Zips made on Windows store Cyrillic names in cp866 without the UTF-8 flag."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def scan_zip(zf: zipfile.ZipFile) -> ScanResult:
    total = 0
    entries: list[tuple[tuple[str, ...], zipfile.ZipInfo]] = []
    result = ScanResult(groups=[])

    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = tuple(p for p in PurePosixPath(decode_name(info).replace("\\", "/")).parts if p not in ("", ".", ".."))
        if not parts or any(p in JUNK_PARTS or p.startswith("._") for p in parts):
            continue
        ext = PurePosixPath(parts[-1]).suffix.lower()
        if ext in VIDEO_EXTS:
            result.videos += 1
            continue
        if ext not in IMAGE_EXTS:
            result.other_files += 1
            continue
        if info.file_size > MAX_FILE_UNCOMPRESSED:
            result.other_files += 1
            continue
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise ValueError("Archive is too large when unpacked (> 4 GB)")
        entries.append((parts, info))

    # Strip wrapping folders: while every photo shares the first folder AND would still be inside an item folder.
    while entries and len({p[0] for p, _ in entries}) == 1 and all(len(p) >= 3 for p, _ in entries):
        entries = [(p[1:], i) for p, i in entries]

    groups: dict[str, FolderGroup] = {}
    for parts, info in sorted(entries, key=lambda e: e[0]):
        if len(parts) < 2:
            result.loose_photos += 1
            continue
        groups.setdefault(parts[0], FolderGroup(hint=parts[0])).members.append(info)
    result.groups = list(groups.values())
    return result


async def import_zip(batch_id: int, zip_path: Path, settings: Settings) -> tuple[list[int], dict]:
    """Unpacks the archive and creates Item + ItemPhoto rows. Returns (new item ids, report)."""
    report: dict = {"duplicates": [], "empty": [], "bad_photos": 0}
    work_dir = settings.tmp_dir / f"batch_{batch_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    item_ids: list[int] = []

    try:
        with zipfile.ZipFile(zip_path) as zf:
            scan = await asyncio.to_thread(scan_zip, zf)
            report.update(loose_photos=scan.loose_photos, videos=scan.videos, other_files=scan.other_files)

            for g_idx, group in enumerate(scan.groups):
                group_dir = work_dir / str(g_idx)
                group_dir.mkdir(exist_ok=True)
                files: list[tuple[Path, str]] = []
                for n, info in enumerate(group.members):
                    dest = group_dir / f"{n}{PurePosixPath(info.filename).suffix.lower()}"
                    # Never use archive paths for output -> no zip-slip.
                    await asyncio.to_thread(_extract_member, zf, info, dest)
                    files.append((dest, PurePosixPath(decode_name(info)).name))

                item_id = await _create_item(batch_id, group.hint, files, settings, report)
                if item_id:
                    item_ids.append(item_id)
                shutil.rmtree(group_dir, ignore_errors=True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return item_ids, report


def _extract_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    with zf.open(info) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)


async def _create_item(batch_id: int, hint: str, files: list[tuple[Path, str]], settings: Settings, report: dict) -> int | None:
    hashes = [await asyncio.to_thread(sha256_file, p) for p, _ in files]

    async with session_scope() as s:
        # Withdrawn items don't block a re-upload (e.g. after lost photos or "не продаю" by mistake).
        dup = await s.scalar(
            select(ItemPhoto.item_id).join(Item, Item.id == ItemPhoto.item_id)
            .where(ItemPhoto.sha256.in_(hashes), Item.status != ItemStatus.WITHDRAWN).limit(1)
        )
        if dup:
            report["duplicates"].append({"folder": hint, "existing_item_id": dup})
            return None

        item = Item(batch_id=batch_id, folder_hint=hint[:255], status=ItemStatus.NEW)
        s.add(item)
        await s.flush()

        order = 0
        for (path, original_name), digest in zip(files, hashes, strict=True):
            try:
                processed = await asyncio.to_thread(
                    process_photo, path, settings.data_dir, item.id, order,
                    settings.photo_max_side, settings.photo_thumb_side,
                )
            except Exception:  # corrupt / unsupported image
                log.warning("Bad photo %s in folder %s", original_name, hint, exc_info=True)
                report["bad_photos"] += 1
                continue
            s.add(ItemPhoto(
                item_id=item.id, path=processed.rel_path, thumb_path=processed.rel_thumb,
                width=processed.width, height=processed.height, sort_order=order,
                is_cover=order == 0, sha256=digest, original_name=original_name[:255],
            ))
            order += 1

        if order == 0:
            report["empty"].append(hint)
            await s.delete(item)
            return None

        await item_service.log_event(s, item, None, ItemStatus.NEW, Actor.SYSTEM, f"imported {order} photos")
        return item.id


async def set_batch(batch_id: int, **fields) -> None:
    async with session_scope() as s:
        batch = await s.get(IntakeBatch, batch_id)
        if batch:
            for k, v in fields.items():
                setattr(batch, k, v)
