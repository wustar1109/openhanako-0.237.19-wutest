# Infinite-Canvas Integration

OpenHanako integrates `hero8152/Infinite-Canvas` as a first-party module. It is not an OpenHanako plugin, iframe, webview, or external website embed.

## Source Setup

This workspace vendors Infinite-Canvas at:

```bash
third_party/Infinite-Canvas
```

Source repository:

```bash
https://github.com/hero8152/Infinite-Canvas.git
```

Pinned commit:

```bash
92837f5cd34705212b115c9d46293ada8cc63808
```

In a normal git checkout, prefer a submodule:

```bash
git submodule add https://github.com/hero8152/Infinite-Canvas.git third_party/Infinite-Canvas
cd third_party/Infinite-Canvas
git checkout 92837f5cd34705212b115c9d46293ada8cc63808
```

## Python Dependencies

OpenHanako does not install Infinite-Canvas Python dependencies automatically.

macOS/Linux:

```bash
cd third_party/Infinite-Canvas
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows:

```powershell
cd third_party\Infinite-Canvas
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Runtime Data

When launched by OpenHanako, Infinite-Canvas writes runtime data under:

```bash
$HANA_HOME/infinite-canvas
```

Important subdirectories:

```bash
$HANA_HOME/infinite-canvas/data
$HANA_HOME/infinite-canvas/output
$HANA_HOME/infinite-canvas/assets
$HANA_HOME/infinite-canvas/workflows
$HANA_HOME/infinite-canvas/API
```

The source static files still come from `third_party/Infinite-Canvas/static`.

## Internal Routes

OpenHanako starts the Infinite-Canvas FastAPI app as an internal child service on `127.0.0.1` using a random port. The port is not exposed to the renderer.

HTTP proxy:

```bash
/api/infinite-canvas/*
```

WebSocket proxy:

```bash
/ws/infinite-canvas/*
```

Examples:

```bash
/api/infinite-canvas/api/config
/api/infinite-canvas/static/canvas.html
/api/infinite-canvas/output/example.png
/api/infinite-canvas/assets/input/example.png
/ws/infinite-canvas/stats
```

These routes are local-only in OpenHanako route security.

## Frontend Hosting

The Canvas tab renders a native OpenHanako `CanvasPage`. The page loads Infinite-Canvas HTML, CSS, and JavaScript into a DOM host and installs a temporary runtime bridge that rewrites legacy `/api`, `/static`, `/output`, `/assets`, and `/ws` URLs to OpenHanako proxy paths.

`static/index.html` from Infinite-Canvas is kept in the source and remains proxy-accessible, but CanvasPage does not use it as the main shell because that file uses internal iframes. CanvasPage opens the actual tool pages directly.

## Known Limitations

Infinite-Canvas self-update APIs are not the primary OpenHanako update path. For this integration, update `third_party/Infinite-Canvas` through the vendored source or submodule workflow.

If a legacy page depends on browser behaviors that cannot be fully reproduced inside a DOM host, keep the page source and server APIs intact, then patch the bridge or that specific legacy script.
