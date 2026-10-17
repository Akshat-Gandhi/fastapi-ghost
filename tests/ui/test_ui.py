"""Browser tests (Playwright) for the Ghost web UI against a real server and the todo demo app.

Run:  pytest tests/ui          (needs: pip install playwright && playwright install chromium)
Set GHOST_SHOT_DIR to keep screenshots.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(os.environ["GHOST_SHOT_DIR"]) if os.environ.get("GHOST_SHOT_DIR") else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(cmd: list[str], env: dict, port: int, path: str) -> subprocess.Popen:
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}{path}", timeout=0.5)
            return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"server on {port} did not start")


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ghost")
    todo_port, ghost_port = free_port(), free_port()
    base = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"}
    todo = start(
        [sys.executable, "-m", "uvicorn", "examples.todo_app.main:app", "--port", str(todo_port), "--log-level", "critical"],
        {**base, "TODO_TOKEN": "ui-token"}, todo_port, "/health",
    )
    ghost = start(
        [sys.executable, "-c", f"from ghost.cli import app; app()", "serve", "--port", str(ghost_port)],
        {**base, "GHOST_DATABASE_URL": f"sqlite:///{tmp}/ui.db", "GHOST_HOME": str(tmp / "ws"),
         "GHOST_DATA_DIR": str(tmp), "GHOST_ALLOW_SIGNUP": "1"}, ghost_port, "/healthz",
    )
    yield {"ghost": f"http://127.0.0.1:{ghost_port}", "todo": f"http://127.0.0.1:{todo_port}"}
    for p in (ghost, todo):
        p.terminate()
        p.wait(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def seeded(servers):
    """A user with one finished run, created over HTTP so UI tests do not depend on each other."""
    ghost = servers["ghost"]
    with httpx.Client(base_url=ghost, follow_redirects=False, timeout=30) as c:
        c.post("/register", data={"email": "seed@x.io", "password": "password123"})
        r = c.post("/projects", data={"name": "Seeded", "base_url": servers["todo"], "samples": "5",
                                      "headers": "Authorization: Bearer ui-token", "allow_writes": "1"})
        project_id = int(r.headers["location"].rsplit("/", 1)[1])
        run_id = int(c.post(f"/projects/{project_id}/run").headers["location"].rsplit("/", 1)[1])
        for _ in range(90):
            if c.get(f"/runs/{run_id}/report.json").json()["status"] != "running":
                break
            time.sleep(1)
    return {"email": "seed@x.io", "password": "password123", "project_id": project_id, "run_id": run_id}


def log_in(page, base, user):
    page.goto(f"{base}/login")
    page.get_by_label("Email").fill(user["email"])
    page.get_by_label("Password").fill(user["password"])
    page.get_by_role("button", name="Sign in").click()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    ctx.close()


def shot(page, name):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def sign_up(page, base, email, pw="password123"):
    page.goto(f"{base}/register")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password (8+ characters)").fill(pw)
    page.get_by_role("button", name="Create account").click()


def test_anonymous_is_sent_to_login_and_bad_login_is_explained(servers, page):
    base = servers["ghost"]
    page.goto(f"{base}/projects")
    expect(page).to_have_url(f"{base}/login")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    page.get_by_label("Email").fill("nobody@x.io")
    page.get_by_label("Password").fill("wrong-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_text("Wrong email/username or password.")).to_be_visible()
    shot(page, "01-login-error")
    assert page.errors == [] or all("401" in e for e in page.errors)  # the 401 resource log is expected


def test_register_validation(servers, page):
    base = servers["ghost"]
    page.goto(f"{base}/register")
    page.get_by_label("Email").fill("short@x.io")
    page.get_by_label("Password (8+ characters)").fill("abc")
    page.get_by_role("button", name="Create account").click()
    expect(page).to_have_url(f"{base}/register")  # blocked by minlength in the browser


def test_full_user_journey(servers, page):
    ghost, todo = servers["ghost"], servers["todo"]

    # --- sign up lands on the (empty) projects page
    sign_up(page, ghost, "ui-user@x.io")
    expect(page).to_have_url(f"{ghost}/projects")
    expect(page.get_by_text("No projects yet")).to_be_visible()
    shot(page, "02-projects-empty")

    # --- bad URL is rejected with a message
    page.get_by_label("Name", exact=True).fill("bad")
    page.get_by_label("Server URL").fill("localhost:8000")
    page.get_by_role("button", name="Enroll project").click()
    expect(page.get_by_text("URL must start with http:// or https://")).to_be_visible()

    # --- enroll the todo app with an auth header and writes enabled
    page.get_by_label("Name", exact=True).fill("Todo demo")
    page.get_by_label("Server URL").fill(todo)
    page.get_by_label("Headers for authentication").fill("Authorization: Bearer ui-token")
    page.get_by_label("Requests per endpoint").fill("5")
    page.get_by_label("Also call POST").check()
    page.get_by_role("button", name="Enroll project").click()
    expect(page.get_by_role("heading", name="Todo demo")).to_be_visible()
    body = page.inner_text("body")
    assert "Authorization" in body and "ui-token" not in body  # names shown, values never
    shot(page, "03-project")

    # --- run it and wait for the report
    page.get_by_role("button", name="Run now").click()
    expect(page).to_have_url(re.compile(rf"{re.escape(ghost)}/runs/\d+$"))
    run_id = page.url.rsplit("/", 1)[1]
    expect(page.locator("h2")).to_contain_text("done", timeout=90_000)
    expect(page.get_by_text("never blocks anything")).to_be_visible()
    shot(page, "04-report")

    # summary + table
    expect(page.get_by_text("endpoints with findings")).to_be_visible()
    row = page.locator("tr", has_text="/calc/average")
    expect(row).to_contain_text("Server error")
    expect(row).to_contain_text("500")
    expect(page.locator("tr", has_text="/slow")).to_contain_text("Slow")
    expect(page.locator("tr", has_text="GET /health")).not_to_contain_text("Server error")
    expect(page.get_by_text("What do these labels mean?")).to_be_visible()
    assert "5XX" not in page.locator("table").inner_text()  # codes stay in the JSON, not in the UI

    page.get_by_text("What do these labels mean?").click()
    expect(page.get_by_text("crashed or failed internally")).to_be_visible()
    shot(page, "04b-legend")
    page.get_by_text("What do these labels mean?").click()

    # per-API detail shows what the client sees, and never the token
    detail = page.locator("details", has_text="GET /todos/{todo_id}")
    detail.locator("summary").click()
    expect(detail).to_contain_text("Todo not found")
    expect(detail).to_contain_text("What the client sees on errors")
    assert "ui-token" not in page.content()
    shot(page, "05-report-detail")

    # downloads
    md = page.request.get(f"{ghost}/runs/{run_id}/report.md")
    assert md.ok and "# API report: Todo demo" in md.text() and "ui-token" not in md.text()
    js = page.request.get(f"{ghost}/runs/{run_id}/report.json").json()
    assert js["status"] == "done" and js["summary"]["endpoints"] == 9

    # navigation back to the project lists the run
    page.get_by_role("link", name="Todo demo").first.click()
    expect(page.get_by_role("link", name=f"Run #{run_id}")).to_be_visible()

    # a second run keeps working and reports no drift on an unchanged API
    page.get_by_role("button", name="Run now").click()
    expect(page.locator("h2")).to_contain_text("done", timeout=90_000)
    expect(page.get_by_text("0 spec changes since last run")).to_be_visible()

    # --- sign out
    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(f"{ghost}/login")
    page.goto(f"{ghost}/runs/{run_id}")
    expect(page).to_have_url(f"{ghost}/login")


def test_other_users_cannot_see_projects_or_runs(servers, browser, seeded):
    ghost = servers["ghost"]
    ctx = browser.new_context()
    pg = ctx.new_page()
    sign_up(pg, ghost, "intruder@x.io")
    expect(pg).to_have_url(f"{ghost}/projects")
    expect(pg.get_by_text("No projects yet")).to_be_visible()
    assert pg.request.get(f"{ghost}/projects/{seeded['project_id']}").status == 404
    assert pg.request.get(f"{ghost}/runs/{seeded['run_id']}/report.json").status == 404
    assert pg.request.get(f"{ghost}/runs/{seeded['run_id']}/report.md").status == 404
    ctx.close()


def test_mobile_layout_has_no_horizontal_scroll(servers, browser, seeded):
    ghost = servers["ghost"]
    ctx = browser.new_context(viewport={"width": 390, "height": 800})
    pg = ctx.new_page()
    log_in(pg, ghost, seeded)
    for url in ("/projects", f"/projects/{seeded['project_id']}", f"/runs/{seeded['run_id']}"):
        pg.goto(ghost + url)
        overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"{url} scrolls horizontally by {overflow}px"
    shot(pg, "06-mobile-report")
    ctx.close()


def test_dark_mode_changes_palette(servers, browser):
    ghost = servers["ghost"]
    colors = {}
    for scheme in ("light", "dark"):
        ctx = browser.new_context(color_scheme=scheme)
        pg = ctx.new_page()
        pg.goto(f"{ghost}/login")
        colors[scheme] = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        if scheme == "dark":
            shot(pg, "07-dark-login")
        ctx.close()
    assert colors["light"] != colors["dark"]


def test_form_labels_are_accessible(servers, browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(f"{servers['ghost']}/login")
    for el in pg.locator("input:not([type=hidden])").all():
        assert el.get_attribute("id"), "input without id cannot be labelled"
        assert pg.locator(f"label[for='{el.get_attribute('id')}']").count() == 1
    assert pg.locator("html").get_attribute("lang") == "en"
    ctx.close()


_LUMINANCE_JS = """
(sel) => {
  const parse = (c) => c.match(/[\\d.]+/g).slice(0, 4).map(Number);
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const el = document.querySelector(sel);
  let bg = null, node = el;
  while (node && !bg) { const c = parse(getComputedStyle(node).backgroundColor); if (c[3] === undefined || c[3] > 0) bg = c; node = node.parentElement; }
  const fg = parse(getComputedStyle(el).color);
  const [l1, l2] = [lum(fg), lum(bg || [255, 255, 255])].sort((a, b) => b - a);
  return (l1 + 0.05) / (l2 + 0.05);
}
"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_contrast_meets_wcag_aa(servers, browser, scheme):
    ctx = browser.new_context(color_scheme=scheme)
    pg = ctx.new_page()
    pg.goto(f"{servers['ghost']}/login")
    checks = {"body text": "h2", "link": "a[href='/register']", "button": "button[type=submit]",
              "muted text": "p.muted", "input text": "input#email"}
    for name, sel in checks.items():
        ratio = pg.evaluate(_LUMINANCE_JS, sel)
        assert ratio >= 4.5, f"{scheme} {name} contrast {ratio:.2f} < 4.5"
    ctx.close()


def test_mobile_report_keeps_flags_visible(servers, browser, seeded):
    ghost = servers["ghost"]
    ctx = browser.new_context(viewport={"width": 390, "height": 800})
    pg = ctx.new_page()
    log_in(pg, ghost, seeded)
    pg.goto(f"{ghost}/runs/{seeded['run_id']}")
    flag = pg.locator("td .flag").first
    box = flag.bounding_box()
    assert box and box["x"] + box["width"] <= 390, "flags are cut off on a phone-width screen"
    ctx.close()


def test_keyboard_focus_is_visible(servers, browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(f"{servers['ghost']}/login")
    pg.keyboard.press("Tab")  # first focus lands on the brand link
    pg.get_by_role("button", name="Sign in").focus()
    outline = pg.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline != "none"
    ctx.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_logged_in_pages_contrast(servers, browser, seeded, scheme):
    ctx = browser.new_context(color_scheme=scheme)
    pg = ctx.new_page()
    log_in(pg, servers["ghost"], seeded)
    pg.goto(f"{servers['ghost']}/projects/{seeded['project_id']}")
    for name, sel in {"delete button": "button.danger", "run button": "form[action$='/run'] button",
                      "sign out": "header button.link", "back link": "a[href='/projects']"}.items():
        assert (ratio := pg.evaluate(_LUMINANCE_JS, sel)) >= 4.5, f"{scheme} {name}: {ratio:.2f}"
    pg.goto(f"{servers['ghost']}/runs/{seeded['run_id']}")
    for name, sel in {"flag 5XX": ".flag.bad", "flag SLOW": ".flag.warn", "muted": "p.muted", "table text": "td.code"}.items():
        assert (ratio := pg.evaluate(_LUMINANCE_JS, sel)) >= 4.5, f"{scheme} {name}: {ratio:.2f}"
    if scheme == "dark":
        shot(pg, "08-dark-report")
    ctx.close()


def test_default_admin_is_forced_to_change_password(servers, browser):
    ghost = servers["ghost"]
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(f"{ghost}/login")
    pg.get_by_label("Email or username").fill("admin")
    pg.get_by_label("Password").fill("admin")
    pg.get_by_role("button", name="Sign in").click()
    expect(pg).to_have_url(f"{ghost}/change-password")
    expect(pg.get_by_text("You are using a default password")).to_be_visible()
    shot(pg, "09-forced-change-password")

    pg.goto(f"{ghost}/projects")  # cannot skip the change
    expect(pg).to_have_url(f"{ghost}/change-password")

    pg.get_by_label("Current password").fill("admin")
    pg.get_by_label("New password (8+ characters)").fill("password")  # long enough for the browser, still a default
    pg.get_by_label("Confirm new password").fill("password")
    pg.get_by_role("button", name="Change password").click()
    expect(pg).to_have_url(f"{ghost}/change-password")
    expect(pg.get_by_role("alert")).to_contain_text("not a default")

    pg.get_by_label("Current password").fill("admin")
    pg.get_by_label("New password (8+ characters)").fill("a-much-better-pass")
    pg.get_by_label("Confirm new password").fill("a-much-better-pass")
    pg.get_by_role("button", name="Change password").click()
    expect(pg).to_have_url(f"{ghost}/projects")
    expect(pg.get_by_role("link", name="Change password")).to_be_visible()  # available any time afterwards

    # --- admin manages users: add one, reset their password, they must choose their own
    pg.get_by_role("link", name="Users").click()
    expect(pg).to_have_url(f"{ghost}/admin/users")
    pg.get_by_label("Email or username").fill("sam")
    pg.get_by_label("Initial password (8+ characters)").fill("initial-pass-1")
    pg.get_by_role("button", name="Add user").click()
    expect(pg.get_by_role("status")).to_contain_text("Created sam")
    pg.locator("tr", has_text="sam").get_by_role("button", name="Reset password").click()
    notice = pg.get_by_role("status")
    expect(notice).to_contain_text("Temporary password for sam:")
    temp = notice.inner_text().split("Temporary password for sam:")[1].split()[0]
    shot(pg, "10-admin-users")
    pg.get_by_role("button", name="Sign out").click()
    pg.get_by_label("Email or username").fill("sam")
    pg.get_by_label("Password").fill(temp)
    pg.get_by_role("button", name="Sign in").click()
    expect(pg).to_have_url(f"{ghost}/change-password")
    pg.get_by_label("Current password").fill(temp)
    pg.get_by_label("New password (8+ characters)").fill("sams-own-password-1")
    pg.get_by_label("Confirm new password").fill("sams-own-password-1")
    pg.get_by_role("button", name="Change password").click()
    expect(pg).to_have_url(f"{ghost}/projects")
    assert pg.request.get(f"{ghost}/admin/users").status == 404  # sam is not an admin
    expect(pg.get_by_role("link", name="Users")).to_have_count(0)
    pg.get_by_role("button", name="Sign out").click()
    pg.get_by_label("Email or username").fill("admin")
    pg.get_by_label("Password").fill("a-much-better-pass")
    pg.get_by_role("button", name="Sign in").click()

    pg.get_by_role("button", name="Sign out").click()
    pg.get_by_label("Email or username").fill("admin")
    pg.get_by_label("Password").fill("admin")
    pg.get_by_role("button", name="Sign in").click()
    expect(pg.get_by_text("Wrong email/username or password.")).to_be_visible()
    ctx.close()
