# 风格转绘（需魔法）

Generated from ComfyUI workflow `custom/I2L一键风格转绘___超邪修搭配_一张图直接转绘.json`.

This app calls the Hanako / Infinite Canvas backend, which forwards workflow runs to your configured ComfyUI API service.

## API

Default backend:

```text
http://127.0.0.1:13000
```

Run endpoint:

```text
POST /api/workflows/custom%2FI2L%E4%B8%80%E9%94%AE%E9%A3%8E%E6%A0%BC%E8%BD%AC%E7%BB%98___%E8%B6%85%E9%82%AA%E4%BF%AE%E6%90%AD%E9%85%8D_%E4%B8%80%E5%BC%A0%E5%9B%BE%E7%9B%B4%E6%8E%A5%E8%BD%AC%E7%BB%98.json/run
```

## Start

```bash
npm install
npm run dev
```

Set `VITE_HANAKO_API_BASE` for React/Vue or `HANAKO_API_BASE` for Gradio/Streamlit if the backend runs on another host.
