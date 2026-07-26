"""
Selenium + PyTest functional tests for generator/sample_app.py.

Start the app first:
    python generator/sample_app.py
Then run the suite:
    pytest
Point at a different host with:
    APP_URL=http://1.2.3.4:5000 pytest
"""

import pytest
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

USERNAME = "admin"
PASSWORD = "password123"
WAIT_SECONDS = 10


def _login(browser, app_url, username, password):
    """Fill in and submit the login form."""
    browser.get(f"{app_url}/login")
    browser.find_element(By.NAME, "username").send_keys(username)
    browser.find_element(By.NAME, "password").send_keys(password)
    browser.find_element(By.ID, "login-btn").click()


def test_home_redirects_to_login_when_not_authenticated(browser, app_url):
    """An unauthenticated visit to '/' must redirect to the login page."""
    browser.get(f"{app_url}/")
    WebDriverWait(browser, WAIT_SECONDS).until(EC.url_contains("/login"))
    assert "/login" in browser.current_url
    assert browser.find_element(By.ID, "login-btn").is_displayed()


def test_login_page_loads(browser, app_url):
    """The login page renders with its submit button present."""
    browser.get(f"{app_url}/login")
    assert browser.find_element(By.ID, "login-btn").is_displayed()


def test_login_with_valid_credentials(browser, app_url):
    """Valid credentials should log in and land on the welcome page."""
    _login(browser, app_url, USERNAME, PASSWORD)
    welcome = WebDriverWait(browser, WAIT_SECONDS).until(
        EC.presence_of_element_located((By.ID, "welcome"))
    )
    assert "Welcome" in welcome.text
    assert USERNAME in welcome.text


def test_login_with_invalid_credentials(browser, app_url):
    """Bad credentials should keep the user on login and show an error."""
    _login(browser, app_url, USERNAME, "wrong-password")
    error = WebDriverWait(browser, WAIT_SECONDS).until(
        EC.presence_of_element_located((By.ID, "error"))
    )
    assert "Invalid" in error.text


def test_logout_clears_session(browser, app_url):
    """After logout the session is gone, so '/' redirects back to login."""
    _login(browser, app_url, USERNAME, PASSWORD)
    WebDriverWait(browser, WAIT_SECONDS).until(
        EC.presence_of_element_located((By.ID, "welcome"))
    )
    browser.find_element(By.ID, "logout-link").click()
    WebDriverWait(browser, WAIT_SECONDS).until(EC.url_contains("/login"))

    # Session must be cleared: hitting home should bounce back to login.
    browser.get(f"{app_url}/")
    WebDriverWait(browser, WAIT_SECONDS).until(EC.url_contains("/login"))
    assert browser.find_element(By.ID, "login-btn").is_displayed()


@pytest.mark.security
def test_debug_mode_disabled(app_url):
    """
    SECURITY: a deployed Flask app must not run with debug=True.

    EXPECTED TO FAIL against the current sample_app.py - debug=True is the planted
    flaw, and catching it is the whole point. We trigger a server error and assert
    the interactive Werkzeug debugger is NOT exposed in the response.
    """
    response = requests.get(f"{app_url}/crash", timeout=WAIT_SECONDS)

    # These strings only appear in Werkzeug's interactive debugger (debug=True);
    # a production 500 page ("Internal Server Error") contains none of them.
    debugger_markers = ["Werkzeug Debugger", "Traceback (most recent call last)"]
    exposed = [marker for marker in debugger_markers if marker in response.text]
    assert not exposed, (
        f"Werkzeug interactive debugger is exposed ({exposed}). Flask is running "
        "with debug=True - a remote-code-execution risk. Disable debug before deploy."
    )


def test_home_page_title(browser, app_url):
    """Basic sanity check: the authenticated home page has the expected title."""
    _login(browser, app_url, USERNAME, PASSWORD)
    WebDriverWait(browser, WAIT_SECONDS).until(
        EC.presence_of_element_located((By.ID, "welcome"))
    )
    assert browser.title == "Home"
