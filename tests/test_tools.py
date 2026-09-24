from unittest.mock import MagicMock, patch

import pytest
from livekit.agents import ToolError

from browser_tools import BrowserManager
from tools import search_web


@pytest.mark.asyncio
async def test_search_web_success():
    mock_run = MagicMock(return_value="Python is a programming language.")
    with patch("tools.DuckDuckGoSearchRun") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.run = mock_run
        mock_cls.return_value = mock_instance

        # Call the underlying function of the function_tool
        result = await search_web._func(MagicMock(), query="python programming")

        assert result == "Python is a programming language."
        mock_run.assert_called_once_with(tool_input="python programming")


@pytest.mark.asyncio
async def test_search_web_failure_raises_tool_error():
    mock_run = MagicMock(side_effect=RuntimeError("Rate limit exceeded"))
    with patch("tools.DuckDuckGoSearchRun") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.run = mock_run
        mock_cls.return_value = mock_instance

        with pytest.raises(ToolError) as exc_info:
            await search_web._func(MagicMock(), query="failing query")

        assert "Failed to search the web" in str(exc_info.value)
        assert "Rate limit exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_browser_manager_url_normalization():
    bm = BrowserManager()
    captured_urls = []

    async def fake_ensure():
        pass

    async def fake_settle(page):
        pass

    async def fake_info(include_summary=False):
        return {"title": "Test", "url": captured_urls[-1]}

    class FakePage:
        async def goto(self, url, **kwargs):
            captured_urls.append(url)

    fake_page = FakePage()

    with (
        patch.object(bm, "ensure_started", side_effect=fake_ensure),
        patch.object(bm, "_current_page", return_value=fake_page),
        patch.object(bm, "_settle", side_effect=fake_settle),
        patch.object(bm, "current_page_info", side_effect=fake_info),
    ):
        # URL without scheme
        await bm.open("google.com")
        assert captured_urls[-1] == "https://google.com"

        # URL with whitespace and without scheme
        await bm.open("  example.org/search  ")
        assert captured_urls[-1] == "https://example.org/search"

        # URL with existing http scheme preserved
        await bm.open("http://localhost:8080")
        assert captured_urls[-1] == "http://localhost:8080"

        # URL with existing https scheme preserved
        await bm.open("https://github.com")
        assert captured_urls[-1] == "https://github.com"
