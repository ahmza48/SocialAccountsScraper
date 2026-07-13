import json

from scrapers.base import BaseScraper
from core.logging_config import get_logger
from core.exceptions import ParsingError, ScrapingError, AccountBlockedError
from core.platforms import Platform
from utils.anti_detection import human_delay

logger = get_logger(__name__)


class FacebookScraper(BaseScraper):
    """Facebook scraper using web interface with session reuse."""

    PLATFORM = Platform.FACEBOOK.value
    BASE_URL = "https://www.facebook.com"

    def _navigate_to_platform(self):
        self._page.goto(self.BASE_URL, wait_until="networkidle", timeout=30000)

    def login(self):
        """Login to Facebook using stored credentials."""
        creds = self.account["credentials"]
        self._page.goto(self.BASE_URL, wait_until="networkidle", timeout=30000)
        human_delay(1.0, 2.0)

        # Accept cookies if prompted
        try:
            self._page.click(
                'button[data-cookiebanner="accept_button"]', timeout=3000
            )
        except Exception:
            pass

        human_delay(0.5, 1.5)
        self._page.fill("#email", creds["username"], timeout=10000)
        human_delay(0.3, 0.8)
        self._page.fill("#pass", creds["password"], timeout=10000)
        human_delay(0.5, 1.0)
        self._page.click('button[name="login"]', timeout=10000)
        self._page.wait_for_load_state("networkidle", timeout=30000)
        human_delay(2.0, 4.0)

    def scrape_profile(self, username: str) -> dict:
        """Scrape Facebook profile page."""
        url = f"{self.BASE_URL}/{username}"
        try:
            response = self._page.goto(
                url, wait_until="networkidle", timeout=30000
            )
            if response.status == 404:
                raise ScrapingError(f"User {username} not found on Facebook")
            if response.status in (401, 403):
                raise AccountBlockedError(
                    "Account blocked or requires checkpoint on Facebook"
                )
            if response.status != 200:
                raise ScrapingError(
                    f"Facebook returned status {response.status}"
                )

            human_delay(1.0, 2.0)

            # Extract profile info from page content
            title = self._page.title()
            profile = {"username": username, "display_name": title}

            # Try to extract follower count from page text
            try:
                page_text = self._page.inner_text("body")
                import re

                follower_match = re.search(
                    r"([\d,\.]+[KMB]?)\s+followers", page_text, re.IGNORECASE
                )
                if follower_match:
                    profile["follower_count"] = follower_match.group(1)

                friends_match = re.search(
                    r"([\d,\.]+[KMB]?)\s+friends", page_text, re.IGNORECASE
                )
                if friends_match:
                    profile["friends_count"] = friends_match.group(1)
            except Exception:
                pass

            return profile
        except (ParsingError, ScrapingError, AccountBlockedError):
            raise
        except Exception as e:
            raise ScrapingError(
                f"Failed to scrape Facebook profile for {username}: {e}"
            )

    def scrape_posts(self, username: str, cursor: str = None) -> dict:
        """Scrape Facebook posts.

        Facebook's GraphQL API is heavily protected, so this uses page
        scrolling as a fallback. Cursor support is limited compared to
        Instagram/TikTok.
        """
        if not cursor:
            url = f"{self.BASE_URL}/{username}"
            self._page.goto(url, wait_until="networkidle", timeout=30000)
            human_delay(1.0, 2.0)

        # Scroll to load posts
        items = []
        try:
            for _ in range(3):
                self._page.evaluate("window.scrollBy(0, window.innerHeight)")
                human_delay(1.5, 3.0)

            # Extract visible post data from page
            posts_data = self._page.evaluate("""
                () => {
                    const posts = document.querySelectorAll('[data-ad-preview="message"]');
                    return Array.from(posts).slice(0, 12).map((el, i) => ({
                        id: i.toString(),
                        text: el.innerText || '',
                    }));
                }
            """)

            for post in posts_data:
                items.append({
                    "id": post.get("id"),
                    "text": post.get("text", ""),
                })
        except Exception as e:
            logger.warning(f"Error scraping Facebook posts: {e}")

        return {
            "items": items,
            "next_cursor": None,
            "has_more": False,
        }
