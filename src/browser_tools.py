import asyncio
import logging
import os
import re
from contextlib import suppress
from pathlib import Path

from livekit.agents import RunContext, ToolError, function_tool
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
MAX_TEXT_CHARS = 2_000
MAX_SUMMARY_CHARS = 2_000
MAX_LINKS = 50
MAX_INPUTS = 30
MAX_MATCHES = 5


def _collapse_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class BrowserManager:
    """Owns a single persistent headless Chromium for the agent's lifetime.

    The browser is started lazily on first use and shared across every browser
    tool call, so navigation state (tabs, current page) survives between turns.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._pages: list = []
        self._current_index = 0

    @staticmethod
    def is_installed() -> bool:
        env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        candidates: list[Path] = []
        if env:
            candidates.append(Path(env))
        elif os.name == "nt":
            candidates.append(
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
            )
        else:
            candidates.append(Path.home() / ".cache" / "ms-playwright")

        for base in candidates:
            try:
                entries = os.listdir(base)
            except OSError:
                continue
            if any(re.fullmatch(r"chromium-\d+", entry) for entry in entries):
                return True
        return False

    async def _handle_dialog(self, dialog) -> None:
        # Auto-accept alerts/confirms/prompts so they never block a turn.
        await dialog.accept()

    async def ensure_started(self) -> None:
        if self._playwright is not None:
            if self._browser is not None and self._browser.is_connected():
                return
            await self.aclose()

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=os.environ.get("JARVIS_BROWSER_HEADFUL", "0") != "1",
            )
            self._context = await self._browser.new_context(ignore_https_errors=True)
            self._context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            page = await self._context.new_page()
            page.on("dialog", self._handle_dialog)
            self._pages = [page]
            self._current_index = 0
        except Exception as e:
            await self.aclose()
            raise ToolError(f"Failed to start the browser: {e}") from e

    def reset(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._pages = []
        self._current_index = 0

    async def aclose(self) -> None:
        if self._context is not None:
            with suppress(Exception):
                await self._context.close()
        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
        self.reset()

    def _current_page(self):
        if not self._pages:
            raise ToolError(
                "No browser tab is open yet. Open a page with browser_open first."
            )
        return self._pages[self._current_index]

    async def _settle(self, page) -> None:
        with suppress(PlaywrightTimeoutError, PlaywrightError):
            await page.wait_for_load_state("networkidle", timeout=5_000)

    async def open(self, url: str) -> dict:
        url = url.strip()
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = f"https://{url}"
        await self.ensure_started()
        page = self._current_page()
        try:
            await page.goto(url, wait_until="load", timeout=DEFAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise ToolError(f"The page {url} took too long to load.") from None
        except PlaywrightError as e:
            raise ToolError(f"Could not open {url}: {e}") from e
        await self._settle(page)
        return await self.current_page_info(include_summary=True)

    async def current_page_info(self, include_summary: bool = False) -> dict:
        page = self._current_page()
        info = {"title": await page.title(), "url": page.url}
        if include_summary:
            info["summary"] = await self.get_text(MAX_SUMMARY_CHARS)
        return info

    async def get_text(self, max_chars: int = MAX_TEXT_CHARS) -> str:
        page = self._current_page()
        try:
            body = page.locator("body")
            if await body.count() == 0:
                return ""
            text = await body.inner_text()
        except PlaywrightError:
            return ""
        text = _collapse_whitespace(text)
        if len(text) > max_chars:
            text = text[:max_chars] + " ... [truncated]"
        return text

    async def get_links(self, max_links: int = MAX_LINKS) -> list[dict]:
        page = self._current_page()
        links = await page.eval_on_selector_all(
            "a[href]",
            """els => els.map(e => {
                const text = (e.innerText || e.textContent || '').trim();
                const rects = e.getClientRects();
                const visible = !!rects.length;
                return { text, href: e.href, visible };
            }).filter(l => l.text && l.href)""",
        )
        return links[:max_links]

    async def get_inputs(self, max_inputs: int = MAX_INPUTS) -> list[dict]:
        page = self._current_page()
        inputs = await page.eval_on_selector_all(
            (
                "input, textarea, select, button, [role='button'], "
                "[role='textbox'], [role='combobox'], [role='link']"
            ),
            """els => els.map(e => {
                const tag = e.tagName.toLowerCase();
                const text = (e.innerText || e.value || '').trim();
                const rects = e.getClientRects();
                const visible = !!rects.length;
                let selector = tag;
                if (e.id) selector += `#${e.id}`;
                else if (e.getAttribute('name')) selector += `[name="${e.getAttribute('name')}"]`;
                else if (text) selector += `:text-is("${text}")`;
                return {
                    tag,
                    type: e.getAttribute('type') || '',
                    id: e.id || '',
                    name: e.getAttribute('name') || '',
                    placeholder: e.getAttribute('placeholder') || '',
                    text,
                    selector,
                    visible,
                };
            }).filter(i => i.visible)""",
        )
        return inputs[:max_inputs]

    async def find_text(self, query: str, max_matches: int = MAX_MATCHES) -> dict:
        text = await self.get_text(max_chars=200_000)
        matches: list[dict] = []
        lowered = text.lower()
        needle = query.lower()
        start = 0
        while len(matches) < max_matches:
            idx = lowered.find(needle, start)
            if idx == -1:
                break
            window = text[max(0, idx - 80) : idx + len(query) + 80]
            matches.append({"context": _collapse_whitespace(window)})
            start = idx + len(query)
        return {"query": query, "count": len(matches), "matches": matches}

    async def click(self, selector: str) -> str:
        page = self._current_page()
        locator = page.locator(selector)
        try:
            await locator.first.click(timeout=5_000)
            await self._settle(page)
        except PlaywrightError as e:
            raise ToolError(f"Could not click '{selector}': {e}") from e
        return f"Clicked {selector}."

    async def click_text(self, text: str) -> str:
        page = self._current_page()
        try:
            locator = page.get_by_text(text, exact=False).first
            await locator.click(timeout=5_000)
            await self._settle(page)
        except PlaywrightError as e:
            raise ToolError(f"Could not click on text '{text}': {e}") from e
        return f"Clicked on '{text}'."

    async def fill(self, selector: str, value: str) -> str:
        page = self._current_page()
        try:
            await page.locator(selector).first.fill(value, timeout=5_000)
        except PlaywrightError as e:
            raise ToolError(f"Could not fill '{selector}': {e}") from e
        return f"Filled '{selector}' with '{value}'."

    async def press_key(self, key: str) -> str:
        page = self._current_page()
        try:
            await page.keyboard.press(key)
            await self._settle(page)
        except PlaywrightError as e:
            raise ToolError(f"Could not press key '{key}': {e}") from e
        return f"Pressed '{key}'."

    async def select_option(self, selector: str, option: str) -> str:
        page = self._current_page()
        try:
            await page.locator(selector).select_option(label=option, timeout=5_000)
        except PlaywrightError as e:
            try:
                await page.locator(selector).select_option(value=option, timeout=5_000)
            except PlaywrightError:
                raise ToolError(
                    f"Could not select '{option}' in '{selector}': {e}"
                ) from e
        return f"Selected '{option}' in '{selector}'."

    async def scroll(self, direction: str) -> str:
        page = self._current_page()
        direction = direction.lower().strip()
        if direction == "bottom":
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top":
            await page.evaluate("() => window.scrollTo(0, 0)")
        elif direction == "up":
            await page.mouse.wheel(0, -800)
        elif direction == "down":
            await page.mouse.wheel(0, 800)
        else:
            raise ToolError("Direction must be one of: top, bottom, up, down.")
        return f"Scrolled {direction}."

    async def go_back(self) -> dict:
        page = self._current_page()
        try:
            await page.go_back(timeout=DEFAULT_TIMEOUT_MS)
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            raise ToolError(f"Could not go back: {e}") from e
        await self._settle(page)
        return await self.current_page_info()

    async def go_forward(self) -> dict:
        page = self._current_page()
        try:
            await page.go_forward(timeout=DEFAULT_TIMEOUT_MS)
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            raise ToolError(f"Could not go forward: {e}") from e
        await self._settle(page)
        return await self.current_page_info()

    async def reload(self) -> dict:
        page = self._current_page()
        try:
            await page.reload(timeout=DEFAULT_TIMEOUT_MS)
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            raise ToolError(f"Could not reload the page: {e}") from e
        await self._settle(page)
        return await self.current_page_info()

    async def tabs(self, action: str, index: int | None = None, url: str | None = None):
        action = action.lower()
        if action == "list":
            await self.ensure_started()
            out = []
            for i, page in enumerate(self._pages):
                out.append(
                    {
                        "index": i,
                        "title": await page.title(),
                        "url": page.url,
                        "current": i == self._current_index,
                    }
                )
            return out
        if action == "new":
            await self.ensure_started()
            page = await self._context.new_page()
            page.on("dialog", self._handle_dialog)
            self._pages.append(page)
            self._current_index = len(self._pages) - 1
            if url:
                await self.open(url)
            return await self.tabs(action="list")
        if action == "switch":
            if index is None:
                raise ToolError("Switching tabs requires an index.")
            if index < 0 or index >= len(self._pages):
                raise ToolError(f"Tab index {index} is out of range.")
            self._current_index = index
            page = self._current_page()
            return {"index": index, "title": await page.title(), "url": page.url}
        if action == "close":
            if len(self._pages) <= 1:
                raise ToolError("Cannot close the last open tab.")
            if index is None:
                index = self._current_index
            if index < 0 or index >= len(self._pages):
                raise ToolError(f"Tab index {index} is out of range.")
            page = self._pages[index]
            await page.close()
            self._pages.pop(index)
            if index < self._current_index:
                self._current_index -= 1
            elif index == self._current_index:
                self._current_index = min(index, len(self._pages) - 1)
            return await self.tabs(action="list")
        raise ToolError("Tab action must be one of: list, new, switch, close.")

    async def wait(self, milliseconds: int) -> str:
        ms = max(0, min(int(milliseconds), 60_000))
        await asyncio.sleep(ms / 1000)
        return f"Waited {ms} ms."

    async def screenshot(self, name: str, output_dir: str | None = None) -> Path:
        base = Path(
            output_dir or os.environ.get("JARVIS_BROWSER_SCREENSHOT_DIR", "screenshots")
        )
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "screenshot"
        path = base / f"{safe}.png"
        await self._current_page().screenshot(path=str(path), full_page=False)
        return path


browser_manager = BrowserManager()


@function_tool
async def browser_open(context: RunContext, url: str) -> str:
    """Open a webpage at the given URL in the browser and summarize its content. Use this when
    the user asks you to open, visit, or browse a website or URL.

    Args:
        url: The full web address to open, including the protocol, e.g. https://example.com
    """
    info = await browser_manager.open(url)
    return f"Opened {info['url']}.\nTitle: {info['title']}\nSummary:\n{info['summary']}"


@function_tool
async def browser_go(context: RunContext, action: str) -> str:
    """Navigate the current browser tab. Actions: back (previous page), forward (next page),
    or reload (refresh the current page).

    Args:
        action: One of \"back\", \"forward\", or \"reload\".
    """
    if action == "back":
        info = await browser_manager.go_back()
    elif action == "forward":
        info = await browser_manager.go_forward()
    elif action == "reload":
        info = await browser_manager.reload()
    else:
        raise ToolError('Action must be one of "back", "forward", or "reload".')
    return f"Title: {info['title']}\nURL: {info['url']}"


@function_tool
async def browser_current_page(context: RunContext) -> str:
    """Report which page is currently open in the browser (title and web address)."""
    info = await browser_manager.current_page_info()
    return f"Title: {info['title']}\nURL: {info['url']}"


@function_tool
async def browser_get_text(context: RunContext) -> str:
    """Extract the readable text of the current page, with navigation and formatting removed.
    Use this after opening or navigating a page to learn what it says.
    """
    return await browser_manager.get_text()


@function_tool
async def browser_get_links(context: RunContext) -> str:
    """List the links visible on the current page as \"link text -> web address\" lines.
    Use this to find where the user can click next.
    """
    try:
        links = await browser_manager.get_links()
    except Exception as e:
        raise ToolError(f"Could not read links: {e}") from e
    if not links:
        return "No links found on this page."
    lines = [f"{link['text']} -> {link['href']}" for link in links if link["visible"]]
    return "\n".join(lines) if lines else "No visible links found on this page."


@function_tool
async def browser_get_inputs(context: RunContext) -> str:
    """List forms, text fields, buttons, and dropdowns visible on the current page, including a
    suggested selector for each. Use this to fill in a form or find what button to press.
    """
    try:
        inputs = await browser_manager.get_inputs()
    except Exception as e:
        raise ToolError(f"Could not read the page controls: {e}") from e
    if not inputs:
        return "No input fields, buttons, or forms found on this page."
    lines = []
    for item in inputs:
        parts = [item["tag"]]
        if item["type"]:
            parts.append(f"type={item['type']}")
        if item["placeholder"]:
            parts.append(f"placeholder={item['placeholder']!r}")
        if item["text"]:
            parts.append(f"text={item['text']!r}")
        parts.append(f"selector={item['selector']!r}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


@function_tool
async def browser_find_text(context: RunContext, query: str) -> str:
    """Search the current page's text for the given phrase and return the surrounding context of
    each match. Use this to confirm whether specific content appears on the page.

    Args:
        query: The phrase to look for on the page.
    """
    try:
        result = await browser_manager.find_text(query)
    except Exception as e:
        raise ToolError(f"Could not search the page: {e}") from e
    if result["count"] == 0:
        return f"'{query}' was not found on this page."
    lines = [
        f"Match {i + 1}: ... {m['context']} ..."
        for i, m in enumerate(result["matches"])
    ]
    return f"Found {result['count']} match(es) for '{query}':\n" + "\n".join(lines)


@function_tool
async def browser_click(context: RunContext, selector: str) -> str:
    """Click an element on the current page using a CSS selector. Use the selector returned by
    browser_get_inputs or browser_get_links.

    Args:
        selector: A CSS selector identifying the element to click.
    """
    context.disallow_interruptions()
    return await browser_manager.click(selector)


@function_tool
async def browser_click_text(context: RunContext, text: str) -> str:
    """Click the first element on the current page whose visible text matches the given text.
    Use this to click a button or link when you know its label but not its selector.

    Args:
        text: The visible text of the element to click.
    """
    context.disallow_interruptions()
    return await browser_manager.click_text(text)


@function_tool
async def browser_fill(context: RunContext, selector: str, value: str) -> str:
    """Type the given value into a text field on the current page. Use the selector from
    browser_get_inputs.

    Args:
        selector: A CSS selector identifying the text field.
        value: The text to type into the field.
    """
    context.disallow_interruptions()
    return await browser_manager.fill(selector, value)


@function_tool
async def browser_press_key(context: RunContext, key: str) -> str:
    """Press a keyboard key on the current page, such as Enter to submit a form, Escape to close a
    dialog, or Tab to move focus. Common keys: Enter, Escape, Tab, ArrowDown, ArrowUp.

    Args:
        key: The name of the key to press.
    """
    context.disallow_interruptions()
    return await browser_manager.press_key(key)


@function_tool
async def browser_select(context: RunContext, selector: str, option: str) -> str:
    """Choose an option from a dropdown on the current page.

    Args:
        selector: A CSS selector identifying the dropdown.
        option: The visible label (or value) of the option to choose.
    """
    context.disallow_interruptions()
    return await browser_manager.select_option(selector, option)


@function_tool
async def browser_scroll(context: RunContext, direction: str) -> str:
    """Scroll the current page. Direction must be one of top, bottom, up, or down.

    Args:
        direction: One of \"top\", \"bottom\", \"up\", or \"down\".
    """
    context.disallow_interruptions()
    return await browser_manager.scroll(direction)


@function_tool
async def browser_tabs(
    context: RunContext,
    action: str,
    index: int | None = None,
    url: str | None = None,
) -> str:
    """Manage browser tabs. Actions:
    \"list\" lists all open tabs, \"new\" opens a new tab (optional url), \"switch\" brings the tab
    at the given index to the front, \"close\" closes the tab at the given index.

    Args:
        action: One of \"list\", \"new\", \"switch\", or \"close\".
        index: The tab index to switch to or close (0 is the first tab). Used only with \"switch\" or \"close\".
        url: URL to open in a new tab. Used only with \"new\".
    """
    try:
        result = await browser_manager.tabs(action=action, index=index, url=url)
    except Exception as e:
        raise ToolError(f"Could not manage tabs: {e}") from e
    if isinstance(result, dict):
        return f"Now on tab {result['index']}: {result['title']} ({result['url']})"
    lines = []
    for tab in result:
        marker = " (current)" if tab["current"] else ""
        lines.append(f"Tab {tab['index']}: {tab['title']} - {tab['url']}{marker}")
    return "\n".join(lines)


@function_tool
async def browser_wait(context: RunContext, milliseconds: int) -> str:
    """Wait for the given number of milliseconds. Use this to let a page finish loading or a
    scripted effect run before the next action.

    Args:
        milliseconds: How long to wait, between 0 and 60000.
    """
    return await browser_manager.wait(milliseconds)


@function_tool
async def browser_screenshot(context: RunContext, name: str) -> str:
    """Save a screenshot of the current page to a PNG file and return the file path. Use this so
    the caller can view the browser state on screen.

    Args:
        name: A short name for the screenshot file, e.g. \"login_form\".
    """
    try:
        path = await browser_manager.screenshot(name)
    except Exception as e:
        raise ToolError(f"Could not take a screenshot: {e}") from e
    return f"Screenshot saved to {path}"


BROWSER_TOOLS = [
    browser_open,
    browser_go,
    browser_current_page,
    browser_get_text,
    browser_get_links,
    browser_get_inputs,
    browser_find_text,
    browser_click,
    browser_click_text,
    browser_fill,
    browser_press_key,
    browser_select,
    browser_scroll,
    browser_tabs,
    browser_wait,
    browser_screenshot,
]
