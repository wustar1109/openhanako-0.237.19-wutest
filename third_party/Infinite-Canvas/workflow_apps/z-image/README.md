# z-image-洗图

Generated from ComfyUI workflow `custom/Z_Image-洗图_高清修复v1_api.json`.

This app calls the Hanako / Infinite Canvas backend, which forwards workflow runs to your configured ComfyUI API service.

## API

Default backend:

```text
http://127.0.0.1:13000
```

Run endpoint:

```text
POST /api/workflows/custom%2FZ_Image-%E6%B4%97%E5%9B%BE_%E9%AB%98%E6%B8%85%E4%BF%AE%E5%A4%8Dv1_api.json/run
```

## Start

```bash
npm install
npm run dev
```

Set `VITE_HANAKO_API_BASE` for React/Vue or `HANAKO_API_BASE` for Gradio/Streamlit if the backend runs on another host.
