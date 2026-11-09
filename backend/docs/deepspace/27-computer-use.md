# 27. Computer-Use Provider

DeepSpace can observe and operate a desktop through computer-use tools. The
Linux desktop app uses the local X11 or XWayland display. A separate virtual
Xvfb/VNC driver is also available when configured.

## Use case

- The agent needs to interact with desktop applications that have no API or web interface
- Legacy GUI applications, installers, system configuration tools
- Cross-application workflows (copy from browser → paste into terminal → run in IDE)
- Testing desktop applications end-to-end

## Why it matters

- A remote backend has no desktop access unless an operator explicitly configures a driver
- The local Linux desktop driver can observe and operate the user's X display
- The agent gets 12 tools: screenshot, click, double_click, right_click, drag, scroll, type, key, hotkey, wait, find_image, ocr
- Runs in a virtual display; the host OS is never directly exposed

## How it works

### Architecture

1. **Configuration**: `deepspace_computer_use_enabled` and `deepspace_computer_use_provider` select whether to offer computer-use tools and which driver to use.
2. **Runtime**: `local` drives the host display with `xdotool` and a screenshot utility. `vnc` starts Xvfb and x11vnc on a private virtual display.
3. **Tools**: 12 native tools observe and control the selected display.
4. **Availability**: `/api/v1/deepspace/sandbox/runtime` reports the selected provider, availability, and missing prerequisites. DeepSpace omits the tool schemas when the feature is disabled or unavailable.
5. **Lifecycle**: A process-wide runtime starts on first use and stops during backend shutdown.

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_computer_use_enabled` | bool | `false` on server; `true` on Linux desktop | Master switch when the host driver can be used |
| `deepspace_computer_use_provider` | enum | `"local"` on Linux | Provider type: local/vnc/cua/screenshot_vnc/screenshot_rdp |
| `deepspace_computer_use_vnc_host` | str | `"localhost"` | VNC host |
| `deepspace_computer_use_vnc_port` | int | `5900` | VNC port |
| `deepspace_computer_use_vnc_password` | str | `null` | VNC password |
| `deepspace_computer_use_display_width` | int | `1920` | Virtual display width |
| `deepspace_computer_use_display_height` | int | `1080` | Virtual display height |
| `deepspace_computer_use_screenshot_interval_ms` | int | `500` | Screenshot poll interval |
| `deepspace_computer_use_action_timeout_ms` | int | `30000` | Action timeout |

### Tool reference

| Tool | Mode | Description |
|------|------|-------------|
| `computer_screenshot` | read | Capture desktop screenshot (PNG/JPEG) |
| `computer_click` | write | Click at coordinates |
| `computer_double_click` | write | Double-click at coordinates |
| `computer_right_click` | write | Right-click at coordinates |
| `computer_drag` | write | Drag from (x1,y1) to (x2,y2) |
| `computer_scroll` | write | Scroll at coordinates |
| `computer_type` | write | Type text string |
| `computer_key` | write | Press single key |
| `computer_hotkey` | write | Press key combination |
| `computer_wait` | read | Wait for time or condition |
| `computer_find_image` | read | Template matching (requires OpenCV) |
| `computer_ocr` | read | OCR text from region (requires Tesseract) |

### Example usage

```python
# Agent uses the tools
screenshot = await computer_screenshot(format="png")
await computer_click(x=100, y=200)
await computer_type(text="hello world")
await computer_hotkey(keys=["ctrl", "c"])
await computer_wait(seconds=2.0)
```

## What to watch for

- **Host display dependencies**: `local` requires `DISPLAY`, `xdotool`, and `scrot` or ImageMagick `import`. Linux `.deb` and `.rpm` installers include `xdotool` and `scrot`; AppImage users need them installed on the host.
- **Display support**: The local driver controls X11/XWayland windows. It cannot automate Wayland-native windows that are not exposed through XWayland. Without a usable host display, the optional `vnc` provider requires Xvfb and x11vnc.
- **Security**: Local computer actions affect the signed-in OS user's visible desktop. The local driver does not elevate privileges. Ordinary clicks and typing are not blanket-prompted in Full access; recognizable risky text and system-level key combinations require confirmation. Coordinate-only clicks do not include enough target information for the backend to determine what is on screen, so this check cannot identify every dangerous button.
- **Performance**: Screenshot capture + VNC roundtrip adds latency (~100-500ms per action)
- **Image matching**: `find_image` requires OpenCV (`pip install opencv-python`)
- **OCR**: Requires Tesseract (`apt install tesseract-ocr` + `pip install pytesseract`)
- **Not a CUA driver**: This is a basic VNC automation layer; true CUA (Computer Use Agent) requires a model trained for computer use
- **Availability**: The enabled flag alone does not install a display server or input tools. The runtime status reports missing prerequisites, and the model does not receive unusable computer tool schemas.
- **Recording**: `record_session` saves screenshots + actions to disk for replay/debugging
- **Confirmation**: Approval checks still apply when tool arguments identify a higher-risk action. Do not treat coordinate-only UI automation as a reliable way to identify destructive screen controls.
