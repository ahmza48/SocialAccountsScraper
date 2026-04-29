import abc
from typing import Optional

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from core.config import Config
from core.logging_config import get_logger
from core.exceptions import SessionExpiredError
from sessions.manager import SessionManager
from utils.anti_detection import human_delay, random_viewport, random_user_agent

logger = get_logger(__name__)


class BaseScraper(abc.ABC):
    """Base scraper with Playwright lifecycle, session management, and anti-detection.

    Subclasses must set PLATFORM and implement:
        - login()
        - scrape_profile(username)
        - scrape_posts(username, cursor)
        - _navigate_to_platform()
    """

    PLATFORM: str = ""

    def __init__(self, account: dict, session_manager: SessionManager):
        self.account = account
        self.account_id = account["account_id"]
        self.proxy = account.get("proxy") or None
        self.session_manager = session_manager
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ── Browser lifecycle ──────────────────────────────────────────

    def start_browser(self):
        """Launch Chromium with anti-detection settings and optional proxy."""
        self._playwright = sync_playwright().start()

        launch_args = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }
        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}

        self._browser = self._playwright.chromium.launch(**launch_args)

        viewport = random_viewport()
        context_args = {
            "viewport": viewport,
            "user_agent": random_user_agent(),
        }

        # Reuse existing session if available
        storage_state = self.session_manager.load_session(
            self.PLATFORM, self.account_id
        )
        if storage_state:
            context_args["storage_state"] = storage_state
            logger.info(f"Reusing existing session for {self.account_id}")

        self._context = self._browser.new_context(**context_args)
        self._page = self._context.new_page()

    def close_browser(self):
        """Save session state, then cleanly close all browser resources."""
        try:
            if self._context:
                state = self._context.storage_state()
                self.session_manager.save_session(
                    self.PLATFORM, self.account_id, state
                )
        except Exception as e:
            logger.warning(f"Failed to save session on close: {e}")

        for resource in (self._page, self._context, self._browser):
            try:
                if resource:
                    resource.close()
            except Exception as e:
                logger.warning(f"Error closing browser resource: {e}")

        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning(f"Error stopping playwright: {e}")
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None

    # ── Session validation ─────────────────────────────────────────

    def validate_session(self) -> bool:
        """Navigate to platform and verify session is still valid."""
        if not self._page:
            return False
        return self.session_manager.validate_session_page(
            self._page, self.PLATFORM
        )

    # ── Abstract methods ───────────────────────────────────────────

    @abc.abstractmethod
    def login(self):
        """Platform-specific login flow. Called when no valid session exists."""
        ...

    @abc.abstractmethod
    def scrape_profile(self, username: str) -> dict:
        """Scrape profile data for a username."""
        ...

    @abc.abstractmethod
    def scrape_posts(self, username: str, cursor: str = None) -> dict:
        """Scrape posts with cursor-based pagination."""
        ...

    @abc.abstractmethod
    def _navigate_to_platform(self):
        """Navigate to platform home to check session validity."""
        ...

    # ── Execution orchestrator ─────────────────────────────────────

    def execute(self, username: str, cursor: str = None) -> dict:
        """Full scrape execution: browser → session → login → scrape → cleanup."""
        self.start_browser()
        try:
            # Determine if login is needed
            needs_login = not self.session_manager.has_session(
                self.PLATFORM, self.account_id
            )
            if not needs_login:
                self._navigate_to_platform()
                human_delay()
                if not self.validate_session():
                    self.session_manager.mark_invalid(
                        self.PLATFORM, self.account_id, "login redirect"
                    )
                    needs_login = True

            if needs_login:
                logger.info(f"Logging in with account {self.account_id}")
                self.login()
                human_delay(2.0, 4.0)
                if not self.validate_session():
                    raise SessionExpiredError(
                        "Login failed — session still invalid"
                    )

            # Scrape profile
            human_delay()
            profile = self.scrape_profile(username)

            # Scrape posts (cursor-based pagination)
            human_delay()
            posts = self.scrape_posts(username, cursor)

            return {
                "profile": profile,
                "posts": posts.get("items", []),
                "next_cursor": posts.get("next_cursor"),
                "has_more": posts.get("has_more", False),
            }
        finally:
            self.close_browser()
