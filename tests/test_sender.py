from types import SimpleNamespace

from app.userbot.sender import send_post


class FakeClient:
    def __init__(self, entity_ok=True):
        self.log, self.entity_ok, self._id = [], entity_ok, 0

    async def get_input_entity(self, ref):
        if isinstance(ref, int) and not self.entity_ok:
            raise ValueError("not cached")
        return f"peer:{ref}"

    def _msg(self):
        self._id += 1
        return SimpleNamespace(id=self._id)

    async def send_file(self, peer, files, caption=None, parse_mode=None):
        self.log.append(("file", peer, files, caption))
        return [self._msg() for _ in files] if isinstance(files, list) else self._msg()

    async def send_message(self, peer, text, parse_mode=None, link_preview=False, reply_to=None):
        self.log.append(("text", peer, text, reply_to))
        return self._msg()


async def test_send_variants(tmp_path):
    photos = []
    for i in range(12):
        p = tmp_path / f"{i}.jpg"
        p.write_bytes(b"x")
        photos.append(p)

    c = FakeClient()
    assert await send_post(c, -100, "chat", "short", photos, True) == list(range(1, 11))  # album capped at 10
    assert c.log[0][0] == "file" and c.log[0][3] == "short" and len(c.log[0][2]) == 10

    c = FakeClient()
    ids = await send_post(c, -100, "chat", "x" * 1500, photos[:2], True)  # too long for a caption
    assert ids == [1, 2, 3] and c.log[0][3] is None and c.log[1] == ("text", "peer:-100", "x" * 1500, 1)

    c = FakeClient(entity_ok=False)  # not in the dialog cache -> resolved by username
    assert await send_post(c, -100, "chat", "t", photos[:1], False) == [1]
    assert c.log == [("text", "peer:@chat", "t", None)]
