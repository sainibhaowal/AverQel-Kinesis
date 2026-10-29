# 26. Browser Automation

DeepSpace can drive a real browser (Playwright) as an external process with full tool bindings. This is the "real browser" alternative to the embedded browser panel.

## Use case

- The user wants the agent to interact with websites that require JavaScript execution, complex authentication flows, or dynamic content
- The agent needs to perform actions the embedded browser panel cannot: file uploads, complex drag-and-drop, canvas/WebGL interaction, multiple tabs
- A deployment wants full browser automation without embedding a browser in the UI

## Why it matters

- The embedded browser panel is limited to CDP over a single tab in the same process
- Real browser automation runs Playwright in a separate process with full browser capabilities
- The agent gets 16 dedicated tools: navigate, click, type, fill, select, hover, wait, screenshot, evaluate, get_content, pdf, new_tab, switch_tab, close_tab, download, drag_drop
- Runs in a separate external process; browser isolation and network policy remain deployment responsibilities

## How it works

### Architecture

1. **Configuration**: `deepspace_browser_automation_enabled=true` plus optional settings (browser type, viewport, timeouts, proxy, etc.)
2. **Runtime**: `BrowserAutomationRuntime` launches Playwright via CLI with CDP enabled on a random port
3. **Tools**: 16 native tools registered under the `browser_` prefix, all using CDP over WebSocket
4. **Lifecycle**: One browser session per conversation; started on first tool use, stopped on conversation end or explicit close

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_browser_automation_enabled` | bool | `true` | Master switch |
| `deepspace_browser_automation_playwright_executable` | str | `"playwright"` | Playwright CLI path |
| `deepspace_browser_automation_browser_type` | enum | `"chromium"` | Browser: chromium/firefox/webkit |
| `deepspace_browser_automation_headless` | bool | `true` | Headless mode |
| `deepspace_browser_automation_viewport_width` | int | `1280` | Viewport width |
| `deepspace_browser_automation_viewport_height` | int | `720` | Viewport height |
| `deepspace_browser_automation_default_timeout_ms` | int | `30000` | Default timeout |
| `deepspace_browser_automation_max_timeout_ms` | int | `300000` | Max allowed timeout |
| `deepspace_browser_automation_screenshot_on_error` | bool | `true` | Auto-screenshot on failure |
| `deepspace_browser_automation_record_video` | bool | `false` | Record video of session |
| `deepspace_browser_automation_downloads_dir` | str | `null` | Download directory |
| `deepspace_browser_automation_proxy` | dict | `null` | Proxy configuration |

### Tool reference

| Tool | Mode | Description |
|------|------|-------------|
| `browser_navigate` | write | Navigate to URL with wait condition |
| `browser_click` | write | Click element by selector |
| `browser_type` | write | Type text into element |
| `browser_fill` | write | Fill input field |
| `browser_select` | write | Select dropdown options |
| `browser_hover` | read | Hover over element |
| `browser_wait` | read | Wait for navigation or element |
| `browser_screenshot` | read | Capture screenshot (full page or element) |
| `browser_evaluate` | read | Execute JavaScript in page |
| `browser_get_content` | read | Get page HTML/text/JSON |
| `browser_pdf` | read | Generate PDF of page |
| `browser_new_tab` | write | Open new tab |
| `browser_switch_tab` | write | Switch to tab by index |
| `browser_close_tab` | write | Close current tab |
| `browser_download` | write | Trigger file download |
| `browser_drag_drop` | write | Drag and drop between elements |

### Example usage

```python
# Agent uses the tools naturally
await browser_navigate(url="https://github.com", wait_until="networkidle")
await browser_click(selector="a[href='/login']")
await browser_fill(selector="input[name='login']", value="myuser")
await browser_fill(selector="input[name='password']", value="mypass")
await browser_click(selector="input[type='submit']")
screenshot = await browser_screenshot(full_page=True)
```

## What to watch for

- **External dependency**: Requires Playwright and browsers installed on the host (`playwright install chromium`)
- **Process isolation**: Browser runs in separate process; CDP WebSocket is the only communication channel
- **Resource usage**: Each session launches a full browser; monitor memory/CPU
- **Sandbox**: The browser process runs outside the agent sandbox; configure proxy/network restrictions at host level
- **Not a replacement**: The embedded browser panel remains for simple fetch/read tasks; automation is for complex interactions
- **Video recording**: Generates large files; enable only for debugging
- **Proxy support**: Configure via `deepspace_browser_automation_proxy` setting
- **Video/Downloads**: Require writable directories; configure via settings
