# Codex probe app updated

Generated from ComfyUI workflow `LTXDirectorv2-API.json`.

This app calls the Hanako / Infinite Canvas backend, which forwards workflow runs to your configured ComfyUI API service.

## API

Default backend:

```text
http://127.0.0.1:13000
```

Run endpoint:

```text
POST /api/workflows/LTXDirectorv2-API.json/run
```

## Start

```bash
npm install
npm run dev
```

Set `VITE_HANAKO_API_BASE` for React/Vue or `HANAKO_API_BASE` for Gradio/Streamlit if the backend runs on another host.
