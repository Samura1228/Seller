from datetime import datetime
from types import SimpleNamespace

import pytest
from telethon import functions, types

from app.userbot.chat_inspector import ChatCheckError, inspect_chat


def channel(**kw):
    base = dict(id=123, title="Kipr", photo=types.ChatPhotoEmpty(), date=datetime(2020, 1, 1), username="kipr6",
                megagroup=True, access_hash=1)
    base.update(kw)
    return types.Channel(**base)


class FakeClient:
    def __init__(self, entity, pinned=(), recent=(), about="Правила: 1 пост в день", slowmode=None):
        self.entity, self.pinned, self.recent = entity, list(pinned), list(recent)
        self.full = SimpleNamespace(full_chat=SimpleNamespace(about=about, slowmode_seconds=slowmode, participants_count=5000))

    async def get_entity(self, ref):
        if isinstance(self.entity, Exception):
            raise self.entity
        return self.entity

    async def __call__(self, request):
        assert isinstance(request, functions.channels.GetFullChannelRequest)
        return self.full

    async def iter_messages(self, entity, limit=None, filter=None):
        for m in (self.pinned if filter is not None else self.recent)[:limit]:
            yield m


def msg(text=None, media=None):
    return SimpleNamespace(message=text, media=media)


async def test_group_ok():
    c = FakeClient(channel(), pinned=[msg("Хэштег #продам обязателен"), msg(None, media=object())],
                   recent=[msg("Продам велосипед, Лимасол")] * 3, slowmode=300)
    snap = await inspect_chat(c, "https://t.me/kipr6")
    assert snap.tg_chat_id == -1000000000123 and snap.kind == "group" and snap.can_post and snap.can_send_photos
    assert snap.slowmode_seconds == 300 and snap.members_count == 5000
    assert snap.pinned_texts == ["Хэштег #продам обязателен"] and snap.pinned_media_without_text == 1
    assert "[Chat description]" in snap.rules_source() and "images/files without text" in snap.rules_source()


async def test_problems_detected():
    no_photos = types.ChatBannedRights(until_date=None, send_photos=True)
    snap = await inspect_chat(FakeClient(channel(left=True, default_banned_rights=no_photos)), "@kipr6")
    assert not snap.can_post and not snap.can_send_photos and len(snap.problems) == 2

    snap = await inspect_chat(FakeClient(channel(megagroup=False, broadcast=True)), "@kipr6")
    assert snap.kind == "channel" and not snap.can_post

    with pytest.raises(ChatCheckError):
        await inspect_chat(FakeClient(ValueError("No user has 'nope' as username")), "@nope")
    user = types.User(id=1, first_name="x")
    with pytest.raises(ChatCheckError):
        await inspect_chat(FakeClient(user), "@someone")


async def test_forum_topics():
    from app.userbot.chat_inspector import guess_sale_topic

    topics = [types.ForumTopic(id=1, date=None, peer=types.PeerChannel(123), title="General", icon_color=0, top_message=1,
                               read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                               unread_reactions_count=0, unread_poll_votes_count=0, from_id=types.PeerUser(1),
                               notify_settings=types.PeerNotifySettings()),
              types.ForumTopic(id=7, date=None, peer=types.PeerChannel(123), title="📁 Правила", icon_color=0, top_message=7,
                               read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                               unread_reactions_count=0, unread_poll_votes_count=0, from_id=types.PeerUser(1),
                               notify_settings=types.PeerNotifySettings()),
              types.ForumTopic(id=9, date=None, peer=types.PeerChannel(123), title="# Барахолка", icon_color=0, top_message=9,
                               read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                               unread_reactions_count=0, unread_poll_votes_count=0, from_id=types.PeerUser(1),
                               notify_settings=types.PeerNotifySettings())]

    class ForumClient(FakeClient):
        def __init__(self):
            super().__init__(channel(forum=True), recent=[msg("Продам диван")] * 3)
            self.calls = []

        async def __call__(self, request):
            if isinstance(request, functions.messages.GetForumTopicsRequest):
                return SimpleNamespace(topics=topics)
            return await super().__call__(request)

        async def iter_messages(self, entity, limit=None, filter=None, reply_to=None):
            self.calls.append(reply_to)
            if reply_to == 7:
                yield msg("Правила: 1 объявление в день в теме Барахолка")
                return
            async for m in super().iter_messages(entity, limit=limit, filter=filter):
                yield m

    c = ForumClient()
    snap = await inspect_chat(c, "@kipr6")
    assert snap.kind == "forum" and snap.can_post and snap.guessed_topic_id == 9
    assert [t["title"] for t in snap.forum_topics] == ["General", "📁 Правила", "# Барахолка"]
    assert "Правила: 1 объявление в день" in snap.rules_source() and "[9] # Барахолка" in snap.rules_source()
    assert 9 in c.calls  # recent messages for language are read from the sale topic
    assert guess_sale_topic([{"id": 1, "title": "General"}, {"id": 2, "title": "Новости"}]) is None
    assert guess_sale_topic([{"id": 3, "title": "Продам", "closed": True}, {"id": 4, "title": "Sell & Buy"}]) == 4
