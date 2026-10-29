# 27. Computer-Use Provider

DeepSpace can drive a desktop environment (VNC/Xvfb) for computer-use automation: observe (screenshot), act (click, type, drag, scroll), and verify. The feature flag defaults to enabled, but the runtime still requires a reachable VNC/Xvfb environment and its host dependencies.

## Use case

- The agent needs to interact with desktop applications that have no API or web interface
- Legacy GUI applications, installers, system configuration tools
- Cross-application workflows (copy from browser → paste into terminal → run in IDE)
- Testing desktop applications end-to-end

## Why it matters

- DeepSpace runs server-side with no desktop access by default
- This provider bridges that gap via a virtual display (Xvfb) + VNC server (x11vnc)
- The agent gets 12 tools: screenshot, click, double_click, right_click, drag, scroll, type, key, hotkey, wait, find_image, ocr
- Runs in a virtual display; the host OS is never directly exposed

## How it works

### Architecture

1. **Configuration**: `deepspace_computer_use_enabled=true` plus VNC settings
2. **Runtime**: `ComputerUseRuntime` starts Xvfb + x11vnc on a virtual display
3. **Client**: `VNCClient` implements minimal RFB protocol for actions
4. **Tools**: 12 native tools registered under the `computer_` prefix
5. **Lifecycle**: One session per conversation; virtual display created on start, destroyed on stop

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_computer_use_enabled` | bool | `true` | Master switch |
| `deepspace_computer_use_provider` | enum | `"screenshot_vnc"` | Provider type: cua/screenshot_vnc/screenshot_rdp/local |
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

- **Heavy dependencies**: Requires Xvfb, x11vnc, (optional) OpenCV, Tesseract, pycryptodome
- **Virtual display only**: No access to host's real display; runs in isolated Xvfb
- **Security**: VNC server binds to localhost by default; password optional but recommended
- **Performance**: Screenshot capture + VNC roundtrip adds latency (~100-500ms per action)
- **Image matching**: `find_image` requires OpenCV (`pip install opencv-python`)
- **OCR**: Requires Tesseract (`apt install tesseract-ocr` + `pip install pytesseract`)
- **Not a CUA driver**: This is a basic VNC automation layer; true CUA (Computer Use Agent) requires a model trained for computer use
- **Availability**: The enabled flag does not create Xvfb or x11vnc. A host without those services returns an unavailable-provider error until the runtime is deployed.
- **Recording**: `record_session` saves screenshots + actions to disk for replay/debugging
- **Not for production**: Designed for development/testing; not hardened for untrusted code execution
