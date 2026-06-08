# custom/超清放大-最高可放大至1亿像素-SeedVR2.5.15_api

Generated from ComfyUI workflow `custom/超清放大-最高可放大至1亿像素-SeedVR2.5.15_api.json`.

This app calls the Hanako / Infinite Canvas backend, which forwards workflow runs to your configured ComfyUI API service.

## API

Default backend:

```text
http://127.0.0.1:13000
```

Run endpoint:

```text
POST /api/workflows/custom%2F%E8%B6%85%E6%B8%85%E6%94%BE%E5%A4%A7-%E6%9C%80%E9%AB%98%E5%8F%AF%E6%94%BE%E5%A4%A7%E8%87%B31%E4%BA%BF%E5%83%8F%E7%B4%A0-SeedVR2.5.15_api.json/run
```

## Start

```bash
npm install
npm run dev
```

Set `VITE_HANAKO_API_BASE` for React/Vue or `HANAKO_API_BASE` for Gradio/Streamlit if the backend runs on another host.
