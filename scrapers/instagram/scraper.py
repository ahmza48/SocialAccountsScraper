import json

from scrapers.base import BaseScraper
from core.logging_config import get_logger
from core.exceptions import ParsingError, ScrapingError, AccountBlockedError
from core.platforms import Platform
from utils.anti_detection import human_delay

logger = get_logger(__name__)


class InstagramScraper(BaseScraper):
    """Instagram scraper using web API endpoints (not UI scrolling)."""

    PLATFORM = Platform.INSTAGRAM.value
    BASE_URL = "https://www.instagram.com"

    def _navigate_to_platform(self):
        self._page.goto(self.BASE_URL, wait_until="networkidle", timeout=30000)

    def login(self):
        """Login to Instagram using stored credentials."""
        creds = self.account["credentials"]
        self._page.goto(
            f"{self.BASE_URL}/accounts/login/",
            wait_until="networkidle",
            timeout=30000,
        )
        human_delay(1.0, 2.0)

        # Accept cookies if prompted
        try:
            self._page.click("button:has-text('Allow')", timeout=3000)
        except Exception:
            pass

        human_delay(0.5, 1.5)
        self._page.fill('input[name="username"]', creds["username"], timeout=10000)
        human_delay(0.3, 0.8)
        self._page.fill('input[name="password"]', creds["password"], timeout=10000)
        human_delay(0.5, 1.0)
        self._page.click('button[type="submit"]', timeout=10000)
        self._page.wait_for_load_state("networkidle", timeout=30000)
        human_delay(2.0, 4.0)

        # Dismiss "Save Login Info" prompt
        try:
            self._page.click("button:has-text('Not Now')", timeout=5000)
        except Exception:
            pass

        # Dismiss notifications prompt
        try:
            self._page.click("button:has-text('Not Now')", timeout=5000)
        except Exception:
            pass

    def scrape_profile(self, username: str) -> dict:
        """Scrape Instagram profile via web API (not HTML parsing)."""
        url = f"{self.BASE_URL}/api/v1/users/web_profile_info/?username={username}"
        try:
            response = self._page.goto(
                url, wait_until="networkidle", timeout=20000
            )
            if response.status == 404:
                raise ScrapingError(f"User {username} not found on Instagram")
            if response.status == 401:
                raise AccountBlockedError(
                    "Account blocked or rate limited by Instagram"
                )
            if response.status != 200:
                raise ScrapingError(
                    f"Instagram API returned status {response.status}"
                )

            body = self._page.inner_text("pre")
            data = json.loads(body)
            user_data = data.get("data", {}).get("user", {})

            if not user_data:
                raise ParsingError(f"No user data found for {username}")

            return {
                "username": user_data.get("username"),
                "full_name": user_data.get("full_name"),
                "biography": user_data.get("biography"),
                "follower_count": user_data.get("edge_followed_by", {}).get(
                    "count", 0
                ),
                "following_count": user_data.get("edge_follow", {}).get(
                    "count", 0
                ),
                "post_count": user_data.get(
                    "edge_owner_to_timeline_media", {}
                ).get("count", 0),
                "is_private": user_data.get("is_private", False),
                "is_verified": user_data.get("is_verified", False),
                "profile_pic_url": user_data.get("profile_pic_url_hd"),
                "external_url": user_data.get("external_url"),
            }
        except (ParsingError, ScrapingError, AccountBlockedError):
            raise
        except Exception as e:
            raise ScrapingError(
                f"Failed to scrape Instagram profile for {username}: {e}"
            )

    def scrape_posts(self, username: str, cursor: str = None) -> dict:
        """Scrape Instagram posts using cursor-based pagination via GraphQL API."""
        query_hash = "e769aa130647d2571c27c44596fb5e7d"
        variables = {"username": username, "first": 12}
        if cursor:
            variables["after"] = cursor

        url = (
            f"{self.BASE_URL}/graphql/query/"
            f"?query_hash={query_hash}"
            f"&variables={json.dumps(variables)}"
        )
        try:
            response = self._page.goto(
                url, wait_until="networkidle", timeout=20000
            )
            if response.status != 200:
                raise ScrapingError(
                    f"Instagram posts API returned {response.status}"
                )

            body = self._page.inner_text("pre")
            data = json.loads(body)

            media = (
                data.get("data", {})
                .get("user", {})
                .get("edge_owner_to_timeline_media", {})
            )
            page_info = media.get("page_info", {})
            edges = media.get("edges", [])

            items = []
            for edge in edges:
                node = edge.get("node", {})
                caption_edges = (
                    node.get("edge_media_to_caption", {}).get("edges", [])
                )
                caption = (
                    caption_edges[0].get("node", {}).get("text", "")
                    if caption_edges
                    else ""
                )
                items.append({
                    "id": node.get("id"),
                    "shortcode": node.get("shortcode"),
                    "type": node.get("__typename"),
                    "caption": caption,
                    "like_count": node.get("edge_liked_by", {}).get(
                        "count", 0
                    ),
                    "comment_count": node.get(
                        "edge_media_to_comment", {}
                    ).get("count", 0),
                    "timestamp": node.get("taken_at_timestamp"),
                    "display_url": node.get("display_url"),
                    "is_video": node.get("is_video", False),
                    "video_view_count": node.get("video_view_count", 0),
                })

            return {
                "items": items,
                "next_cursor": page_info.get("end_cursor"),
                "has_more": page_info.get("has_next_page", False),
            }
        except (ParsingError, ScrapingError):
            raise
        except Exception as e:
            raise ScrapingError(
                f"Failed to scrape Instagram posts for {username}: {e}"
            )