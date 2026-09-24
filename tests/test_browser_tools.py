import http.server
import threading

import pytest

from browser_tools import browser_manager

HOME_HTML = """<!doctype html>
<html>
  <head><title>Jarvis Test Home</title></head>
  <body>
    <h1>Hello Jarvis</h1>
    <p>The quick brown fox jumps over the lazy dog.</p>
    <a href="/other" id="other-link">Go to other page</a>
    <form id="greet-form" onsubmit="event.preventDefault(); document.getElementById('result').textContent = 'Hello ' + document.getElementById('name').value;">
      <input id="name" name="name" placeholder="Your name">
      <button id="greet-btn" type="submit">Greet</button>
    </form>
    <p id="result"></p>
    <button id="noop-btn" onclick="alert('Hi there')">Ignored</button>
  </body>
</html>
"""

OTHER_HTML = """<!doctype html>
<html>
  <head><title>Jarvis Test Other</title></head>
  <body>
    <h1>Another page</h1>
    <p>Unique content only found on other.</p>
  </body>
</html>
"""


class _TestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/other"):
            body = OTHER_HTML.encode()
        else:
            body = HOME_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server_url() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
async def started_browser():
    await browser_manager.aclose()
    await browser_manager.ensure_started()
    yield browser_manager
    await browser_manager.aclose()
    browser_manager.reset()


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_open_returns_title_url_summary(server_url, started_browser):
    result = await started_browser.open(f"{server_url}/")

    assert result["title"] == "Jarvis Test Home"
    assert result["url"].endswith("/")
    assert "quick brown fox" in result["summary"]
    assert "Hello Jarvis" in result["summary"]


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_open_follows_links_and_history(server_url, started_browser):
    await started_browser.open(f"{server_url}/")
    await started_browser.click("#other-link")
    assert (await started_browser.get_text()) is not None

    await started_browser.go_back()
    assert (await started_browser.current_page_info())["title"] == "Jarvis Test Home"

    await started_browser.go_forward()
    assert (await started_browser.current_page_info())["title"] == "Jarvis Test Other"


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_get_links(server_url, started_browser):
    await started_browser.open(f"{server_url}/")
    links = await started_browser.get_links()

    assert any(link["text"] == "Go to other page" for link in links)
    assert any(link["href"].endswith("/other") for link in links)


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_fill_and_click_form(server_url, started_browser):
    await started_browser.open(f"{server_url}/")

    await started_browser.fill("#name", "Tony")
    await started_browser.click("#greet-btn")

    text = await started_browser.get_text()
    assert "Hello Tony" in text


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_find_text(server_url, started_browser):
    await started_browser.open(f"{server_url}/")
    result = await started_browser.find_text("lazy dog")

    assert len(result["matches"]) == 1
    assert "lazy dog" in result["matches"][0]["context"]


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_tabs_new_switch_close(server_url, started_browser):
    await started_browser.open(f"{server_url}/")
    await started_browser.tabs(action="new", url=f"{server_url}/other")
    assert len(await started_browser.tabs(action="list")) == 2

    await started_browser.tabs(action="switch", index=0)
    assert (await started_browser.current_page_info())["title"] == "Jarvis Test Home"

    await started_browser.tabs(action="close", index=1)
    assert len(await started_browser.tabs(action="list")) == 1


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_press_key_submits_form(server_url, started_browser):
    await started_browser.open(f"{server_url}/")

    await started_browser.fill("#name", "Pepper")
    await started_browser.press_key("Enter")

    text = await started_browser.get_text()
    assert "Hello Pepper" in text


@pytest.mark.skipif(
    not browser_manager.is_installed(),
    reason="Playwright chromium is not installed",
)
async def test_screenshot_creates_file(server_url, started_browser, tmp_path):
    await started_browser.open(f"{server_url}/")
    path = await started_browser.screenshot(name="test_page", output_dir=str(tmp_path))

    assert path.exists()
    assert path.suffix == ".png"
