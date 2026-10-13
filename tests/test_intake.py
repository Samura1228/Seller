import io
import zipfile
from pathlib import Path

from PIL import Image

from app.services.intake import scan_zip
from app.services.photos import ai_jpeg_bytes, process_photo


def _jpeg(size=(40, 30), exif_gps=False) -> bytes:
    img = Image.new("RGB", size, (200, 10, 10))
    buf = io.BytesIO()
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (34.0, 41.0, 0.0)}  # GPSInfo
        exif[0x0112] = 6  # orientation: rotate 90
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


def _zip(entries: dict[str, bytes]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_groups_by_top_folder_and_strips_wrapper():
    zf = _zip({
        "my items/sony headphones/1.jpg": _jpeg(),
        "my items/sony headphones/box/2.jpg": _jpeg(),
        "my items/кресло/1.jpg": _jpeg(),
        "my items/кресло/video.mov": b"x",
        "__MACOSX/my items/._1.jpg": b"x",
    })
    res = scan_zip(zf)
    assert sorted(g.hint for g in res.groups) == ["sony headphones", "кресло"]
    sony = next(g for g in res.groups if g.hint == "sony headphones")
    assert len(sony.members) == 2
    assert res.videos == 1


def test_single_item_folder_is_not_stripped():
    zf = _zip({"lamp/1.jpg": _jpeg(), "lamp/2.jpg": _jpeg()})
    res = scan_zip(zf)
    assert [g.hint for g in res.groups] == ["lamp"]


def test_loose_root_photos_are_skipped():
    zf = _zip({"loose.jpg": _jpeg(), "chair/1.jpg": _jpeg()})
    res = scan_zip(zf)
    assert [g.hint for g in res.groups] == ["chair"]
    assert res.loose_photos == 1


def test_process_photo_strips_exif_and_rotates(tmp_path: Path):
    src = tmp_path / "in.jpg"
    src.write_bytes(_jpeg(size=(400, 300), exif_gps=True))
    out = process_photo(src, tmp_path, item_id=7, index=0, max_side=200, thumb_side=50)
    saved = Image.open(tmp_path / out.rel_path)
    assert saved.size == (150, 200)  # rotated (portrait) and resized to max side 200
    assert not saved.getexif()       # no EXIF at all -> no GPS
    assert max(Image.open(tmp_path / out.rel_thumb).size) == 50
    assert ai_jpeg_bytes(src, 100)[:2] == b"\xff\xd8"


def test_decode_windows_cyrillic_name():
    from app.services.intake import decode_name

    info = zipfile.ZipInfo("кресло/1.jpg".encode("cp866").decode("cp437"))
    info.flag_bits = 0
    assert decode_name(info) == "кресло/1.jpg"
    info2 = zipfile.ZipInfo("кресло/1.jpg".encode().decode("cp437"))
    info2.flag_bits = 0
    assert decode_name(info2) == "кресло/1.jpg"
