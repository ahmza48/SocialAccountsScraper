import json

from scrapers.base import BaseScraper
from core.logging_config import get_logger
from core.exceptions import ParsingError, ScrapingError, AccountBlockedError
from utils.anti_detection import human_delay

logger = get_logger(__name__)


class TikTokScraper(BaseScraper):
    """TikTok scraper using web API endpoints."""

    PLATFORM = "tiktok"
    BASE_URL = "https://www.tiktok.com"

    def _navigate_to_platform(self):
        self._page.goto(self.BASE_URL, wait_until="networkidle", timeout=30000)

    def login(self):
        """Login to TikTok using stored credentials."""
        creds = self.account["credentials"]
        self._page.goto(
            f"{self.BASE_URL}/login/phone-or-email/email",
            wait_until="networkidle",
            timeout=30000,
        )
        human_delay(1.5, 3.0)

        # Accept cookies if prompted
        try:
            self._page.click("button:has-text('Accept')", timeout=3000)
        except Exception:
            pass

        human_delay(0.5, 1.5)
        self._page.fill(
            'input[name="username"], input[type="text"]',
            creds["username"],
            timeout=10000,
        )
        human_delay(0.3, 0.8)
        self._page.fill('input[type="password"]', creds["password"], timeout=10000)
        human_delay(0.5, 1.0)
        self._page.click('button[type="submit"]', timeout=10000)
        self._page.wait_for_load_state("networkidle", timeout=30000)
        human_delay(2.0, 4.0)

    def scrape_profile(self, username: str) -> dict:
        """Scrape TikTok profile via web API."""
        url = f"{self.BASE_URL}/api/user/detail/?uniqueId={username}"
        try:
            response = self._page.goto(
                url, wait_until="networkidle", timeout=20000
            )
            if response.status == 404:
                raise ScrapingError(f"User {username} not found on TikTok")
            if response.status in (401, 403):
                raise AccountBlockedError(
                    "Account blocked or rate limited by TikTok"
                )
            if response.status != 200:
                raise ScrapingError(
                    f"TikTok API returned status {response.status}"
                )

            body = self._page.inner_text("pre")
            data = json.loads(body)
            user_info = data.get("userInfo", {})
            user = user_info.get("user", {})
            stats = user_info.get("stats", {})

            if not user:
                raise ParsingError(f"No user data found for {username}")

            return {
                "username": user.get("uniqueId"),
                "nickname": user.get("nickname"),
                "bio": user.get("signature"),
                "follower_count": stats.get("followerCount", 0),
                "following_count": stats.get("followingCount", 0),
                "like_count": stats.get("heartCount", 0),
                "video_count": stats.get("videoCount", 0),
                "is_verified": user.get("verified", False),
                "profile_pic_url": user.get("avatarLarger"),
            }
        except (ParsingError, ScrapingError, AccountBlockedError):
            raise
        except Exception as e:
            raise ScrapingError(
                f"Failed to scrape TikTok profile for {username}: {e}"
            )

    def scrape_posts(self, username: str, cursor: str = None) -> dict:
        """Scrape TikTok posts using cursor-based pagination."""
        params = f"uniqueId={username}&count=12"
        if cursor:
            params += f"&cursor={cursor}"

        url = f"{self.BASE_URL}/api/post/item_list/?{params}"
        try:
            response = self._page.goto(
                url, wait_until="networkidle", timeout=20000
            )
            if response.status != 200:
                raise ScrapingError(
                    f"TikTok posts API returned {response.status}"
                )

            body = self._page.inner_text("pre")
            data = json.loads(body)

            item_list = data.get("itemList", [])
            has_more = data.get("hasMore", False)
            next_cursor = str(data.get("cursor", "")) if has_more else None

            items = []
            for item in item_list:
                stats = item.get("stats", {})
                items.append({
                    "id": item.get("id"),
                    "description": item.get("desc", ""),
                    "create_time": item.get("createTime"),
                    "like_count": stats.get("diggCount", 0),
                    "comment_count": stats.get("commentCount", 0),
                    "share_count": stats.get("shareCount", 0),
                    "play_count": stats.get("playCount", 0),
                    "cover_url": item.get("video", {}).get("cover"),
                    "duration": item.get("video", {}).get("duration", 0),
                })

            return {
                "items": items,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
        except (ParsingError, ScrapingError):
            raise
        except Exception as e:
            raise ScrapingError(
                f"Failed to scrape TikTok posts for {username}: {e}"
            )
