"""
Shared PyTest fixtures for the LLM-Gate functional test suite.

- One headless Chrome is created per test session (fast).
- Each test gets a clean session via the `browser` fixture (cookies cleared).
- The app under test is chosen with the APP_URL env var
  (default: http://localhost:5000).
"""

import os

import pytest
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


def _make_headless_chrome():
    """Build a headless Chrome driver using a chromedriver from webdriver-manager."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


@pytest.fixture(scope="session")
def app_url():
    """Base URL of the app under test (override with the APP_URL env var)."""
    return os.environ.get("APP_URL", "http://localhost:5000").rstrip("/")


@pytest.fixture(scope="session")
def _driver():
    """Create a single headless Chrome instance for the whole test session."""
    driver = _make_headless_chrome()
    driver.implicitly_wait(5)
    yield driver
    driver.quit()


@pytest.fixture()
def browser(_driver, app_url):
    """
    Per-test driver with a clean session.

    We must be on the app's domain before cookies can be cleared, so we load the
    login page first and then wipe cookies to reset any prior login state.
    """
    try:
        _driver.get(f"{app_url}/login")
        _driver.delete_all_cookies()
    except WebDriverException as exc:
        pytest.fail(
            f"Could not reach the app at {app_url}. "
            f"Is generator/sample_app.py running?\n{exc}"
        )
    return _driver
