import datetime
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, MessageEntity
from yt_dlp.extractor import gen_extractor_classes

import bot
from links import InvalidLink, clean_pasted_url, message_url, normalize_url, validate_web_url
from settings import Limits, Settings


EXAMPLES = (
    "https://www.youtube.com/watch?v=BaW_jenozKc",
    "https://youtu.be/BaW_jenozKc",
    "https://www.youtube.com/shorts/BaW_jenozKc",
    "https://www.facebook.com/watch/?v=10102566283414033",
    "https://www.facebook.com/reel/119528914762838",
    "https://www.facebook.com/share/v/123456789/",
    "https://x.com/user/status/1234567890123456789",
    "https://www.reddit.com/r/videos/comments/abc123/example/",
    "https://v.redd.it/abcdef123456",
    "https://www.instagram.com/reel/CxAbCdEf123/",
    "https://www.tiktok.com/@example/video/7123456789012345678",
    "https://vimeo.com/76979871",
    "https://www.dailymotion.com/video/x84sh87",
    "https://www.twitch.tv/videos/123456789",
    "https://video.example.com/clip.mp4?signature=a%2Fb%3D&expires=123",
    "https://new-site.example.com/article/embedded-video",
)


class LinkTests(unittest.TestCase):
    def test_tiktok_direct_and_share_links_select_tiktok_extractors(self):
        extractors = gen_extractor_classes()
        urls = (
            "https://www.tiktok.com/@example/video/7123456789012345678",
            "https://tiktok.com/@example/video/7123456789012345678?is_from_webapp=1",
            "https://m.tiktok.com/@example/video/7123456789012345678",
            "https://vm.tiktok.com/ZMExample/",
            "https://vt.tiktok.com/ZSExample/",
            "https://www.tiktok.com/t/ZTExample/",
            "https://tiktok.com/t/ZTExample/",
        )
        for url in urls:
            with self.subTest(url=url):
                normalized = message_url(NS(text=f"Watch this: {url}"))
                matches = [ie.ie_key().lower() for ie in extractors if ie.suitable(normalized)]
                self.assertTrue(any(name.startswith("tiktok") for name in matches), normalized)
        self.assertEqual(normalize_url(urls[1]),
                         "https://www.tiktok.com/@example/video/7123456789012345678?is_from_webapp=1")
        self.assertEqual(normalize_url(urls[-1]), urls[-2])
        for url in urls[3:-1]:
            self.assertEqual(normalize_url(url), url)

    def test_many_sites_and_generic_pages_reach_downloader(self):
        for url in EXAMPLES:
            with self.subTest(url=url):
                self.assertEqual(message_url(NS(text=url)), normalize_url(url))

    def test_named_sites_match_installed_ytdlp_extractors(self):
        extractors = gen_extractor_classes()
        for url in (EXAMPLES[0], EXAMPLES[3], EXAMPLES[6], EXAMPLES[7], EXAMPLES[9], EXAMPLES[10], EXAMPLES[11]):
            with self.subTest(url=url):
                matches = [ie.IE_NAME for ie in extractors if ie.IE_NAME != "generic" and ie.suitable(url)]
                self.assertTrue(matches, url)

    def test_caption_link_and_hidden_link(self):
        caption = Message(1, datetime.datetime.now(datetime.timezone.utc), Chat(42, "private"), caption=EXAMPLES[3])
        self.assertEqual(message_url(caption), EXAMPLES[3])
        entity = MessageEntity(MessageEntity.TEXT_LINK, offset=0, length=5, url=EXAMPLES[10])
        hidden = Message(2, datetime.datetime.now(datetime.timezone.utc), Chat(42, "private"), text="watch", entities=[entity])
        self.assertEqual(message_url(hidden), EXAMPLES[10])
        caption_hidden = Message(3, datetime.datetime.now(datetime.timezone.utc), Chat(42, "private"), caption="watch", caption_entities=[entity])
        self.assertEqual(message_url(caption_hidden), EXAMPLES[10])

    def test_telegram_utf16_offsets_and_schemeless_entity(self):
        url = "vimeo.com/76979871"
        entity = MessageEntity(MessageEntity.URL, offset=3, length=len(url))
        message = Message(1, datetime.datetime.now(datetime.timezone.utc), Chat(42, "private"),
                          text="🎬 " + url, entities=[entity])
        self.assertEqual(message_url(message), "https://" + url)

    def test_first_link_wins_across_entity_types(self):
        url = EXAMPLES[0]
        text = url + " and watch"
        entity = MessageEntity(MessageEntity.TEXT_LINK, offset=len(url) + 5, length=5, url=EXAMPLES[3])
        message = Message(1, datetime.datetime.now(datetime.timezone.utc), Chat(42, "private"), text=text, entities=[entity])
        self.assertEqual(message_url(message), url)

    def test_balanced_punctuation_and_signed_query_preserved(self):
        self.assertEqual(message_url(NS(text="See (https://example.com/video).")), "https://example.com/video")
        self.assertEqual(message_url(NS(text="https://example.com/video_(part_1)")), "https://example.com/video_(part_1)")
        self.assertEqual(normalize_url(EXAMPLES[-2]), EXAMPLES[-2])

    def test_case_normalization_preserves_path(self):
        self.assertEqual(normalize_url("HTTPS://WWW.YOUTUBE.COM/watch?v=AbCdEf12345"),
                         "https://www.youtube.com/watch?v=AbCdEf12345")

    def test_rejects_invalid_local_and_credential_urls(self):
        urls = ("file:///etc/passwd", "ftp://example.com/video", "javascript:alert(1)", "https://",
                "http://localhost/a", "http://127.0.0.1/a", "http://192.168.1.1/a", "http://[::1]/a",
                "https://user:password@example.com/video", "https://example.com:bad/video", "http://host.internal/a",
                "https://example.com/\nvideo", "https://example.com\\@localhost/a")
        for url in urls:
            with self.subTest(url=url), self.assertRaises(InvalidLink):
                validate_web_url(url)

    def test_ordinary_text_does_not_create_request(self):
        self.assertIsNone(message_url(NS(text="Just chatting without a link")))


class LinkHandlerTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        state = bot.BotState(Settings("fake", 42, -10042, Limits()))
        return NS(application=NS(bot_data={"state": state})), state

    async def test_facebook_link_creates_buttons_in_allowed_group(self):
        ctx, state = self.context()
        message = NS(text=EXAMPLES[3], reply_text=AsyncMock(return_value=NS(message_id=1, edit_text=AsyncMock())))
        update = NS(message=message, effective_chat=NS(id=-10042, type="supergroup"), effective_user=NS(id=43, is_bot=False))
        await bot.handle_link(update, ctx)
        self.assertEqual(next(iter(state.pending.values())).url, EXAMPLES[3])
        self.assertEqual(message.reply_text.await_count, 2)

    async def test_invalid_link_creates_no_pending_request(self):
        ctx, state = self.context()
        message = NS(text="http://localhost/video", reply_text=AsyncMock())
        update = NS(message=message, effective_chat=NS(id=42, type="private"), effective_user=NS(id=42, is_bot=False))
        await bot.handle_link(update, ctx)
        self.assertFalse(state.pending)
        self.assertIn("public", message.reply_text.call_args.args[0])

    async def test_access_checked_before_parsing_any_url(self):
        ctx, state = self.context()
        update = NS(message=NS(text=EXAMPLES[3]), effective_chat=NS(id=99, type="private"), effective_user=NS(id=99, is_bot=False))
        with patch.object(bot, "message_url") as parse:
            await bot.handle_link(update, ctx)
            parse.assert_not_called()
        self.assertFalse(state.pending)
