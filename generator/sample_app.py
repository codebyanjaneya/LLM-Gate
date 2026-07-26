"""
sample_app.py - Minimal Flask app used as the "AI-generated" application under test.

This stands in for code that an LLM might produce. It is intentionally tiny so our
Selenium + PyTest suite has concrete routes to exercise (login, home, logout).

"""

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    session,
    render_template_string,
)

app = Flask(__name__)
# Hardcoded secret key - fine for a local demo, NOT for real use.
app.secret_key = "demo-secret-key"

# Hardcoded credentials so the login flow is testable without a database.
# (A real security review would also flag hardcoded creds - handy for later.)
VALID_USERNAME = "admin"
VALID_PASSWORD = "password123"

# Inline HTML so we do not need a templates/ folder for the demo.
# Element ids (login-btn, error, welcome) give Selenium stable hooks to target.
LOGIN_PAGE = """
<!doctype html>
<title>Login</title>
<h1>Login</h1>
{% if error %}<p id="error" style="color:red">{{ error }}</p>{% endif %}
<form method="post">
  <input name="username" placeholder="username" />
  <input name="password" type="password" placeholder="password" />
  <button type="submit" id="login-btn">Log in</button>
</form>
"""

HOME_PAGE = """
<!doctype html>
<title>Home</title>
<h1 id="welcome">Welcome, {{ user }}!</h1>
<a href="/logout" id="logout-link">Log out</a>
"""


@app.route("/", methods=["GET"])
def home():
    """Home route - shows a welcome message if logged in, else go to login."""
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    return render_template_string(HOME_PAGE, user=user)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login route with a hardcoded user check (demo only)."""
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == VALID_USERNAME and password == VALID_PASSWORD:
            session["user"] = username
            return redirect(url_for("home"))
        error = "Invalid credentials"
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    """Clear the session and return to the login page."""
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/crash")
def crash():
    """
    Intentional error endpoint used by the security test suite.

    A nonexistent route only returns a 404, which never reveals the debugger.
    Hitting a route that RAISES is what surfaces the flaw: with debug=True (our
    planted misconfig) the response is the interactive Werkzeug debugger (a
    remote-code-execution risk); with debug=False it is a plain 500 page.
    """
    raise RuntimeError("Intentional crash to surface debug-mode exposure")


if __name__ == "__main__":
    # host=0.0.0.0 is legitimate here so the EC2-hosted app is reachable for tests.
    app.run(host="0.0.0.0", port=5000, debug=False)
