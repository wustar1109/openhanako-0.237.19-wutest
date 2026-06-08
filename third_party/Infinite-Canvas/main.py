import json
import uuid
import base64
import urllib.request
import urllib.parse
import urllib.error
import os
import re
import random
import sys
import subprocess
import time
import shutil
import asyncio
import logging
import html
import requests
import zipfile
import mimetypes
import tempfile
from typing import List, Dict, Any, Optional
from threading import Lock
import httpx
from PIL import Image
from io import BytesIO
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Header, Request, Form
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

QUIET_ACCESS_PATHS = {
    "/api/queue_status",
    "/api/canvases",
    "/api/canvases/trash",
}
QUIET_ACCESS_PREFIXES = (
    "/api/canvases/",
)

class QuietAccessLogFilter(logging.Filter):
    def filter(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 3:
            path = str(args[2]).split("?", 1)[0]
            status = int(args[4]) if len(args) >= 5 and str(args[4]).isdigit() else 0
            quiet_dynamic = any(path.startswith(prefix) and path.endswith("/meta") for prefix in QUIET_ACCESS_PREFIXES)
            if (path in QUIET_ACCESS_PATHS or quiet_dynamic) and status < 400:
                return False
        message = record.getMessage()
        if any(f'"GET {path}' in message and '" 200' in message for path in QUIET_ACCESS_PATHS):
            return False
        if 'GET /api/canvases/' in message and '/meta' in message and '" 200' in message:
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(QuietAccessLogFilter())

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- WebSocket 状态管理器 ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.user_connections: Dict[str, WebSocket] = {}
        self.connection_clients: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, client_id: str = None):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.connection_clients[websocket] = client_id or f"anon-{id(websocket)}"
        if client_id:
            self.user_connections[client_id] = websocket
        print(f"WS Connected. Total: {len(self.active_connections)}, Online: {self.online_count()}")
        await self.broadcast_count()

    async def disconnect(self, websocket: WebSocket, client_id: str = None):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self.connection_clients.pop(websocket, None)
        if client_id and self.user_connections.get(client_id) is websocket:
            del self.user_connections[client_id]
        print(f"WS Disconnected. Total: {len(self.active_connections)}, Online: {self.online_count()}")
        await self.broadcast_count()

    def online_count(self):
        visible_clients = {
            client_id for client_id in self.connection_clients.values()
            if client_id and not str(client_id).startswith("canvas_")
        }
        return len(visible_clients)

    async def broadcast_count(self):
        count = self.online_count()
        data = json.dumps({"type": "stats", "online_count": count})
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(data)
            except Exception as e:
                print(f"Broadcast error: {e}")
                self.active_connections.remove(connection)

    async def broadcast_new_image(self, image_data: dict):
        data = json.dumps({"type": "new_image", "data": image_data})
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(data)
            except Exception as e:
                print(f"Broadcast image error: {e}")
                self.active_connections.remove(connection)

    async def broadcast_canvas_updated(self, canvas_id: str, updated_at: int, client_id: str = ""):
        data = json.dumps({
            "type": "canvas_updated",
            "canvas_id": canvas_id,
            "updated_at": updated_at,
            "client_id": client_id or "",
        })
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(data)
            except Exception as e:
                print(f"Broadcast canvas error: {e}")
                self.active_connections.remove(connection)

    async def send_personal_message(self, message: dict, client_id: str):
        ws = self.user_connections.get(client_id)
        if ws:
            try:
                await ws.send_text(json.dumps(message))
            except Exception as e:
                print(f"Personal message error for {client_id}: {e}")

manager = ConnectionManager()
GLOBAL_LOOP = None
APP_VERSION = "2026.06.06.1"
GITHUB_REPO_URL = "https://github.com/hero8152/Infinite-Canvas"
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/hero8152/Infinite-Canvas/main/VERSION"
GITHUB_TREE_URL = "https://api.github.com/repos/hero8152/Infinite-Canvas/git/trees/main?recursive=1"
GITHUB_RAW_ROOT = "https://raw.githubusercontent.com/hero8152/Infinite-Canvas/main"

@app.on_event("startup")
async def startup_event():
    global GLOBAL_LOOP
    GLOBAL_LOOP = asyncio.get_running_loop()
    if not OPENHANAKO_HOSTED:
        sync_static_html_versions()

@app.websocket("/ws/stats")
async def websocket_endpoint(websocket: WebSocket, client_id: str = None):
    await manager.connect(websocket, client_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        await manager.disconnect(websocket, client_id)
    except Exception as e:
        print(f"WS Error: {e}")
        await manager.disconnect(websocket, client_id)

# --- 配置区域 ---

CLIENT_ID = str(uuid.uuid4())
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def runtime_path(env_name, default):
    value = os.getenv(env_name)
    return os.path.abspath(value) if value else default

INFINITE_CANVAS_HOME = runtime_path("INFINITE_CANVAS_HOME", BASE_DIR)
OPENHANAKO_HOSTED = bool(os.getenv("INFINITE_CANVAS_HOME"))
WORKFLOW_DIR = runtime_path("INFINITE_CANVAS_WORKFLOW_DIR", os.path.join(BASE_DIR, "workflows"))
ZIMAGE_DEFAULT_WORKFLOW = "Z-Image-Yaoguang-Detail.json"
WORKFLOW_PATH = os.path.join(WORKFLOW_DIR, ZIMAGE_DEFAULT_WORKFLOW)
STATIC_DIR = runtime_path("INFINITE_CANVAS_STATIC_DIR", os.path.join(BASE_DIR, "static"))
STATIC_RUNNINGHUB_DIR = os.path.join(STATIC_DIR, "runninghub")
STATIC_RUNNINGHUB_API_PROVIDERS_FILE = os.path.join(STATIC_RUNNINGHUB_DIR, "api_providers.json")
OUTPUT_DIR = runtime_path("INFINITE_CANVAS_OUTPUT_DIR", os.path.join(BASE_DIR, "output"))
ASSETS_DIR = runtime_path("INFINITE_CANVAS_ASSETS_DIR", os.path.join(BASE_DIR, "assets"))
OUTPUT_INPUT_DIR = os.path.join(ASSETS_DIR, "input")
OUTPUT_OUTPUT_DIR = os.path.join(ASSETS_DIR, "output")
ASSET_LIBRARY_DIR = os.path.join(ASSETS_DIR, "library")
LOCAL_UPLOAD_DIR = os.path.join(ASSETS_DIR, "uploads")
HISTORY_FILE = os.path.join(INFINITE_CANVAS_HOME, "history.json")
API_DIR = runtime_path("INFINITE_CANVAS_API_DIR", os.path.join(BASE_DIR, "API"))
API_ENV_FILE = os.path.join(API_DIR, ".env")
DATA_DIR = runtime_path("INFINITE_CANVAS_DATA_DIR", os.path.join(BASE_DIR, "data"))
CONVERSATION_DIR = os.path.join(DATA_DIR, "conversations")
CANVAS_DIR = os.path.join(DATA_DIR, "canvases")
ASSET_LIBRARY_PATH = os.path.join(DATA_DIR, "asset_library.json")
PROMPT_LIBRARY_PATH = os.path.join(DATA_DIR, "prompt_libraries.json")
API_PROVIDERS_FILE = os.path.join(DATA_DIR, "api_providers.json")
RUNNINGHUB_WORKFLOW_STORE_FILE = os.path.join(DATA_DIR, "runninghub_workflows.json")
WORKFLOW_APP_STORE_FILE = os.path.join(DATA_DIR, "workflow_apps.json")
WORKFLOW_APP_DIR = runtime_path("INFINITE_CANVAS_WORKFLOW_APP_DIR", os.path.join(BASE_DIR, "workflow_apps"))
SHARED_FOLDERS_FILE = os.path.join(DATA_DIR, "shared_folders.json")
GLOBAL_CONFIG_FILE = os.path.join(INFINITE_CANVAS_HOME, "global_config.json")
CANVAS_TRASH_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
LOCAL_IMAGE_IMPORT_MAX_BYTES = int(os.getenv("LOCAL_IMAGE_IMPORT_MAX_BYTES", str(50 * 1024 * 1024)))
LOCAL_IMAGE_IMPORT_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
GENERATED_ASSET_CATEGORIES = [
    {"id": "generated_zimage", "name": "文生图", "type": "image", "items": []},
    {"id": "generated_enhance", "name": "细节增强", "type": "image", "items": []},
    {"id": "generated_edit", "name": "图片编辑", "type": "image", "items": []},
    {"id": "generated_vector", "name": "转矢量图", "type": "image", "items": []},
    {"id": "generated_angle", "name": "角度控制", "type": "image", "items": []},
    {"id": "generated_online", "name": "在线生图", "type": "image", "items": []},
    {"id": "generated_canvas", "name": "无限画布", "type": "image", "items": []},
    {"id": "generated_other", "name": "其他生成", "type": "image", "items": []},
]
GENERATED_ASSET_CATEGORY_BY_TYPE = {
    "zimage": "generated_zimage",
    "cloud": "generated_zimage",
    "enhance": "generated_enhance",
    "klein": "generated_edit",
    "image-edit": "generated_edit",
    "edit": "generated_edit",
    "vectorize": "generated_vector",
    "angle": "generated_angle",
    "online": "generated_online",
    "ltx-director": "generated_canvas",
    "workflow-custom": "generated_canvas",
    "canvas": "generated_canvas",
}

QUEUE = []
QUEUE_LOCK = Lock()
HISTORY_LOCK = Lock()
ASSET_LIBRARY_LOCK = Lock()
PROMPT_LIBRARY_LOCK = Lock()
SHARED_FOLDERS_LOCK = Lock()
GLOBAL_CONFIG_LOCK = Lock()
CONVERSATION_LOCK = Lock()
CANVAS_LOCK = Lock()
LOAD_LOCK = Lock()
RUNNINGHUB_WORKFLOW_LOCK = Lock()
WORKFLOW_APP_LOCK = Lock()
NEXT_TASK_ID = 1
UPDATE_LOCK = Lock()

PROVIDER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{2,40}$")
SUPPORTED_PROVIDER_PROTOCOLS = {"openai", "apimart", "gemini", "volcengine", "runninghub"}
RUNNINGHUB_DEFAULT_BASE_URL = "https://www.runninghub.cn"
RUNNINGHUB_DEFAULT_IMAGE_MODELS = [
    "seedream-v5-lite/text-to-image",
    "seedream-v5-lite/image-to-image",
]
RUNNINGHUB_DEFAULT_APPS = [
    {
        "id": "2058517022748798977",
        "appId": "2058517022748798977",
        "title": "2511-风格迁移",
        "note": "",
        "thumbnail": "",
        "enabled": True,
        "fields": [
            {
                "id": "100::image",
                "nodeId": "100",
                "fieldName": "image",
                "fieldValue": "pasted/57ef7dc980b6446bca366caaf3f94eb12b22b23f78aa30e294b39cabd7d0187b.png",
                "fieldType": "IMAGE",
                "label": "image",
                "enabled": True,
                "sourceFromUpstream": True,
                "group": "AI 应用参数",
                "note": "image",
                "options": [],
                "random_enabled": False,
                "min": "",
                "max": "",
                "step": "",
                "imageOrder": 0,
                "required": False,
            },
            {
                "id": "112::image",
                "nodeId": "112",
                "fieldName": "image",
                "fieldValue": "8cff63ee4b3e0285ca85ab90a52e26746df84ed0dec0be9d76c679cbb62a247d.png",
                "fieldType": "IMAGE",
                "label": "image",
                "enabled": True,
                "sourceFromUpstream": True,
                "group": "AI 应用参数",
                "note": "image",
                "options": [],
                "random_enabled": False,
                "min": "",
                "max": "",
                "step": "",
                "imageOrder": 0,
                "required": False,
            },
            {
                "id": "14::seed",
                "nodeId": "14",
                "fieldName": "seed",
                "fieldValue": "554049736557817",
                "fieldType": "INT",
                "label": "seed",
                "enabled": True,
                "sourceFromUpstream": True,
                "group": "AI 应用参数",
                "note": "seed",
                "options": [],
                "random_enabled": True,
                "min": "",
                "max": "",
                "step": "",
                "imageOrder": 0,
                "required": False,
            },
        ],
    },
    {
        "id": "1997622492837646338",
        "appId": "1997622492837646338",
        "title": "2511-光线迁移",
        "note": "",
        "thumbnail": "",
        "enabled": True,
    },
]
RUNNINGHUB_DEFAULT_WORKFLOWS = [
    {
        "id": "2058554058318897153",
        "workflowId": "2058554058318897153",
        "title": "GPT-Image-2-图片编辑",
        "note": "",
        "thumbnail": "",
        "enabled": True,
        "optionalImageMode": "prune-workflow",
    },
    {
        "id": "2058541134623891458",
        "workflowId": "2058541134623891458",
        "title": "NanoBanana-2-图片编辑",
        "note": "",
        "thumbnail": "",
        "enabled": True,
        "optionalImageMode": "prune-workflow",
    },
]

def ensure_runtime_config_files():
    """首次运行时提前创建配置目录，避免第一次保存 API Key 时才创建目录/文件。"""
    try:
        os.makedirs(os.path.dirname(API_ENV_FILE), exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(API_ENV_FILE):
            with open(API_ENV_FILE, "a", encoding="utf-8"):
                pass
    except Exception as e:
        print(f"初始化 API 配置目录失败: {e}")

def load_env_file():
    if not os.path.exists(API_ENV_FILE):
        return
    try:
        with open(API_ENV_FILE, 'r', encoding='utf-8-sig') as f:
            for raw_line in f.read().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except Exception as e:
        print(f"加载 API/.env 失败: {e}")
ensure_runtime_config_files()
load_env_file()

COMFYUI_INSTANCES = [s.strip() for s in os.getenv("COMFYUI_INSTANCES", "127.0.0.1:8188").split(",") if s.strip()]
COMFYUI_ADDRESS = COMFYUI_INSTANCES[0]

AI_BASE_URL = os.getenv("COMFLY_BASE_URL", "https://ai.comfly.chat").rstrip("/")
AI_API_KEY = os.getenv("COMFLY_API_KEY", "")
MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
MODELSCOPE_CHAT_BASE_URL = "https://api-inference.modelscope.cn/v1"
MODELSCOPE_DEFAULT_TEXT_IMAGE_MODELS = [
    "Tongyi-MAI/Z-Image-Turbo",
    "Qwen/Qwen-Image-2512",
    "black-forest-labs/FLUX.2-klein-9B",
]
MODELSCOPE_DEFAULT_IMAGE_TO_IMAGE_MODELS = [
    "Qwen/Qwen-Image-Edit-2511",
]
MODELSCOPE_DEFAULT_IMAGE_MODELS = [
    *MODELSCOPE_DEFAULT_TEXT_IMAGE_MODELS,
    *MODELSCOPE_DEFAULT_IMAGE_TO_IMAGE_MODELS,
]
MODELSCOPE_DEFAULT_CHAT_MODELS = [
    "Qwen/Qwen3-235B-A22B",
    "Qwen/Qwen3-VL-235B-A22B-Instruct",
    "MiniMax/MiniMax-M2.7:MiniMax",
]
_MODELSCOPE_CONFIGURED_CHAT_MODELS = [m.strip() for m in os.getenv("MODELSCOPE_CHAT_MODELS", "").split(",") if m.strip()]
MODELSCOPE_CHAT_MODELS = list(dict.fromkeys([m for m in [*MODELSCOPE_DEFAULT_CHAT_MODELS, *_MODELSCOPE_CONFIGURED_CHAT_MODELS] if m]))
MODELSCOPE_DEFAULT_IMAGE_MODEL = MODELSCOPE_DEFAULT_IMAGE_MODELS[0]
MODELSCOPE_IMAGE_EDIT_MODELS = set(MODELSCOPE_DEFAULT_IMAGE_TO_IMAGE_MODELS)
MODELSCOPE_ENHANCE_MODEL = "black-forest-labs/FLUX.2-klein-9B"
MODELSCOPE_ENHANCE_LORA = "Daniel8152/Klein-enhance"
MODELSCOPE_LEGACY_ENHANCE_MODEL = "Tongyi-MAI/Z-Image-Turbo"
MODELSCOPE_LEGACY_ENHANCE_LORA = "xiiian/z-zhigan"
MODELSCOPE_DEFAULT_CHAT_MODEL = "Qwen/Qwen3-235B-A22B"
MODELSCOPE_DEFAULT_LORAS = [
    {
        "id": "Daniel8152/film",
        "name": "Z-Image Film",
        "target_model": "Tongyi-MAI/Z-Image-Turbo",
        "strength": 0.8,
        "enabled": True,
        "note": "",
    },
    {
        "id": "Daniel8152/Qwen-Image-2512-Film",
        "name": "Qwen Image 2512 Film",
        "target_model": "Qwen/Qwen-Image-2512",
        "strength": 0.8,
        "enabled": True,
        "note": "",
    },
    {
        "id": "Daniel8152/Klein-enhance",
        "name": "Klein enhance",
        "target_model": "black-forest-labs/FLUX.2-klein-9B",
        "strength": 0.8,
        "enabled": True,
        "note": "",
    },
]
MODELSCOPE_DEFAULTS_VERSION = 3
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "30"))
AI_REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "1800"))
IMAGE_POLL_INTERVAL = float(os.getenv("IMAGE_POLL_INTERVAL", "2"))
IMAGE_TASK_TIMEOUT = float(os.getenv("IMAGE_TASK_TIMEOUT", str(AI_REQUEST_TIMEOUT)))
COMFYUI_HISTORY_TIMEOUT = int(float(os.getenv("COMFYUI_HISTORY_TIMEOUT", "1800")))
APIMART_IMAGE_TASK_TIMEOUT = float(os.getenv("APIMART_IMAGE_TASK_TIMEOUT", "1800"))
APIMART_IMAGE_POLL_INTERVAL = float(os.getenv("APIMART_IMAGE_POLL_INTERVAL", "5"))
APIMART_IMAGE_INITIAL_POLL_DELAY = float(os.getenv("APIMART_IMAGE_INITIAL_POLL_DELAY", "10"))
VIDEO_POLL_TIMEOUT = float(os.getenv("VIDEO_POLL_TIMEOUT", "1800"))
ONLINE_IMAGE_PROMPT_MAX_LENGTH = int(os.getenv("ONLINE_IMAGE_PROMPT_MAX_LENGTH", "20000"))
VIDEO_PROMPT_MAX_LENGTH = int(os.getenv("VIDEO_PROMPT_MAX_LENGTH", "4000"))
LLM_MESSAGE_MAX_LENGTH = int(os.getenv("LLM_MESSAGE_MAX_LENGTH", "20000"))

FIELD_LABELS = {
    "prompt": "提示词",
    "message": "文本",
    "system_prompt": "系统提示词",
}

def friendly_validation_error(errors):
    parts = []
    for err in errors or []:
        loc = [str(item) for item in err.get("loc", []) if item != "body"]
        field = loc[-1] if loc else ""
        label = FIELD_LABELS.get(field, field or "请求参数")
        ctx = err.get("ctx") or {}
        limit = ctx.get("limit_value") or ctx.get("max_length") or ctx.get("min_length")
        err_type = str(err.get("type") or "")
        msg = str(err.get("msg") or "")
        if "max_length" in err_type or "at most" in msg:
            parts.append(f"{label}过长：当前内容超过后端上限 {limit} 个字符。请拆分为多个提示词节点，或先用 LLM 节点压缩后再生成。")
        elif "min_length" in err_type:
            parts.append(f"{label}不能为空。")
        else:
            parts.append(f"{label}格式不正确：{msg}")
    return "\n".join(parts) or "请求参数不正确。"

@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": friendly_validation_error(exc.errors()), "errors": exc.errors()},
    )

def model_list(env_name, primary, defaults):
    configured = os.getenv(env_name, "")
    configured_values = [item.strip() for item in configured.split(",") if item.strip()]
    values = configured_values or [primary, *defaults]
    deduped = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped

def reload_env_globals():
    """保存 API 设置后，将 os.environ 里最新的值同步回模块级全局变量，
    避免保存后需要重启才能生效。"""
    global MODELSCOPE_API_KEY, AI_API_KEY, AI_BASE_URL
    global IMAGE_MODELS, CHAT_MODELS, VIDEO_MODELS, MODELSCOPE_CHAT_MODELS
    MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
    AI_API_KEY = os.getenv("COMFLY_API_KEY", "")
    AI_BASE_URL = os.getenv("COMFLY_BASE_URL", "https://ai.comfly.chat").rstrip("/")
    IMAGE_MODELS = model_list("IMAGE_MODELS", os.getenv("IMAGE_MODEL", IMAGE_MODEL), ["nano-banana-pro"])
    CHAT_MODELS = model_list("CHAT_MODELS", os.getenv("CHAT_MODEL", CHAT_MODEL), ["gpt-4o-mini", "gemini-3.1-flash-image-preview-2k"])
    VIDEO_MODELS = model_list("VIDEO_MODELS", "veo3-fast", [
        "veo2", "veo2-fast", "veo2-pro",
        "veo3", "veo3-fast", "veo3-pro",
        "veo3.1", "veo3.1-fast", "veo3.1-quality", "veo3.1-lite",
        "sora-2", "sora-2-pro",
        "wan2.6-t2v", "wan2.6-i2v",
        "wan2.5-t2v-preview", "wan2.5-i2v-preview",
        "wan2.2-t2v-plus", "wan2.2-i2v-plus", "wan2.2-i2v-flash",
        "doubao-seedance-2-0-260128",
        "doubao-seedance-2-0-fast-260128",
        "doubao-seedance-1-5-pro-251215",
        "doubao-seedance-1-0-pro-250528",
        "doubao-seedance-1-0-lite-t2v-250428",
        "doubao-seedance-1-0-lite-i2v-250428",
    ])
    _configured = [m.strip() for m in os.getenv("MODELSCOPE_CHAT_MODELS", "").split(",") if m.strip()]
    MODELSCOPE_CHAT_MODELS = list(dict.fromkeys([m for m in [*MODELSCOPE_DEFAULT_CHAT_MODELS, *_configured] if m]))

CHAT_MODELS = model_list("CHAT_MODELS", CHAT_MODEL, ["gpt-4o-mini", "gemini-3.1-flash-image-preview-2k"])
IMAGE_MODELS = model_list("IMAGE_MODELS", IMAGE_MODEL, ["nano-banana-pro"])
VIDEO_MODELS = model_list("VIDEO_MODELS", "veo3-fast", [
    # —— Veo 系列 ——
    "veo2", "veo2-fast", "veo2-pro",
    "veo3", "veo3-fast", "veo3-pro",
    "veo3.1", "veo3.1-fast", "veo3.1-quality", "veo3.1-lite",
    # —— Sora ——
    "sora-2", "sora-2-pro",
    # —— 阿里 通义万相 ——
    "wan2.6-t2v", "wan2.6-i2v",
    "wan2.5-t2v-preview", "wan2.5-i2v-preview",
    "wan2.2-t2v-plus", "wan2.2-i2v-plus", "wan2.2-i2v-flash",
    # —— 火山 豆包 Seedance ——
    "doubao-seedance-2-0-260128",
    "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-1-5-pro-251215",
    "doubao-seedance-1-0-pro-250528",
    "doubao-seedance-1-0-lite-t2v-250428",
    "doubao-seedance-1-0-lite-i2v-250428",
])

def provider_key_env(provider_id):
    if provider_id == "comfly":
        return "COMFLY_API_KEY"
    if provider_id == "modelscope":
        return "MODELSCOPE_API_KEY"
    if provider_id == "runninghub":
        return "RUNNINGHUB_API_KEY"
    return f"API_PROVIDER_{re.sub(r'[^A-Za-z0-9]', '_', provider_id).upper()}_KEY"

def runninghub_wallet_key_env():
    return "RUNNINGHUB_WALLET_API_KEY"

def mask_secret(value):
    if not value:
        return ""
    tail = value[-4:] if len(value) > 4 else value
    return f"••••••••{tail}"

def default_api_providers():
    # 只保留 ModelScope 为强制默认平台，其他平台均可自定义增删
    return [
        {
            "id": "modelscope",
            "name": "ModelScope",
            "base_url": MODELSCOPE_CHAT_BASE_URL,
            "protocol": "openai",
            "image_generation_endpoint": "",
            "image_edit_endpoint": "",
            "enabled": True,
            "primary": False,
            "text_image_models": MODELSCOPE_DEFAULT_TEXT_IMAGE_MODELS,
            "image_to_image_models": MODELSCOPE_DEFAULT_IMAGE_TO_IMAGE_MODELS,
            "image_models": MODELSCOPE_DEFAULT_IMAGE_MODELS,
            "chat_models": MODELSCOPE_CHAT_MODELS,
            "video_models": [],
            "ms_loras": MODELSCOPE_DEFAULT_LORAS,
            "ms_defaults_version": MODELSCOPE_DEFAULTS_VERSION,
        },
        {
            "id": "runninghub",
            "name": "RunningHub",
            "base_url": RUNNINGHUB_DEFAULT_BASE_URL,
            "protocol": "runninghub",
            "image_generation_endpoint": "",
            "image_edit_endpoint": "",
            "enabled": True,
            "primary": False,
            "text_image_models": RUNNINGHUB_DEFAULT_IMAGE_MODELS,
            "image_to_image_models": [],
            "image_models": RUNNINGHUB_DEFAULT_IMAGE_MODELS,
            "chat_models": [],
            "video_models": [],
            "ms_loras": [],
            "ms_defaults_version": 0,
            "rh_apps": RUNNINGHUB_DEFAULT_APPS,
            "rh_workflows": RUNNINGHUB_DEFAULT_WORKFLOWS,
        },
    ]

def merge_default_api_providers(providers):
    merged = [dict(item) for item in providers]
    # 强制保留独立入口平台（不再强制 comfly）
    ms_default = next((d for d in default_api_providers() if d["id"] == "modelscope"), None)
    if ms_default:
        current = next((item for item in merged if item.get("id") == "modelscope"), None)
        if not current:
            merged.append(ms_default)
        else:
            if not current.get("base_url"):
                current["base_url"] = ms_default["base_url"]
            seeded_version = int(current.get("ms_defaults_version") or 0)
            if seeded_version < MODELSCOPE_DEFAULTS_VERSION:
                text_models, i2i_models, image_models = split_image_model_lists(
                    [*MODELSCOPE_DEFAULT_IMAGE_MODELS, *(current.get("image_models") or [])],
                    [*MODELSCOPE_DEFAULT_TEXT_IMAGE_MODELS, *(current.get("text_image_models") or [])],
                    [*MODELSCOPE_DEFAULT_IMAGE_TO_IMAGE_MODELS, *(current.get("image_to_image_models") or [])],
                )
                chat_models = model_list_from_values([*MODELSCOPE_DEFAULT_CHAT_MODELS, *(current.get("chat_models") or [])])
                loras = normalize_ms_loras([*MODELSCOPE_DEFAULT_LORAS, *(current.get("ms_loras") or [])])
                current["text_image_models"] = text_models
                current["image_to_image_models"] = i2i_models
                current["image_models"] = image_models
                current["chat_models"] = chat_models
                current["ms_loras"] = loras
                current["ms_defaults_version"] = MODELSCOPE_DEFAULTS_VERSION
    rh_default = load_static_runninghub_provider() or next((d for d in default_api_providers() if d["id"] == "runninghub"), None)
    if rh_default:
        current = next((item for item in merged if item.get("id") == "runninghub"), None)
        if not current:
            merged.append(rh_default)
        else:
            if not current.get("base_url"):
                current["base_url"] = rh_default["base_url"]
            if not current.get("protocol") or current.get("protocol") == "openai":
                current["protocol"] = "runninghub"
            text_models, i2i_models, image_models = split_image_model_lists(
                [*(current.get("image_models") or []), *(rh_default.get("image_models") or [])],
                [*(current.get("text_image_models") or []), *(rh_default.get("text_image_models") or [])],
                [*(current.get("image_to_image_models") or []), *(rh_default.get("image_to_image_models") or [])],
            )
            current["text_image_models"] = text_models
            current["image_to_image_models"] = i2i_models
            current["image_models"] = image_models
            current["rh_apps"] = merge_runninghub_system_entries(rh_default.get("rh_apps") or [], current.get("rh_apps") or [], "app")
            current["rh_workflows"] = merge_runninghub_system_entries(rh_default.get("rh_workflows") or [], current.get("rh_workflows") or [], "workflow")
    return merged

def normalize_model_list(values):
    return model_list_from_values(values)

def model_list_from_values(values):
    deduped = []
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in deduped:
            selected_model(item, item)
            deduped.append(item)
    return deduped

def dedupe_model_ids(values):
    return model_list_from_values(values)

IMAGE_TO_IMAGE_MODEL_PATTERNS = [
    r"(^|[-_/])image[-_]?edit([-_/]|$)",
    r"(^|[-_/])edit([-_/]|$)",
    r"img2img",
    r"image[-_]?to[-_]?image",
    r"i2i",
    r"inpaint",
    r"outpaint",
    r"control",
    r"reference",
    r"ip[-_]?adapter",
    r"remix",
    r"upscale",
    r"super[-_]?resolution",
    r"restore",
    r"enhance",
    r"detail",
]

MODELSCOPE_LIBRARY_PAGE_SIZE = 50
MODELSCOPE_LIBRARY_MAX_GENERAL_PAGES = 20
MODELSCOPE_LIBRARY_MAX_SEARCH_PAGES = 3
MODELSCOPE_LIBRARY_SEARCH_PLAN = [
    ("image", 20),
    ("text-to-image-synthesis", 20),
    ("text-to-image", 10),
    ("image generation", 6),
    ("image-edit", 6),
    ("inpainting", 6),
    ("super-resolution", 6),
    ("qwen image", 4),
    ("z-image", 4),
    ("flux", 4),
    ("stable diffusion", 4),
    ("sdxl", 4),
    ("seedream", 4),
    ("kolors", 4),
    ("upscale", 4),
]

MODELSCOPE_TEXT_IMAGE_TASKS = {
    "text-to-image",
    "text-to-image-synthesis",
    "text-to-image-generation",
    "image-generation",
    "image-synthesis",
}

MODELSCOPE_IMAGE_TO_IMAGE_TASKS = {
    "image-to-image",
    "image-to-image-synthesis",
    "image-to-image-generation",
    "image-editing",
    "image-inpainting",
    "image-outpainting",
    "image-super-resolution",
    "image-restoration",
    "image-denoising",
    "image-deblurring",
    "image-colorization",
}

MODELSCOPE_NON_GENERATION_IMAGE_TASKS = {
    "image-text-to-text",
    "image-to-text",
    "image-captioning",
    "image-classification",
    "image-segmentation",
    "semantic-segmentation",
    "instance-segmentation",
    "object-detection",
    "image-object-detection",
    "visual-question-answering",
    "image-text-retrieval",
    "image-retrieval",
    "ocr",
    "optical-character-recognition",
    "document-vqa",
    "depth-estimation",
    "face-detection",
    "face-recognition",
    "keypoint-detection",
}

def is_image_to_image_model_name(model):
    value = str(model or "").strip()
    if not value:
        return False
    if value in MODELSCOPE_IMAGE_EDIT_MODELS:
        return True
    lc = value.lower()
    return any(re.search(pattern, lc, re.I) for pattern in IMAGE_TO_IMAGE_MODEL_PATTERNS)

def is_modelscope_base_url(base_url: str) -> bool:
    try:
        host = urllib.parse.urlparse(str(base_url or "")).netloc.lower()
    except Exception:
        host = str(base_url or "").lower()
    return "modelscope.cn" in host or "modelscope.ai" in host

def modelscope_library_models_url(base_url: str) -> str:
    try:
        host = urllib.parse.urlparse(str(base_url or "")).netloc.lower()
    except Exception:
        host = ""
    domain = "modelscope.ai" if host.endswith("modelscope.ai") or ".modelscope.ai" in host else "modelscope.cn"
    return f"https://{domain}/openapi/v1/models"

def modelscope_record_id(record: dict) -> str:
    if not isinstance(record, dict):
        return ""
    return str(record.get("id") or record.get("model_id") or record.get("modelId") or record.get("name") or "").strip()

def modelscope_record_text(record: dict) -> str:
    if not isinstance(record, dict):
        return ""
    pieces = [
        modelscope_record_id(record),
        record.get("display_name") or "",
        record.get("name") or "",
        record.get("description") or "",
    ]
    for key in ("tasks", "tags", "task", "pipeline_tag"):
        value = record.get(key)
        if isinstance(value, list):
            pieces.extend(str(item or "") for item in value)
        elif value:
            pieces.append(str(value))
    return " ".join(pieces).lower()

def classify_modelscope_library_record(record: dict) -> Optional[str]:
    """Return text_image / image_to_image for ModelScope Hub records, or None when not a generation model."""
    model_id = modelscope_record_id(record)
    if not model_id:
        return None
    tasks = record.get("tasks") if isinstance(record, dict) else []
    if isinstance(tasks, str):
        task_set = {tasks.lower()}
    elif isinstance(tasks, list):
        task_set = {str(item or "").lower() for item in tasks if str(item or "").strip()}
    else:
        task_set = set()
    text = modelscope_record_text(record)

    if task_set & MODELSCOPE_IMAGE_TO_IMAGE_TASKS:
        return "image_to_image"
    if task_set & MODELSCOPE_TEXT_IMAGE_TASKS:
        return "text_image"
    if task_set & MODELSCOPE_NON_GENERATION_IMAGE_TASKS:
        return None
    if task_set and not any("image" in task or "diffusion" in task for task in task_set):
        return None
    if is_image_to_image_model_name(model_id) or any(key in text for key in (
        "image edit", "image-edit", "img2img", "image-to-image", "inpaint", "outpaint",
        "upscale", "super-resolution", "super resolution", "restore", "restoration",
        "enhance", "detail", "controlnet", "ip-adapter",
    )):
        return "image_to_image"
    if any(key in text for key in (
        "text-to-image", "text to image", "txt2img", "image generation", "image-generation",
        "z-image", "qwen-image", "qwen image", "flux", "stable-diffusion", "stable diffusion",
        "sdxl", "sd3", "seedream", "kolors", "dall-e", "dalle", "kandinsky", "diffusion",
    )):
        if any(key in text for key in ("image-text-to-text", "image to text", "ocr", "segmentation", "classification", "object-detection", "detection", "retrieval", "vl-")):
            return None
        return "text_image"
    return None

def modelscope_library_headers(api_key: str) -> dict:
    headers = {"Accept": "application/json"}
    api_key = (api_key or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers

async def fetch_modelscope_library_models(base_url: str, api_key: str):
    """Fetch ModelScope Hub candidates ordered by downloads and keep image-generation models only."""
    url = modelscope_library_models_url(base_url)
    page_size = MODELSCOPE_LIBRARY_PAGE_SIZE
    jobs = [(None, page) for page in range(1, MODELSCOPE_LIBRARY_MAX_GENERAL_PAGES + 1)]
    for term, pages in MODELSCOPE_LIBRARY_SEARCH_PLAN:
        max_pages = max(1, int(pages or MODELSCOPE_LIBRARY_MAX_SEARCH_PAGES))
        jobs.extend((term, page) for page in range(1, max_pages + 1))

    semaphore = asyncio.Semaphore(6)

    async def fetch_page(client, search_term, page_number):
        params = {
            "page_number": page_number,
            "page_size": page_size,
            "sort": "downloads",
            "order": "desc",
        }
        if search_term:
            params["search"] = search_term
        async with semaphore:
            try:
                resp = await client.get(url, params=params, headers=modelscope_library_headers(api_key))
                if resp.status_code >= 400:
                    return []
                data = resp.json() if resp.text else {}
            except Exception:
                return []
        payload = data.get("data") if isinstance(data, dict) else {}
        models = payload.get("models") if isinstance(payload, dict) else []
        return models if isinstance(models, list) else []

    async with httpx.AsyncClient(timeout=25) as client:
        pages = await asyncio.gather(*(fetch_page(client, term, page) for term, page in jobs))

    records = {}
    for page in pages:
        for record in page:
            model_id = modelscope_record_id(record)
            if not model_id:
                continue
            cat = classify_modelscope_library_record(record)
            if not cat:
                continue
            try:
                downloads = int(record.get("downloads") or 0)
            except Exception:
                downloads = 0
            previous = records.get(model_id)
            if not previous or downloads > previous["downloads"]:
                records[model_id] = {"id": model_id, "category": cat, "downloads": downloads}

    ordered = sorted(records.values(), key=lambda item: (-item["downloads"], item["id"].lower()))
    grouped = {"image": [], "text_image": [], "image_to_image": []}
    for item in ordered:
        model_id = item["id"]
        grouped["image"].append(model_id)
        grouped[item["category"]].append(model_id)
    return grouped

def split_image_model_lists(image_models=None, text_image_models=None, image_to_image_models=None):
    text_models = model_list_from_values(text_image_models or [])
    i2i_models = model_list_from_values(image_to_image_models or [])
    combined_source = model_list_from_values(image_models or [])
    if not combined_source:
        combined_source = model_list_from_values([*text_models, *i2i_models])
    for model in combined_source:
        if model in text_models or model in i2i_models:
            continue
        if is_image_to_image_model_name(model):
            i2i_models.append(model)
        else:
            text_models.append(model)
    combined = model_list_from_values([*text_models, *i2i_models])
    return text_models, i2i_models, combined

def normalize_ms_loras(values):
    normalized = []
    seen = set()
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        lora_id = str(raw.get("id") or "").strip()
        if not lora_id:
            continue
        target_model = str(raw.get("target_model") or raw.get("model") or "").strip()
        if not target_model:
            continue
        key = (target_model, lora_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            strength = float(raw.get("strength", raw.get("default_strength", 0.8)))
        except Exception:
            strength = 0.8
        strength = max(0.0, min(2.0, strength))
        name = re.sub(r"\s+", " ", str(raw.get("name") or "").strip())[:80]
        normalized.append({
            "id": lora_id[:180],
            "name": name or lora_id,
            "target_model": target_model[:180],
            "strength": strength,
            "enabled": bool(raw.get("enabled", True)),
            "note": str(raw.get("note") or "").strip()[:300],
        })
    return normalized

def ms_lora_strength(loras, lora_id):
    if loras is None:
        return None
    if isinstance(loras, dict):
        for key, value in loras.items():
            if str(key).strip() == lora_id:
                try:
                    return float(value)
                except Exception:
                    return 0.8
        return None
    if isinstance(loras, list):
        for item in loras:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("lora") or item.get("model") or "").strip()
            if item_id == lora_id:
                try:
                    return float(item.get("strength", item.get("weight", 0.8)))
                except Exception:
                    return 0.8
    return None

def normalize_ms_enhance_request(model, loras):
    selected = selected_model(model, MODELSCOPE_ENHANCE_MODEL)
    legacy_strength = ms_lora_strength(loras, MODELSCOPE_LEGACY_ENHANCE_LORA)
    enhance_strength = ms_lora_strength(loras, MODELSCOPE_ENHANCE_LORA)
    if legacy_strength is not None:
        return MODELSCOPE_ENHANCE_MODEL, {MODELSCOPE_ENHANCE_LORA: legacy_strength}
    if enhance_strength is not None:
        return MODELSCOPE_ENHANCE_MODEL, {MODELSCOPE_ENHANCE_LORA: enhance_strength}
    return selected, loras

def normalize_runninghub_entry(raw, kind):
    if not isinstance(raw, dict):
        return None
    raw_id = raw.get("appId") if kind == "app" else raw.get("workflowId")
    entry_id = str(raw_id or raw.get("id") or "").strip()
    match = re.search(r"/run/(ai-app|workflow)/([0-9A-Za-z_-]+)", entry_id)
    if match:
        entry_id = match.group(2)
    if not entry_id:
        return None
    title = re.sub(r"\s+", " ", str(raw.get("title") or raw.get("name") or "").strip())[:80]
    note = str(raw.get("note") or raw.get("description") or "").strip()[:500]
    thumb = str(raw.get("thumbnail") or "").strip()
    if len(thumb) > 1500000:
        thumb = ""
    entry = {
        "id": entry_id[:80],
        "title": title or (f"AI 应用 {entry_id[-6:]}" if kind == "app" else f"工作流 {entry_id[-6:]}"),
        "note": note,
        "thumbnail": thumb,
        "enabled": bool(raw.get("enabled", True)),
    }
    if raw.get("hidden") is True:
        entry["hidden"] = True
    fields = raw.get("fields")
    if isinstance(fields, list):
        entry["fields"] = [runninghub_normalize_field(field) for field in fields if isinstance(field, dict)]
    if kind == "workflow":
        mode = str(raw.get("optionalImageMode") or raw.get("optional_image_mode") or "prune-workflow").strip()
        entry["optionalImageMode"] = mode or "prune-workflow"
        workflow_json = raw.get("workflowJson") or raw.get("workflow_json")
        if isinstance(workflow_json, dict):
            entry["workflowJson"] = workflow_json
    raw_payload = raw.get("raw")
    if isinstance(raw_payload, dict):
        entry["raw"] = raw_payload
    try:
        updated_at = int(raw.get("updatedAt") or raw.get("updated_at") or 0)
        if updated_at > 0:
            entry["updatedAt"] = updated_at
    except Exception:
        pass
    if kind == "app":
        entry["appId"] = entry["id"]
    else:
        entry["workflowId"] = entry["id"]
    return entry

def normalize_runninghub_entries(values, kind):
    normalized = []
    seen = set()
    for raw in values or []:
        entry = normalize_runninghub_entry(raw, kind)
        if not entry or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        normalized.append(entry)
    return normalized

def runninghub_entry_id(entry, kind):
    if not isinstance(entry, dict):
        return ""
    raw_id = entry.get("workflowId") if kind == "workflow" else entry.get("appId")
    return str(raw_id or entry.get("id") or "").strip()

def merge_runninghub_system_entries(system_entries, user_entries, kind):
    merged = []
    index = {}
    hidden_ids = set()
    for entry in normalize_runninghub_entries(system_entries or [], kind):
        entry_id = runninghub_entry_id(entry, kind)
        if not entry_id:
            continue
        index[entry_id] = len(merged)
        merged.append(entry)
    for entry in normalize_runninghub_entries(user_entries or [], kind):
        entry_id = runninghub_entry_id(entry, kind)
        if not entry_id:
            continue
        if entry.get("hidden") is True:
            hidden_ids.add(entry_id)
            if entry_id in index:
                merged.pop(index[entry_id])
                index = {runninghub_entry_id(item, kind): idx for idx, item in enumerate(merged)}
            continue
        if entry_id in index:
            merged[index[entry_id]] = entry
        else:
            index[entry_id] = len(merged)
            merged.append(entry)
    return [entry for entry in merged if runninghub_entry_id(entry, kind) not in hidden_ids]

def load_static_runninghub_provider():
    if not os.path.exists(STATIC_RUNNINGHUB_API_PROVIDERS_FILE):
        return None
    try:
        with open(STATIC_RUNNINGHUB_API_PROVIDERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        candidates = raw if isinstance(raw, list) else raw.get("providers") if isinstance(raw, dict) else []
        if isinstance(raw, dict) and raw.get("id") == "runninghub":
            candidates = [raw]
        for item in candidates or []:
            if isinstance(item, dict) and str(item.get("id") or "").strip().lower() == "runninghub":
                return normalize_provider(item)
    except Exception as e:
        print(f"加载 static RunningHub 配置失败: {e}")
    return None

def merge_runninghub_provider_with_static(provider):
    static_provider = load_static_runninghub_provider()
    if not static_provider:
        return provider
    if not isinstance(provider, dict):
        return static_provider
    merged = {**static_provider, **provider}
    merged["protocol"] = "runninghub"
    text_models, i2i_models, image_models = split_image_model_lists(
        [*(provider.get("image_models") or []), *(static_provider.get("image_models") or [])],
        [*(provider.get("text_image_models") or []), *(static_provider.get("text_image_models") or [])],
        [*(provider.get("image_to_image_models") or []), *(static_provider.get("image_to_image_models") or [])],
    )
    merged["text_image_models"] = text_models
    merged["image_to_image_models"] = i2i_models
    merged["image_models"] = image_models
    merged["rh_apps"] = merge_runninghub_system_entries(static_provider.get("rh_apps") or [], provider.get("rh_apps") or [], "app")
    merged["rh_workflows"] = merge_runninghub_system_entries(static_provider.get("rh_workflows") or [], provider.get("rh_workflows") or [], "workflow")
    return normalize_provider(merged)

def preserve_runninghub_hidden_overrides(provider):
    if not isinstance(provider, dict) or provider.get("id") != "runninghub":
        return provider
    static_provider = load_static_runninghub_provider()
    if not static_provider:
        return provider
    provider = dict(provider)
    for list_key, kind in (("rh_apps", "app"), ("rh_workflows", "workflow")):
        current = normalize_runninghub_entries(provider.get(list_key) or [], kind)
        current_ids = {runninghub_entry_id(item, kind) for item in current}
        for static_entry in static_provider.get(list_key) or []:
            entry_id = runninghub_entry_id(static_entry, kind)
            if entry_id and entry_id not in current_ids:
                tombstone = normalize_runninghub_entry({**static_entry, "enabled": False, "hidden": True}, kind)
                if tombstone:
                    current.append(tombstone)
        provider[list_key] = current
    return provider

def normalize_endpoint_override(value, label):
    endpoint = str(value or "").strip()
    if not endpoint:
        return ""
    if len(endpoint) > 300 or re.search(r"\s", endpoint):
        raise HTTPException(status_code=400, detail=f"{label} 不合法，请填写类似 /v1/images/edits 的路径")
    if re.match(r"^https?://", endpoint, re.I):
        return endpoint.rstrip("/")
    if not endpoint.startswith("/"):
        raise HTTPException(status_code=400, detail=f"{label} 需要以 /v1/... 开头，或填写完整 http(s) 地址")
    return endpoint

def provider_endpoint_url(provider, key, default_path):
    base_url = str((provider or {}).get("base_url") or AI_BASE_URL).strip().rstrip("/")
    override = str((provider or {}).get(key) or "").strip()
    if override:
        if re.match(r"^https?://", override, re.I):
            return override.rstrip("/")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{override}"
        return override
    if base_url.endswith("/v1") and default_path.startswith("/v1/"):
        return f"{base_url}{default_path[3:]}"
    if base_url.endswith("/v1beta") and default_path.startswith("/v1beta/"):
        return f"{base_url}{default_path[7:]}"
    return f"{base_url}{default_path}"

def runninghub_endpoint_url(provider, path):
    base_url = str((provider or {}).get("base_url") or RUNNINGHUB_DEFAULT_BASE_URL).strip().rstrip("/")
    return f"{base_url}{path}"

def normalize_provider(item):
    provider_id = str(item.get("id") or "").strip().lower()
    if not PROVIDER_ID_RE.fullmatch(provider_id):
        raise HTTPException(status_code=400, detail=f"API 平台 ID 不合法：{provider_id or '(empty)'}")
    name = re.sub(r"\s+", " ", str(item.get("name") or provider_id).strip())[:60] or provider_id
    base_url = str(item.get("base_url") or "").strip().rstrip("/")
    if base_url and not re.match(r"^https?://", base_url):
        raise HTTPException(status_code=400, detail=f"{name} 的 Base URL 需要以 http:// 或 https:// 开头")
    protocol = str(item.get("protocol") or "openai").strip().lower()
    if protocol not in SUPPORTED_PROVIDER_PROTOCOLS:
        protocol = "openai"
    image_generation_endpoint = normalize_endpoint_override(item.get("image_generation_endpoint"), "文生图端口")
    image_edit_endpoint = normalize_endpoint_override(item.get("image_edit_endpoint"), "图生图/编辑端口")
    text_image_models, image_to_image_models, image_models = split_image_model_lists(
        item.get("image_models") or [],
        item.get("text_image_models") or [],
        item.get("image_to_image_models") or item.get("image_edit_models") or [],
    )
    return {
        "id": provider_id,
        "name": name,
        "base_url": base_url,
        "protocol": protocol,
        "image_generation_endpoint": image_generation_endpoint,
        "image_edit_endpoint": image_edit_endpoint,
        "enabled": bool(item.get("enabled", True)),
        "primary": bool(item.get("primary", False)),
        "text_image_models": text_image_models,
        "image_to_image_models": image_to_image_models,
        "image_models": image_models,
        "chat_models": model_list_from_values(item.get("chat_models") or []),
        "video_models": model_list_from_values(item.get("video_models") or []),
        "ms_loras": normalize_ms_loras(item.get("ms_loras") or []),
        "ms_defaults_version": int(item.get("ms_defaults_version") or 0),
        "rh_apps": normalize_runninghub_entries(item.get("rh_apps") or [], "app"),
        "rh_workflows": normalize_runninghub_entries(item.get("rh_workflows") or [], "workflow"),
    }

def load_api_providers():
    defaults = default_api_providers()
    if not os.path.exists(API_PROVIDERS_FILE):
        return defaults
    try:
        with open(API_PROVIDERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        providers = [normalize_provider(item) for item in raw if isinstance(item, dict)]
        return merge_default_api_providers(providers or defaults)
    except Exception as e:
        print(f"加载 API 平台配置失败: {e}")
        return defaults

def save_api_providers(providers):
    os.makedirs(DATA_DIR, exist_ok=True)
    with GLOBAL_CONFIG_LOCK:
        with open(API_PROVIDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(providers, f, ensure_ascii=False, indent=2)

def public_provider(provider):
    if provider.get("id") == "runninghub":
        try:
            provider = runninghub_provider_with_workflow_store(provider)
        except Exception:
            pass
    key = os.getenv(provider_key_env(provider["id"]), "")
    item = {
        **provider,
        "has_key": bool(key),
        "key_preview": mask_secret(key),
        "key_env": provider_key_env(provider["id"]),
    }
    if provider.get("id") == "runninghub":
        wallet_key = os.getenv(runninghub_wallet_key_env(), "")
        item.update({
            "has_wallet_key": bool(wallet_key),
            "wallet_key_preview": mask_secret(wallet_key),
            "wallet_key_env": runninghub_wallet_key_env(),
        })
    return item

def get_primary_provider_id(providers=None):
    """返回当前首选 provider 的 id；优先 primary=True 的，否则取第一个非 modelscope 的，再次取第一个。"""
    providers = providers if providers is not None else load_api_providers()
    primary = next((p for p in providers if p.get("primary") and p.get("enabled", True)), None)
    if primary:
        return primary["id"]
    non_ms = next((p for p in providers if p["id"] != "modelscope" and p.get("enabled", True)), None)
    if non_ms:
        return non_ms["id"]
    return providers[0]["id"] if providers else "modelscope"

def get_api_provider(provider_id="comfly"):
    providers = load_api_providers()
    target = (provider_id or "").strip().lower()
    # 兼容旧的 "comfly" 硬编码：若 comfly 不存在或未指定，回退到首选 provider
    if not target or not any(p["id"] == target for p in providers):
        target = get_primary_provider_id(providers)
    provider = next((p for p in providers if p["id"] == target), None)
    if not provider:
        raise HTTPException(status_code=400, detail=f"未找到 API 平台：{target}")
    if not provider.get("enabled", True):
        raise HTTPException(status_code=400, detail=f"API 平台已禁用：{provider.get('name') or target}")
    return provider

def get_api_provider_exact(provider_id: str):
    providers = load_api_providers()
    target = (provider_id or "").strip().lower()
    provider = next((p for p in providers if p["id"] == target), None)
    if not provider:
        raise HTTPException(status_code=400, detail=f"未找到 API 平台：{target or '(empty)'}。新增平台未保存时请使用当前表单拉取模型。")
    if not provider.get("enabled", True):
        raise HTTPException(status_code=400, detail=f"API 平台已禁用：{provider.get('name') or target}")
    return provider

def env_quote(value):
    text = str(value or "")
    if not text or re.search(r"\s|#|['\"]", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text

def update_env_values(updates):
    os.makedirs(os.path.dirname(API_ENV_FILE), exist_ok=True)
    lines = []
    if os.path.exists(API_ENV_FILE):
        with open(API_ENV_FILE, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    seen = set()
    next_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            next_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            next_lines.append(f"{key}={env_quote(updates[key])}")
            os.environ[key] = str(updates[key] or "")
            seen.add(key)
        else:
            next_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            next_lines.append(f"{key}={env_quote(value)}")
            os.environ[key] = str(value or "")
    with open(API_ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(next_lines).rstrip() + "\n")

BACKEND_LOCAL_LOAD = {addr: 0 for addr in COMFYUI_INSTANCES}

os.makedirs(INFINITE_CANVAS_HOME, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(OUTPUT_INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSET_LIBRARY_DIR, exist_ok=True)
os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(WORKFLOW_DIR, exist_ok=True)
os.makedirs(API_DIR, exist_ok=True)
os.makedirs(CONVERSATION_DIR, exist_ok=True)
os.makedirs(CANVAS_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

# --- Pydantic 模型 ---

def current_app_version():
    version_file = os.path.join(BASE_DIR, "VERSION")
    try:
        if os.path.exists(version_file):
            with open(version_file, "r", encoding="utf-8") as f:
                version = (f.read().strip().splitlines() or [""])[0].strip()
                if version:
                    return version
    except Exception:
        pass
    try:
        return time.strftime("%Y.%m.%d", time.localtime())
    except Exception:
        return ""

def versioned_static_html(html: str) -> str:
    version = current_app_version()
    if not version:
        return html
    safe_version = urllib.parse.quote(version, safe="._-")
    pattern = re.compile(r'(?P<prefix>(?:src|href)=["\']|@import\s+url\(["\'])(?P<url>/static/[^"\')?#]+(?:\.(?:js|css|html)))(?:\?v=[^"\')#]*)?', re.I)
    return pattern.sub(lambda m: f"{m.group('prefix')}{m.group('url')}?v={safe_version}", html)

def sync_static_html_versions():
    version = current_app_version()
    if not version:
        return
    safe_version = urllib.parse.quote(version, safe="._-")
    try:
        for name in os.listdir(STATIC_DIR):
            if not name.lower().endswith(".html"):
                continue
            path = os.path.join(STATIC_DIR, name)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                old = f.read()
            new = re.sub(r'([?&]v=)[^"\'`\s<>)]*', rf'\g<1>{safe_version}', old)
            if new != old:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(new)
    except Exception as e:
        print(f"同步静态页面版本号失败: {e}")

def static_html_response(filename: str):
    path = os.path.join(STATIC_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(
        versioned_static_html(html),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )

@app.get("/api/app-info")
def app_info():
    version = current_app_version()
    return {
        "version": version,
        "repo_url": GITHUB_REPO_URL,
        "version_url": GITHUB_VERSION_URL,
    }

def update_allowed_file(path: str) -> bool:
    path = str(path or "").replace("\\", "/").lstrip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        return False
    return path in {"main.py", "VERSION"} or path.startswith("static/")

# 缓存 GitHub Tree API 响应（含 ETag），减少 60 次/h 限流压力
GITHUB_TREE_CACHE: Dict[str, Any] = {"etag": "", "data": None, "expires_at": 0.0}

def github_json(url: str, use_etag_cache: bool = False):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Infinite-Canvas-Updater",
    }
    cache_key = url
    if use_etag_cache and cache_key == GITHUB_TREE_URL:
        if GITHUB_TREE_CACHE["data"] and time.time() < GITHUB_TREE_CACHE["expires_at"]:
            return GITHUB_TREE_CACHE["data"]
        if GITHUB_TREE_CACHE["etag"]:
            headers["If-None-Match"] = GITHUB_TREE_CACHE["etag"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            etag = resp.headers.get("ETag", "")
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            if use_etag_cache and cache_key == GITHUB_TREE_URL:
                GITHUB_TREE_CACHE.update({
                    "etag": etag,
                    "data": payload,
                    "expires_at": time.time() + 600,  # 10 分钟内复用
                })
            return payload
    except urllib.error.HTTPError as exc:
        # 304 表示对方树未变，沿用缓存
        if exc.code == 304 and use_etag_cache and GITHUB_TREE_CACHE["data"]:
            GITHUB_TREE_CACHE["expires_at"] = time.time() + 600
            return GITHUB_TREE_CACHE["data"]
        raise

def github_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Infinite-Canvas-Updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()

def download_github_update_files(files: List[str], staging_root: str) -> None:
    staging_root_abs = os.path.abspath(staging_root)
    for rel in files:
        safe_update_target(rel)
        raw_url = f"{GITHUB_RAW_ROOT}/{urllib.parse.quote(rel, safe='/')}"
        data = github_bytes(raw_url)
        stage_path = os.path.abspath(os.path.join(staging_root_abs, *rel.split("/")))
        if os.path.commonpath([staging_root_abs, stage_path]) != staging_root_abs:
            raise ValueError(f"更新暂存路径不安全：{rel}")
        os.makedirs(os.path.dirname(stage_path), exist_ok=True)
        with open(stage_path, "wb") as f:
            f.write(data)

def safe_update_target(path: str) -> str:
    rel = str(path or "").replace("\\", "/").lstrip("/")
    if not update_allowed_file(rel):
        raise ValueError(f"更新文件不在允许范围：{rel}")
    target = os.path.abspath(os.path.join(BASE_DIR, *rel.split("/")))
    base = os.path.abspath(BASE_DIR)
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"更新路径不安全：{rel}")
    return target

def safe_static_dir() -> str:
    target = os.path.abspath(STATIC_DIR)
    expected = os.path.abspath(os.path.join(BASE_DIR, "static"))
    base = os.path.abspath(BASE_DIR)
    if target != expected or os.path.commonpath([base, target]) != base:
        raise RuntimeError(f"static 路径不安全：{target}")
    return target

def schedule_self_restart(delay_seconds: int = 3) -> bool:
    """派生脱离父进程的小脚本，等几秒后启动启动服务脚本，并干掉当前 PID。"""
    delay = max(1, int(delay_seconds or 3))
    pid = os.getpid()
    try:
        if os.name == "nt":
            launcher = os.path.join(BASE_DIR, "启动服务.bat")
            if not os.path.exists(launcher):
                launcher = os.path.join(BASE_DIR, "start.bat")
            bat_path = os.path.join(BASE_DIR, "_self_restart.bat")
            log_path = os.path.join(BASE_DIR, "_self_restart.log")
            script = (
                "@echo off\r\n"
                "chcp 65001 >nul\r\n"
                "setlocal\r\n"
                f"set \"APP_DIR={BASE_DIR}\"\r\n"
                f"set \"LAUNCHER={launcher}\"\r\n"
                f"set \"LOG_FILE={log_path}\"\r\n"
                "echo [%date% %time%] restart scheduled >> \"%LOG_FILE%\"\r\n"
                f"timeout /t {delay} /nobreak >nul\r\n"
                "echo [%date% %time%] stopping old process >> \"%LOG_FILE%\"\r\n"
                f"taskkill /F /PID {pid} >nul 2>&1\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                "cd /d \"%APP_DIR%\"\r\n"
                "if exist \"%LAUNCHER%\" (\r\n"
                "  echo [%date% %time%] starting launcher: %LAUNCHER% >> \"%LOG_FILE%\"\r\n"
                "  start \"ComfyUI-API-Modelscope\" /D \"%APP_DIR%\" cmd /k call \"%LAUNCHER%\"\r\n"
                ") else (\r\n"
                "  echo [%date% %time%] launcher missing, fallback to python main.py >> \"%LOG_FILE%\"\r\n"
                "  if exist \"%APP_DIR%\\python\\python.exe\" (\r\n"
                "    start \"ComfyUI-API-Modelscope\" /D \"%APP_DIR%\" cmd /k \"\"%APP_DIR%\\python\\python.exe\" main.py\"\r\n"
                "  ) else (\r\n"
                "    start \"ComfyUI-API-Modelscope\" /D \"%APP_DIR%\" cmd /k python main.py\r\n"
                "  )\r\n"
                ")\r\n"
                "del \"%~f0\"\r\n"
            )
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(script)
            subprocess.Popen(
                ["cmd", "/c", bat_path],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            launcher = os.path.join(BASE_DIR, "mac-启动服务.command")
            if not os.path.exists(launcher):
                launcher = os.path.join(BASE_DIR, "start.sh")
            sh_path = os.path.join(BASE_DIR, "_self_restart.sh")
            script = (
                "#!/bin/sh\n"
                f"sleep {delay}\n"
                f"kill -9 {pid} 2>/dev/null\n"
                f"cd \"{BASE_DIR}\"\n"
                f"if [ -x \"{launcher}\" ]; then nohup \"{launcher}\" >/dev/null 2>&1 &\n"
                f"elif [ -f \"{launcher}\" ]; then nohup /bin/sh \"{launcher}\" >/dev/null 2>&1 &\n"
                "fi\n"
                "rm -- \"$0\"\n"
            )
            with open(sh_path, "w", encoding="utf-8") as f:
                f.write(script)
            os.chmod(sh_path, 0o755)
            subprocess.Popen(
                ["/bin/sh", sh_path],
                start_new_session=True,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True
    except Exception as exc:
        logging.exception("schedule_self_restart failed: %s", exc)
        return False

class UpdateRequest(BaseModel):
    auto_restart: bool = False
    restart_delay: int = 3

@app.post("/api/update-from-github")
def update_from_github(req: UpdateRequest = UpdateRequest()):
    if not UPDATE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="正在更新中，请稍后再试")
    staging_root = ""
    try:
        tree_data = github_json(GITHUB_TREE_URL, use_etag_cache=True)
        entries = tree_data.get("tree") or []
        static_files = []
        root_files = []
        for entry in entries:
            path = str(entry.get("path") or "").replace("\\", "/")
            if entry.get("type") == "blob" and update_allowed_file(path):
                if path.startswith("static/"):
                    static_files.append(path)
                else:
                    root_files.append(path)
        if "main.py" not in root_files:
            root_files.append("main.py")
        if "VERSION" not in root_files:
            root_files.append("VERSION")
        static_files = sorted(set(static_files))
        root_files = sorted(set(root_files))
        files = root_files + static_files
        if not static_files:
            raise RuntimeError("GitHub 未返回 static 文件，已取消更新")

        backup_root = os.path.join(DATA_DIR, "update_backups", time.strftime("%Y%m%d-%H%M%S"))
        staging_root = os.path.join(DATA_DIR, "update_staging", f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
        download_github_update_files(files, staging_root)

        updated = []
        for rel in root_files:
            target = safe_update_target(rel)
            if os.path.exists(target):
                backup_path = os.path.join(backup_root, *rel.split("/"))
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                shutil.copy2(target, backup_path)

        staged_static_dir = os.path.join(staging_root, "static")
        if not os.path.isdir(staged_static_dir):
            raise RuntimeError("GitHub static 暂存目录不存在，已取消更新")
        static_dir = safe_static_dir()
        backup_static_dir = os.path.join(backup_root, "static")
        if os.path.isdir(static_dir):
            os.makedirs(os.path.dirname(backup_static_dir), exist_ok=True)
            shutil.copytree(static_dir, backup_static_dir)
            shutil.rmtree(static_dir)
        try:
            shutil.copytree(staged_static_dir, static_dir)
        except Exception:
            if os.path.isdir(static_dir):
                shutil.rmtree(static_dir, ignore_errors=True)
            if os.path.isdir(backup_static_dir):
                shutil.copytree(backup_static_dir, static_dir)
            raise
        updated.extend(static_files)

        replaced_root_files = []
        try:
            for rel in root_files:
                target = safe_update_target(rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                temp_path = f"{target}.update_tmp"
                shutil.copy2(os.path.join(staging_root, *rel.split("/")), temp_path)
                os.replace(temp_path, target)
                replaced_root_files.append(rel)
                updated.append(rel)
        except Exception:
            for rel in reversed(replaced_root_files):
                backup_path = os.path.join(backup_root, *rel.split("/"))
                target = safe_update_target(rel)
                if os.path.exists(backup_path):
                    temp_path = f"{target}.rollback_tmp"
                    shutil.copy2(backup_path, temp_path)
                    os.replace(temp_path, target)
            if os.path.isdir(static_dir):
                shutil.rmtree(static_dir, ignore_errors=True)
            if os.path.isdir(backup_static_dir):
                shutil.copytree(backup_static_dir, static_dir)
            raise

        restart_scheduled = False
        if req.auto_restart and updated:
            restart_scheduled = schedule_self_restart(req.restart_delay)
        return {
            "ok": True,
            "updated": updated,
            "count": len(updated),
            "backup_dir": backup_root if os.path.exists(backup_root) else "",
            "restart_required": True,
            "restart_scheduled": restart_scheduled,
        }
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub 下载失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接 GitHub：{exc.reason}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"更新失败：{exc}") from exc
    finally:
        if staging_root and os.path.isdir(staging_root):
            shutil.rmtree(staging_root, ignore_errors=True)
        UPDATE_LOCK.release()

def list_update_backups() -> List[Dict[str, Any]]:
    root = os.path.join(DATA_DIR, "update_backups")
    if not os.path.isdir(root):
        return []
    items = []
    for name in sorted(os.listdir(root), reverse=True):
        bp = os.path.join(root, name)
        if not os.path.isdir(bp):
            continue
        file_count = 0
        for _, _, fs in os.walk(bp):
            file_count += len(fs)
        try:
            created_at = os.path.getmtime(bp)
        except OSError:
            created_at = 0.0
        items.append({
            "name": name,
            "file_count": file_count,
            "created_at": created_at,
        })
    return items

@app.get("/api/update-backups")
def get_update_backups():
    return {"backups": list_update_backups()}

class RollbackRequest(BaseModel):
    name: str = ""
    auto_restart: bool = False
    restart_delay: int = 3

@app.post("/api/update-rollback")
def rollback_update(req: RollbackRequest):
    if not req.name:
        raise HTTPException(status_code=400, detail="缺少备份名称")
    if not UPDATE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="正在更新中，请稍后再试")
    try:
        backup_root_abs = os.path.abspath(os.path.join(DATA_DIR, "update_backups"))
        backup_dir = os.path.abspath(os.path.join(backup_root_abs, req.name))
        if os.path.commonpath([backup_root_abs, backup_dir]) != backup_root_abs:
            raise HTTPException(status_code=400, detail="备份路径不安全")
        if not os.path.isdir(backup_dir):
            raise HTTPException(status_code=404, detail="备份不存在")
        restored = []
        skipped = []
        backup_static_dir = os.path.join(backup_dir, "static")
        if os.path.isdir(backup_static_dir):
            static_dir = safe_static_dir()
            if os.path.isdir(static_dir):
                shutil.rmtree(static_dir)
            try:
                shutil.copytree(backup_static_dir, static_dir)
            except Exception:
                if os.path.isdir(static_dir):
                    shutil.rmtree(static_dir, ignore_errors=True)
                raise
            for dirpath, _, filenames in os.walk(backup_static_dir):
                for fn in filenames:
                    src = os.path.join(dirpath, fn)
                    restored.append(os.path.relpath(src, backup_dir).replace("\\", "/"))
        for dirpath, _, filenames in os.walk(backup_dir):
            for fn in filenames:
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, backup_dir).replace("\\", "/")
                if rel.startswith("static/"):
                    continue
                if not update_allowed_file(rel):
                    skipped.append(rel)
                    continue
                try:
                    target = safe_update_target(rel)
                except ValueError:
                    skipped.append(rel)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                temp_path = f"{target}.rollback_tmp"
                with open(src, "rb") as fin, open(temp_path, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                os.replace(temp_path, target)
                restored.append(rel)
        restart_scheduled = False
        if req.auto_restart and restored:
            restart_scheduled = schedule_self_restart(req.restart_delay)
        return {
            "ok": True,
            "restored": restored,
            "skipped": skipped,
            "count": len(restored),
            "restart_required": True,
            "restart_scheduled": restart_scheduled,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"回滚失败：{exc}") from exc
    finally:
        UPDATE_LOCK.release()

class GenerateRequest(BaseModel):
    prompt: str = ""
    width: int = 1024
    height: int = 1024
    workflow_json: str = ZIMAGE_DEFAULT_WORKFLOW
    params: Dict[str, Any] = {}
    type: str = "zimage"
    client_id: str = ""
    convert_to_jpg: bool = False

class DeleteHistoryRequest(BaseModel):
    timestamp: float

class TokenRequest(BaseModel):
    token: str

def _comfy_link_target(value):
    if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], str):
        return value[0]
    return None

def _workflow_param_overrides(params, node_id, input_name):
    node_inputs = params.get(str(node_id)) if isinstance(params, dict) else None
    return isinstance(node_inputs, dict) and input_name in node_inputs

def _positive_prompt_text_targets(workflow):
    targets = []

    def visit_text_sources(node_id, seen=None):
        seen = seen or set()
        node_id = str(node_id)
        if node_id in seen or node_id not in workflow:
            return []
        seen.add(node_id)
        node = workflow.get(node_id) or {}
        inputs = node.get("inputs") or {}
        found = []
        if isinstance(inputs.get("text"), str):
            found.append((node_id, "text"))
        for value in inputs.values():
            source_id = _comfy_link_target(value)
            if source_id:
                found.extend(visit_text_sources(source_id, seen))
        return found

    for node_id, node in workflow.items():
        inputs = (node or {}).get("inputs") or {}
        positive_id = _comfy_link_target(inputs.get("positive"))
        if not positive_id:
            continue
        for target in visit_text_sources(positive_id):
            if target not in targets:
                targets.append(target)
    return targets

def apply_prompt_to_workflow(workflow, prompt, params):
    text = (prompt or "").strip()
    if not text:
        return None

    candidates = _positive_prompt_text_targets(workflow)
    if "23" in workflow:
        candidates.append(("23", "text"))
    for node_id, node in workflow.items():
        inputs = (node or {}).get("inputs") or {}
        class_type = str((node or {}).get("class_type") or "").lower()
        if isinstance(inputs.get("text"), str) and ("cliptextencode" in class_type or "text" in class_type):
            candidates.append((str(node_id), "text"))

    seen = set()
    for node_id, input_name in candidates:
        key = (str(node_id), input_name)
        if key in seen or str(node_id) not in workflow:
            continue
        seen.add(key)
        if _workflow_param_overrides(params, str(node_id), input_name):
            return key
        workflow[str(node_id)].setdefault("inputs", {})[input_name] = text
        return key
    return None

def apply_dimensions_to_workflow(workflow, width, height):
    updated = []
    for node_id, node in workflow.items():
        inputs = (node or {}).get("inputs") or {}
        class_type = str((node or {}).get("class_type") or "").lower()
        if "width" not in inputs or "height" not in inputs:
            continue
        if "latent" not in class_type and class_type not in {"emptylatentimage", "emptysd3latentimage"}:
            continue
        inputs["width"] = width
        inputs["height"] = height
        updated.append(str(node_id))
    if not updated and "144" in workflow:
        workflow["144"].setdefault("inputs", {})["width"] = width
        workflow["144"].setdefault("inputs", {})["height"] = height
        updated.append("144")
    return updated

class CloudGenRequest(BaseModel):
    prompt: str
    api_key: str = ""
    model: str = ""
    resolution: str = "1024x1024"
    type: str = "zimage"
    image_urls: List[str] = Field(default_factory=list)
    image_url: Optional[Any] = None
    loras: Optional[Any] = None
    client_id: Optional[str] = None

class CloudPollRequest(BaseModel):
    task_id: str
    api_key: str = ""
    client_id: Optional[str] = None

class AIReference(BaseModel):
    url: str = ""
    name: str = ""
    role: str = ""

class OnlineImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=ONLINE_IMAGE_PROMPT_MAX_LENGTH)
    provider_id: str = "comfly"
    model: str = ""
    size: str = "1024x1024"
    quality: str = "auto"
    n: int = 1
    reference_images: List[AIReference] = []

CANVAS_TASKS: Dict[str, Dict[str, Any]] = {}
CANVAS_TASK_LOCK = Lock()

class CanvasVideoRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=VIDEO_PROMPT_MAX_LENGTH)
    provider_id: str = "comfly"
    model: str = "veo3-fast"
    duration: int = 5
    aspect_ratio: str = "16:9"
    resolution: str = ""
    size: str = ""
    images: List[AIReference] = []
    videos: List[str] = []
    enhance_prompt: bool = False
    enable_upsample: bool = False
    watermark: bool = False
    seed: Optional[int] = None
    camerafixed: bool = False
    return_last_frame: bool = False
    generate_audio: bool = False

class RunningHubSubmitRequest(BaseModel):
    webappId: str = ""
    nodeInfoList: List[Dict[str, Any]] = []
    instanceType: str = ""
    useWallet: bool = False

class RunningHubWorkflowSubmitRequest(BaseModel):
    workflowId: str = ""
    nodeInfoList: List[Dict[str, Any]] = []
    workflow: Any = None
    useWallet: bool = False

class RunningHubUploadAssetRequest(BaseModel):
    url: str = ""
    useWallet: bool = False

class RunningHubWorkflowConfigField(BaseModel):
    id: str = ""
    nodeId: str = ""
    fieldName: str = ""
    fieldValue: str = ""
    fieldType: str = "TEXT"
    label: str = ""
    enabled: bool = True
    sourceFromUpstream: bool = True
    group: str = ""
    note: str = ""
    options: List[str] = Field(default_factory=list)
    random_enabled: bool = False
    min: Any = ""
    max: Any = ""
    step: Any = ""
    imageOrder: int = 0
    required: bool = False

class RunningHubWorkflowConfig(BaseModel):
    workflowId: str = ""
    title: str = ""
    description: str = ""
    fields: List[RunningHubWorkflowConfigField] = Field(default_factory=list)
    workflowJson: Dict[str, Any] = Field(default_factory=dict)
    optionalImageMode: str = "prune-workflow"
    raw: Dict[str, Any] = Field(default_factory=dict)

class ApiProviderPayload(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    protocol: str = "openai"
    image_generation_endpoint: str = ""
    image_edit_endpoint: str = ""
    enabled: bool = True
    primary: bool = False
    text_image_models: List[str] = []
    image_to_image_models: List[str] = []
    image_models: List[str] = []
    chat_models: List[str] = []
    video_models: List[str] = []
    ms_loras: List[Dict[str, Any]] = []
    ms_defaults_version: int = 0
    rh_apps: List[Dict[str, Any]] = []
    rh_workflows: List[Dict[str, Any]] = []
    api_key: Optional[str] = None
    wallet_api_key: Optional[str] = None
    clear_key: bool = False
    clear_wallet_key: bool = False

class ChatRequest(BaseModel):
    conversation_id: str = ""
    message: str = Field(min_length=1, max_length=LLM_MESSAGE_MAX_LENGTH)
    model: str = ""
    image_model: str = ""
    mode: str = "chat"
    size: str = "1024x1024"
    quality: str = "auto"
    reference_images: List[AIReference] = []
    provider: str = "comfly"
    ms_model: str = ""

class MsGenerateRequest(BaseModel):
    prompt: str
    api_key: str = ""
    model: str = "black-forest-labs/FLUX.2-klein-9B"
    image_urls: List[str] = []
    width: int = 0
    height: int = 0
    size: str = ""
    loras: Optional[Any] = None
    client_id: Optional[str] = None

class CanvasLLMRequest(BaseModel):
    message: str = Field(min_length=1, max_length=LLM_MESSAGE_MAX_LENGTH)
    system_prompt: str = ""
    model: str = ""
    messages: List[Dict[str, Any]] = []
    provider: str = "comfly"
    ms_model: str = ""
    images: List[str] = []   # 可以是 /output/*.png、/assets/*.png 本地路径 或 http(s) URL 或 data URL

class ConversationCreateRequest(BaseModel):
    title: str = "新对话"

class CanvasCreateRequest(BaseModel):
    title: str = "未命名画布"
    icon: str = "🧩"
    kind: str = "classic"

class CanvasSaveRequest(BaseModel):
    title: str = "未命名画布"
    icon: str = "🧩"
    nodes: List[Dict[str, Any]] = []
    connections: List[Dict[str, Any]] = []
    viewport: Dict[str, Any] = {}
    logs: List[Dict[str, Any]] = []
    settings: Dict[str, Any] = {}
    client_id: str = ""
    base_updated_at: int = 0

class CanvasAssetCheckRequest(BaseModel):
    urls: List[str] = []

class CanvasAssetDownloadRequest(BaseModel):
    urls: List[str] = []
    filename: str = "canvas-output-images.zip"

class SmartCanvasGroupExportItem(BaseModel):
    kind: str = ""
    url: str = ""
    text: str = ""
    name: str = ""

class SmartCanvasGroupExportRequest(BaseModel):
    folder: str = ""
    group_name: str = "group"
    items: List[SmartCanvasGroupExportItem] = []

class LocalImageImportRequest(BaseModel):
    path: str = ""
    paths: List[str] = Field(default_factory=list)

class AssetLibraryCategoryRequest(BaseModel):
    name: str = "新文件夹"
    type: str = "image"

class AssetLibraryAddRequest(BaseModel):
    category_id: str = ""
    url: str = ""
    name: str = ""

class AssetLibraryRenameRequest(BaseModel):
    name: str = ""

class LocalAssetFolderRequest(BaseModel):
    folder: str = ""
    parent: str = ""
    path: str = ""
    name: str = ""

class LocalAssetDeleteRequest(BaseModel):
    path: str = ""
    paths: List[str] = Field(default_factory=list)
    name: str = ""
    names: List[str] = Field(default_factory=list)

class LocalAssetCaptionRequest(BaseModel):
    path: str = ""
    name: str = ""
    names: List[str] = Field(default_factory=list)
    caption: str = ""
    model: str = ""
    provider: str = ""
    prompt: str = ""

class AssetLibraryCategoryRequest(BaseModel):
    name: str = "新文件夹"
    type: str = "image"
    library_id: str = ""

class AssetLibraryRequest(BaseModel):
    name: str = "新资产库"
    type: str = "asset"

class AssetLibraryAddRequest(BaseModel):
    category_id: str = ""
    url: str = ""
    name: str = ""
    library_id: str = ""

class AssetLibraryBatchItem(BaseModel):
    url: str = ""
    name: str = ""

class AssetLibraryBatchAddRequest(BaseModel):
    category_id: str = ""
    library_id: str = ""
    items: List[AssetLibraryBatchItem] = Field(default_factory=list)

class AssetLibraryRenameRequest(BaseModel):
    name: str = ""
    library_id: str = ""

class AssetLibraryBatchDeleteRequest(BaseModel):
    ids: List[str] = Field(default_factory=list)
    library_id: str = ""

class AssetLibraryBatchMoveRequest(BaseModel):
    ids: List[str] = Field(default_factory=list)
    target_category_id: str = ""
    library_id: str = ""

class SharedFolderRegister(BaseModel):
    path: str = ""
    name: str = ""

class SharedFolderImportRequest(BaseModel):
    folder_id: str = ""
    paths: List[str] = Field(default_factory=list)
    category_id: str = ""
    library_id: str = ""

class PromptLibraryRequest(BaseModel):
    name: str = "新提示词库"

class PromptLibraryItemRequest(BaseModel):
    title: str = ""
    prompt: str = ""
    negative: str = ""
    tags: List[str] = Field(default_factory=list)
    category_id: str = ""

class PromptLibraryBatchDeleteRequest(BaseModel):
    ids: List[str] = Field(default_factory=list)

class PromptLibraryCategoryRequest(BaseModel):
    name: str = "新分类"
    library_id: str = ""

# --- 负载均衡 ---

def check_images_exist(backend_addr, images):
    if not images: return True
    for img in images:
        try:
            url = f"http://{backend_addr}/view?filename={urllib.parse.quote(img)}&type=input"
            r = requests.get(url, stream=True, timeout=0.5)
            r.close()
            if r.status_code != 200: return False
        except: return False
    return True

MEDIA_INPUT_KEYS = ("image", "video", "audio", "mask", "filename", "file")
MEDIA_INPUT_EXT_RE = re.compile(r"\.(png|jpe?g|webp|gif|bmp|tiff?|mp4|webm|mov|m4v|avi|mkv|mp3|wav|m4a|aac|ogg|flac)(?:\?|$)", re.I)

def is_comfy_input_media_value(input_name: str, value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    key = str(input_name or "").lower()
    if any(token in key for token in MEDIA_INPUT_KEYS):
        return True
    return bool(MEDIA_INPUT_EXT_RE.search(value))

def collect_required_comfy_media(params: Dict[str, Any]) -> List[str]:
    required = []
    for node_inputs in (params or {}).values():
        if not isinstance(node_inputs, dict):
            continue
        for input_name, value in node_inputs.items():
            if is_comfy_input_media_value(input_name, value):
                required.append(value)
    return list(dict.fromkeys(required))

def get_best_backend(required_images: List[str] = None):
    best_backend = COMFYUI_INSTANCES[0]
    min_queue_size = float('inf')
    candidates_with_images = []
    candidates_others = []
    backend_stats = {}

    for addr in COMFYUI_INSTANCES:
        try:
            with urllib.request.urlopen(f"http://{addr}/queue", timeout=1) as response:
                data = json.loads(response.read())
                remote_load = len(data.get('queue_running', [])) + len(data.get('queue_pending', []))
                with LOAD_LOCK:
                    local_load = BACKEND_LOCAL_LOAD.get(addr, 0)
                effective_load = max(remote_load, local_load)
                has_images = check_images_exist(addr, required_images)
                backend_stats[addr] = {"load": effective_load, "has_images": has_images}
                if has_images:
                    candidates_with_images.append(addr)
                else:
                    candidates_others.append(addr)
        except Exception as e:
            print(f"Backend {addr} unreachable: {e}")
            continue

    target_candidates = candidates_with_images if candidates_with_images else candidates_others
    if not target_candidates:
        if candidates_others:
            target_candidates = candidates_others
        else:
            return COMFYUI_INSTANCES[0]

    for addr in target_candidates:
        load = backend_stats[addr]["load"]
        if load < min_queue_size:
            min_queue_size = load
            best_backend = addr

    return best_backend

# --- 辅助工具 ---

def download_image(comfy_address, comfy_url_path, prefix="studio_"):
    filename = f"{prefix}{uuid.uuid4().hex[:10]}.png"
    local_path = output_path_for(filename, "output")
    full_url = f"http://{comfy_address}{comfy_url_path}"
    try:
        with urllib.request.urlopen(full_url) as response, open(local_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        return output_url_for(filename, "output")
    except Exception as e:
        print(f"下载图片失败: {e}")
        if comfy_url_path.startswith("/view"):
            return comfy_url_path.replace("/view", "/api/view", 1)
        return full_url

def comfy_output_extension(item):
    filename = str((item or {}).get("filename") or "")
    ext = os.path.splitext(filename)[1].lower()
    if ext in {
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
        ".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv",
        ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
        ".txt", ".json", ".csv", ".srt", ".vtt", ".md",
    }:
        return ext
    fmt = str((item or {}).get("format") or "").lower()
    if "mpeg" in fmt or "mp3" in fmt:
        return ".mp3"
    if "wav" in fmt or "wave" in fmt:
        return ".wav"
    if "ogg" in fmt:
        return ".ogg"
    if "flac" in fmt:
        return ".flac"
    if "text" in fmt or "plain" in fmt:
        return ".txt"
    if "json" in fmt:
        return ".json"
    if "webm" in fmt:
        return ".webm"
    if "quicktime" in fmt or "mov" in fmt:
        return ".mov"
    if "mp4" in fmt or "h264" in fmt or "video" in fmt:
        return ".mp4"
    return ext or ".bin"

def is_video_output_item(item):
    ext = comfy_output_extension(item)
    fmt = str((item or {}).get("format") or "").lower()
    return ext in {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"} or "video" in fmt

def comfy_output_kind(item):
    ext = comfy_output_extension(item)
    fmt = str((item or {}).get("format") or "").lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"} or "image" in fmt:
        return "image"
    if ext in {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"} or "video" in fmt:
        return "video"
    if ext in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"} or "audio" in fmt or "sound" in fmt:
        return "audio"
    if ext in {".txt", ".json", ".csv", ".srt", ".vtt", ".md"} or "text" in fmt or "json" in fmt:
        return "text"
    return "file"

def download_comfy_output(comfy_address, item, prefix="studio_"):
    ext = comfy_output_extension(item)
    filename = f"{prefix}{uuid.uuid4().hex[:10]}{ext}"
    local_path = output_path_for(filename, "output")
    subfolder = urllib.parse.quote(str(item.get("subfolder") or ""))
    file_type = urllib.parse.quote(str(item.get("type") or "output"))
    comfy_url_path = f"/view?filename={urllib.parse.quote(str(item['filename']))}&subfolder={subfolder}&type={file_type}"
    full_url = f"http://{comfy_address}{comfy_url_path}"
    try:
        with urllib.request.urlopen(full_url) as response, open(local_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        return output_url_for(filename, "output")
    except Exception as e:
        print(f"下载 ComfyUI 输出失败: {e}")
        if comfy_url_path.startswith("/view"):
            return comfy_url_path.replace("/view", "/api/view", 1)
        return full_url

def save_comfy_text_output(value, prefix="studio_", name=""):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    stem = sanitize_export_filename(name or "comfy_text.txt", "comfy_text.txt")
    _, ext = os.path.splitext(stem)
    if ext.lower() not in {".txt", ".json", ".csv", ".srt", ".vtt", ".md"}:
        stem += ".txt"
    filename = f"{prefix}{uuid.uuid4().hex[:10]}_{stem}"
    path = output_path_for(filename, "output")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return output_url_for(filename, "output")

def comfy_text_values_from_output(node_output):
    values = []
    text_keys = ("text", "texts", "prompt", "prompts", "string", "strings", "caption", "captions")
    for key in text_keys:
        if key not in node_output:
            continue
        value = node_output.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                text = item.get("text") or item.get("prompt") or item.get("caption") or item.get("value")
                name = item.get("filename") or item.get("name") or f"{key}.txt"
            else:
                text = item
                name = f"{key}.txt"
            if text is None:
                continue
            text = str(text)
            if text.strip():
                values.append((text, name))
    return values

def collect_comfy_file_items(node_output):
    items = []
    for key, value in (node_output or {}).items():
        if key in {"text", "texts", "prompt", "prompts", "string", "strings", "caption", "captions"}:
            continue
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if isinstance(item, dict) and item.get("filename"):
                items.append((key, item))
    return items

def save_to_history(record):
    with HISTORY_LOCK:
        history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except: pass
        if "timestamp" not in record:
            record["timestamp"] = time.time()
        history.insert(0, record)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history[:5000], f, ensure_ascii=False, indent=4)
    try:
        record_generated_assets(record)
    except Exception as e:
        print(f"资产库登记失败: {e}")

def get_comfy_history(comfy_address, prompt_id):
    try:
        with urllib.request.urlopen(f"http://{comfy_address}/history/{prompt_id}") as response:
            return json.loads(response.read())
    except Exception as e:
        return {}

def safe_user_id(user_id, request: Request):
    candidate = (user_id or "").strip()
    if not candidate and request.client:
        candidate = f"ip-{request.client.host}"
    if not candidate:
        candidate = "anonymous"
    candidate = re.sub(r"[^a-zA-Z0-9_.-]", "-", candidate)[:80].strip(".-")
    return candidate or "anonymous"

def user_dir(user_id):
    path = os.path.join(CONVERSATION_DIR, user_id)
    os.makedirs(path, exist_ok=True)
    return path

def conversation_path(user_id, conversation_id):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", conversation_id or "")
    if not cleaned:
        raise HTTPException(status_code=400, detail="无效的对话 ID")
    return os.path.join(user_dir(user_id), f"{cleaned}.json")

def now_ms():
    return int(time.time() * 1000)

def save_conversation(user_id, conversation):
    with CONVERSATION_LOCK:
        path = conversation_path(user_id, conversation["id"])
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(conversation, f, ensure_ascii=False, indent=2)

def new_conversation(user_id, title="新对话"):
    timestamp = now_ms()
    conversation = {
        "id": uuid.uuid4().hex,
        "title": (title or "新对话")[:80],
        "created_at": timestamp,
        "updated_at": timestamp,
        "messages": [],
    }
    save_conversation(user_id, conversation)
    return conversation

def load_conversation(user_id, conversation_id):
    path = conversation_path(user_id, conversation_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="对话不存在")
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def list_conversations(user_id):
    records = []
    for filename in os.listdir(user_dir(user_id)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(user_dir(user_id), filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        messages = data.get("messages", [])
        last_message = next((m for m in reversed(messages) if m.get("role") != "system"), None)
        records.append({
            "id": data.get("id"),
            "title": data.get("title", "新对话"),
            "created_at": data.get("created_at", 0),
            "updated_at": data.get("updated_at", 0),
            "last_message": (last_message or {}).get("content", ""),
        })
    return sorted(records, key=lambda item: item["updated_at"], reverse=True)

def canvas_path(canvas_id):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", canvas_id or "")
    if not cleaned:
        raise HTTPException(status_code=400, detail="无效的画布 ID")
    return os.path.join(CANVAS_DIR, f"{cleaned}.json")

def save_canvas(canvas):
    canvas["updated_at"] = now_ms()
    with CANVAS_LOCK:
        with open(canvas_path(canvas["id"]), 'w', encoding='utf-8') as f:
            json.dump(canvas, f, ensure_ascii=False, indent=2)

def normalize_canvas_kind(kind="classic"):
    return "smart" if str(kind or "").strip().lower() == "smart" else "classic"

def new_canvas(title="未命名画布", icon="layers", kind="classic"):
    timestamp = now_ms()
    canvas_kind = normalize_canvas_kind(kind)
    canvas = {
        "id": uuid.uuid4().hex,
        "title": (title or ("智能画布" if canvas_kind == "smart" else "未命名画布"))[:80],
        "icon": (icon or ("sparkles" if canvas_kind == "smart" else "🧩"))[:32],
        "kind": canvas_kind,
        "created_at": timestamp,
        "updated_at": timestamp,
        "nodes": [],
        "connections": [],
        "viewport": {"x": 0, "y": 0, "scale": 1},
    }
    save_canvas(canvas)
    return canvas

def load_canvas(canvas_id):
    path = canvas_path(canvas_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画布不存在")
    with open(path, 'r', encoding='utf-8') as f:
        canvas = json.load(f)
    if canvas.get("deleted_at"):
        raise HTTPException(status_code=404, detail="画布已在回收站")
    return canvas

def load_canvas_any(canvas_id):
    path = canvas_path(canvas_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画布不存在")
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def canvas_record(data):
    return {
        "id": data.get("id"),
        "title": data.get("title", "未命名画布"),
        "icon": data.get("icon", "🧩"),
        "kind": normalize_canvas_kind(data.get("kind")),
        "created_at": data.get("created_at", 0),
        "updated_at": data.get("updated_at", 0),
        "deleted_at": data.get("deleted_at", 0),
        "node_count": len(data.get("nodes", [])),
    }

def cleanup_expired_canvas_trash():
    cutoff = now_ms() - CANVAS_TRASH_RETENTION_MS
    with CANVAS_LOCK:
        for filename in os.listdir(CANVAS_DIR):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(CANVAS_DIR, filename)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                deleted_at = int(data.get("deleted_at") or 0)
                if deleted_at and deleted_at < cutoff:
                    os.remove(path)
            except Exception:
                continue

def iter_canvas_records(include_deleted=False):
    cleanup_expired_canvas_trash()
    records = []
    for filename in os.listdir(CANVAS_DIR):
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(CANVAS_DIR, filename), 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        is_deleted = bool(data.get("deleted_at"))
        if include_deleted != is_deleted:
            continue
        records.append(canvas_record(data))
    return records

def list_canvases():
    records = iter_canvas_records(include_deleted=False)
    return sorted(records, key=lambda item: item["updated_at"], reverse=True)

def list_deleted_canvases():
    records = iter_canvas_records(include_deleted=True)
    return sorted(records, key=lambda item: item["deleted_at"], reverse=True)

def display_title(text):
    title = re.sub(r"\s+", " ", text or "").strip()
    return title[:24] or "新对话"

def resolve_chat_provider(provider: str, model: str, ms_model: str):
    if provider == "modelscope":
        if not MODELSCOPE_API_KEY:
            raise HTTPException(status_code=400, detail="未配置 MODELSCOPE_API_KEY，请在 API/.env 中填写。")
        base = MODELSCOPE_CHAT_BASE_URL
        hdrs = {"Authorization": f"Bearer {MODELSCOPE_API_KEY}", "Content-Type": "application/json"}
        mdl = selected_model(ms_model or model, MODELSCOPE_CHAT_MODELS[0] if MODELSCOPE_CHAT_MODELS else "MiniMax/MiniMax-M2.7")
        return base, hdrs, mdl
    api_provider = get_api_provider(provider or "")
    base_root = (api_provider.get("base_url") or AI_BASE_URL).rstrip("/")
    if not base_root:
        raise HTTPException(status_code=400, detail=f"{api_provider.get('name') or api_provider['id']} 未配置 Base URL")
    base = base_root if base_root.endswith("/v1") else base_root + "/v1"
    hdrs = api_headers(provider=api_provider)
    default_model = (api_provider.get("chat_models") or [CHAT_MODEL])[0]
    mdl = selected_model(model, default_model)
    return base, hdrs, mdl

def api_headers(json_body=True, provider=None):
    if provider:
        key_env = provider_key_env(provider["id"])
        api_key = os.getenv(key_env, "")
        provider_name = provider.get("name") or provider["id"]
        if not api_key:
            raise HTTPException(status_code=400, detail=f"未配置 {provider_name} 的 API Key，请在 API 平台管理中填写。")
    else:
        api_key = AI_API_KEY
        if not api_key:
            raise HTTPException(status_code=400, detail="未配置 COMFLY_API_KEY，请在 API/.env 中填写。")
    if provider and provider_protocol(provider) == "gemini":
        headers = {"Accept": "application/json", "x-goog-api-key": api_key}
    else:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers

def selected_model(requested, fallback):
    model = (requested or fallback).strip()
    if not model:
        raise HTTPException(status_code=400, detail="模型名称不能为空")
    if len(model) > 240 or any(ord(ch) < 32 or ord(ch) == 127 for ch in model):
        raise HTTPException(status_code=400, detail=f"模型名称不合法：{model}")
    return model

def is_modelscope_image_edit_model(model):
    value = str(model or "").strip()
    return value in MODELSCOPE_IMAGE_EDIT_MODELS or bool(re.search(r"(^|[-_/])image[-_]?edit([-_/]|$)", value, re.I))

def modelscope_size(value, fallback="1024x1024"):
    size = str(value or fallback).strip().lower().replace("*", "x")
    if re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        return size
    raise HTTPException(status_code=400, detail=f"ModelScope size 格式不正确：{value or fallback}，应为 WxH，例如 1024x1024")

def unwrap_apimart_response(raw):
    """APIMart 将标准 OpenAI 响应包在 {"code":200,"data":{...}} 里；如果检测到就解包。"""
    if isinstance(raw, dict) and "data" in raw and isinstance(raw.get("data"), dict) and "choices" not in raw:
        return raw["data"]
    return raw

def text_from_chat_response(data):
    data = unwrap_apimart_response(data)
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
        return "\n".join(part for part in parts if part)
    return str(content)

def text_delta_from_chat_chunk(data):
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
        return "".join(parts)
    return str(content) if content else ""

def sse_event(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def extract_image(data):
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData") or part.get("inline_data") or {}
                if not isinstance(inline, dict):
                    continue
                value = inline.get("data")
                if value:
                    return {
                        "type": "b64",
                        "value": value,
                        "mime_type": inline.get("mimeType") or inline.get("mime_type") or "image/png",
                    }
    if isinstance(data.get("data"), dict) and isinstance(data["data"].get("result"), dict):
        data = data["data"]
    if isinstance(data.get("result"), dict):
        result_images = data["result"].get("images") or []
        if result_images:
            first = result_images[0]
            url = first.get("url")
            if isinstance(url, list) and url:
                return {"type": "url", "value": url[0]}
            if isinstance(url, str) and url:
                return {"type": "url", "value": url}
    if isinstance(data.get("data"), dict) and isinstance(data["data"].get("data"), dict):
        data = data["data"]["data"]
    images = data.get("data") or []
    if not isinstance(images, list) or not images:
        raise HTTPException(status_code=502, detail="生图接口没有返回图片数据")
    first = images[0]
    if first.get("url"):
        return {"type": "url", "value": first["url"]}
    if first.get("b64_json"):
        return {"type": "b64", "value": first["b64_json"]}
    raise HTTPException(status_code=502, detail="无法识别生图接口返回格式")

def extract_task_id(data):
    if data.get("task_id"):
        return str(data["task_id"])
    if data.get("id") and str(data.get("id", "")).startswith("task"):
        return str(data["id"])
    nested = data.get("data")
    if isinstance(nested, list) and nested:
        first = nested[0]
        if isinstance(first, dict):
            return extract_task_id(first)
    if isinstance(nested, dict):
        return extract_task_id(nested)
    return None

def images_api_unsupported(response):
    text = str(getattr(response, "text", "") or "").lower()
    return "images api is not supported" in text or "not supported for this platform" in text

def provider_protocol(provider):
    return str((provider or {}).get("protocol") or "openai").strip().lower()

def is_apimart_provider(provider):
    base_url = str((provider or {}).get("base_url") or "").lower()
    return provider_protocol(provider) == "apimart" or "apimart.ai" in base_url

def is_gemini_provider(provider):
    return provider_protocol(provider) == "gemini"

def is_volcengine_provider(provider):
    return provider_protocol(provider) == "volcengine"

def is_runninghub_provider(provider):
    return provider_protocol(provider) == "runninghub" or str((provider or {}).get("id") or "").strip().lower() == "runninghub"

async def wait_for_image_task(client, task_id, provider=None):
    base_url = (provider.get("base_url") if provider else AI_BASE_URL).rstrip("/")
    is_apimart = is_apimart_provider(provider)
    if is_apimart:
        task_url = f"{base_url}/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/tasks/{task_id}"
    else:
        task_url = f"{base_url}/images/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/images/tasks/{task_id}"
    timeout = APIMART_IMAGE_TASK_TIMEOUT if is_apimart else IMAGE_TASK_TIMEOUT
    interval = APIMART_IMAGE_POLL_INTERVAL if is_apimart else IMAGE_POLL_INTERVAL
    initial_delay = APIMART_IMAGE_INITIAL_POLL_DELAY if is_apimart else 0
    deadline = time.monotonic() + timeout
    last_payload = {}
    while time.monotonic() < deadline:
        if initial_delay:
            await asyncio.sleep(min(initial_delay, max(0.0, deadline - time.monotonic())))
            initial_delay = 0
            if time.monotonic() >= deadline:
                break
        response = await client.get(task_url, headers=api_headers(provider=provider))
        response.raise_for_status()
        last_payload = response.json()
        task_data = last_payload.get("data") if isinstance(last_payload.get("data"), dict) else last_payload
        status = str(task_data.get("status") or task_data.get("task_status") or "").upper()
        if status in {"SUCCESS", "SUCCEED", "SUCCEEDED", "COMPLETED", "COMPLETE", "DONE", "FINISHED", "OK", "READY"}:
            return last_payload
        if status in {"FAILURE", "FAILED", "FAIL", "ERROR", "ERRORED", "CANCELED", "CANCELLED", "TIMEOUT", "REJECTED", "EXPIRED"}:
            error = task_data.get("error") if isinstance(task_data.get("error"), dict) else {}
            reason = task_data.get("fail_reason") or task_data.get("message") or error.get("message") or last_payload.get("message") or "生图任务失败"
            raise HTTPException(status_code=502, detail=f"生图任务失败：{reason}")
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    raise HTTPException(status_code=504, detail=f"生图任务超时（已等待 {int(timeout)} 秒），task_id={task_id}")

def output_storage(category="output"):
    return (OUTPUT_INPUT_DIR, "input") if category == "input" else (OUTPUT_OUTPUT_DIR, "output")

def output_url_for(filename, category="output"):
    _, subdir = output_storage(category)
    return f"/assets/{subdir}/{filename}"

def output_path_for(filename, category="output"):
    folder, _ = output_storage(category)
    return os.path.join(folder, filename)

def clamp_int(value, default, min_value, max_value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))

def clamp_float(value, default, min_value, max_value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))

def normalized_choice(value, allowed, default):
    value = str(value or "").strip().lower()
    return value if value in allowed else default

def normalized_bool(value, default=False):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default

def normalize_blur_kernel(value):
    kernel = clamp_int(value, 3, 1, 15)
    if kernel > 1 and kernel % 2 == 0:
        kernel += 1
    return min(kernel, 15)

def preprocess_to_gray_layers(input_path, output_path, layers=12, bg_threshold=248, blur_kernel=3):
    try:
        import cv2
        import numpy as np
    except Exception as e:
        raise RuntimeError("preprocess_missing") from e

    gray = cv2.imread(input_path, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise RuntimeError("preprocess_read_failed")

    blur_kernel = normalize_blur_kernel(blur_kernel)
    if blur_kernel > 1:
        gray = cv2.GaussianBlur(gray, (blur_kernel, blur_kernel), 0)

    layers = clamp_int(layers, 12, 2, 32)
    bg_threshold = clamp_int(bg_threshold, 248, 0, 255)
    mask = gray < bg_threshold
    values = gray[mask]
    if values.size == 0:
        out = np.full_like(gray, 255)
    else:
        quantiles = np.linspace(0.04, 0.95, layers)
        levels = np.quantile(values, quantiles).astype(np.uint8)
        levels = np.unique(levels)
        if levels.size == 0:
            levels = np.array([0], dtype=np.uint8)
        out = np.full_like(gray, 255)
        palette_lookup = levels[
            np.abs(
                np.arange(256, dtype=np.int16)[:, None] -
                levels.astype(np.int16)[None, :]
            ).argmin(axis=1)
        ]
        out[mask] = palette_lookup[gray[mask]]

    out_rgb = cv2.cvtColor(out, cv2.COLOR_GRAY2RGB)
    if not cv2.imwrite(output_path, out_rgb):
        raise RuntimeError("preprocess_write_failed")

def svg_fill_is_white(value):
    text = str(value or "").strip().lower()
    if not text or text == "none":
        return False
    text = text.replace(" ", "")
    if text in {"#fff", "#ffffff", "white"}:
        return True
    rgb_match = re.match(r"rgba?\((\d+),(\d+),(\d+)(?:,[\d.]+)?\)", text)
    if rgb_match:
        return all(int(part) >= 248 for part in rgb_match.groups())
    if text.startswith("#") and len(text) in {7, 9}:
        try:
            channels = [int(text[i:i+2], 16) for i in (1, 3, 5)]
            return all(channel >= 248 for channel in channels)
        except ValueError:
            return False
    return False

def svg_element_fill(element):
    fill = element.attrib.get("fill")
    style = element.attrib.get("style", "")
    if style:
        for item in style.split(";"):
            if ":" not in item:
                continue
            key, value = item.split(":", 1)
            if key.strip().lower() == "fill":
                fill = value
    return fill

def remove_white_svg_elements(svg_path):
    import xml.etree.ElementTree as ET
    removable = {"path", "rect", "polygon", "polyline", "circle", "ellipse"}
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        namespace = ""
        if root.tag.startswith("{") and "}" in root.tag:
            namespace = root.tag[1:].split("}", 1)[0]
            ET.register_namespace("", namespace)

        def local_name(tag):
            return tag.split("}", 1)[-1] if "}" in tag else tag

        def walk(parent):
            for child in list(parent):
                if local_name(child.tag) in removable and svg_fill_is_white(svg_element_fill(child)):
                    parent.remove(child)
                else:
                    walk(child)

        walk(root)
        tree.write(svg_path, encoding="utf-8", xml_declaration=True)
    except Exception as e:
        print(f"SVG white background removal failed: {e}")

def vectorize_image_bytes(content, original_name, options):
    try:
        import vtracer
    except Exception as e:
        raise RuntimeError("vtracer_missing") from e

    tmp_path = None
    processed_path = None
    filename = f"vector_{uuid.uuid4().hex[:12]}.svg"
    output_path = output_path_for(filename, "output")
    try:
        with Image.open(BytesIO(content)) as img:
            img.load()
            if img.mode not in ("RGB", "RGBA", "L"):
                has_alpha = img.mode in ("LA", "PA") or (img.mode == "P" and "transparency" in img.info)
                img = img.convert("RGBA" if has_alpha else "RGB")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp_path = tmp.name
            img.save(tmp_path, "PNG")

        input_path = tmp_path
        if options["preprocess_enabled"]:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                processed_path = tmp.name
            preprocess_to_gray_layers(
                tmp_path,
                processed_path,
                layers=options["gray_layers"],
                bg_threshold=options["bg_threshold"],
                blur_kernel=options["blur_kernel"],
            )
            input_path = processed_path

        vtracer.convert_image_to_svg_py(
            input_path,
            output_path,
            colormode=options["colormode"],
            hierarchical=options["hierarchical"],
            mode=options["mode"],
            filter_speckle=options["filter_speckle"],
            color_precision=options["color_precision"],
            layer_difference=options["layer_difference"],
            corner_threshold=options["corner_threshold"],
            length_threshold=options["length_threshold"],
            max_iterations=options["max_iterations"],
            splice_threshold=options["splice_threshold"],
            path_precision=options["path_precision"],
        )
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise RuntimeError("empty_svg")
        if options["background_mode"] == "remove":
            remove_white_svg_elements(output_path)
        return {
            "url": output_url_for(filename, "output"),
            "name": filename,
            "source_name": original_name or "image",
            "kind": "image",
            "mime_type": "image/svg+xml",
            "preprocessed": options["preprocess_enabled"],
            "background_mode": options["background_mode"],
        }
    finally:
        for path in (tmp_path, processed_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

def output_file_from_url(url):
    if isinstance(url, dict):
        url = url.get("url", "")
    if not url or not (url.startswith("/output/") or url.startswith("/assets/")):
        return None
    clean = urllib.parse.unquote(url.split("?", 1)[0]).replace("\\", "/")
    if clean.startswith("/assets/"):
        root = ASSETS_DIR
        rel = clean[len("/assets/"):]
    else:
        root = OUTPUT_DIR
        rel = clean[len("/output/"):]
    rel = rel.lstrip("/")
    if not rel:
        return None
    path = os.path.abspath(os.path.join(root, rel))
    output_root = os.path.abspath(root)
    if os.path.commonpath([output_root, path]) != output_root or not os.path.exists(path):
        return None
    return path

def origin_from_url(value):
    parsed = urllib.parse.urlparse(str(value or ""))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()

def ensure_same_origin_request(request: Request):
    host = str(request.headers.get("host") or "").lower()
    expected = f"{request.url.scheme}://{host}".lower() if host else ""
    origin = origin_from_url(request.headers.get("origin", ""))
    referer = origin_from_url(request.headers.get("referer", ""))
    actual = origin or referer
    if expected and actual != expected:
        raise HTTPException(status_code=403, detail="只允许从当前页面导入本地图片")

def normalize_local_image_path(value):
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise HTTPException(status_code=400, detail="本地图片路径为空")
    if text.lower().startswith("file:"):
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme.lower() != "file":
            raise HTTPException(status_code=400, detail="只支持本地图片路径")
        if parsed.netloc and re.match(r"^[a-zA-Z]:$", parsed.netloc) and os.name == "nt":
            path = f"{parsed.netloc}{urllib.request.url2pathname(parsed.path or '')}"
        elif parsed.netloc and parsed.netloc.lower() not in ("localhost",):
            raise HTTPException(status_code=400, detail="只支持本机图片路径")
        else:
            path = urllib.request.url2pathname(parsed.path or "")
    else:
        path = text
    path = path.strip().strip('"').strip("'")
    if re.match(r"^/[a-zA-Z]:[\\/]", path):
        path = path[1:]
    if re.match(r"^[a-zA-Z]:[\\/]", path):
        return os.path.abspath(path)
    if path.startswith("/") and os.name != "nt":
        return os.path.abspath(path)
    raise HTTPException(status_code=400, detail="只支持本机绝对图片路径")

def import_local_image_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in LOCAL_IMAGE_IMPORT_EXTS:
        raise HTTPException(status_code=400, detail="仅支持 PNG、JPG、JPEG、WEBP、GIF 图片")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="本地图片不存在或无法读取")
    try:
        size = os.path.getsize(path)
    except OSError:
        raise HTTPException(status_code=404, detail="本地图片不存在或无法读取")
    if size <= 0:
        raise HTTPException(status_code=400, detail="本地图片为空")
    if size > LOCAL_IMAGE_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="本地图片过大，请使用 50MB 以内的图片")
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception:
        raise HTTPException(status_code=400, detail="文件不是可识别的图片")
    filename = f"ai_ref_{uuid.uuid4().hex[:12]}{ext}"
    dest = output_path_for(filename, "input")
    try:
        shutil.copyfile(path, dest)
    except OSError:
        raise HTTPException(status_code=500, detail="导入本地图片失败")
    return {"url": output_url_for(filename, "input"), "name": os.path.basename(path) or filename, "kind": "image"}

def default_asset_library():
    return {
        "categories": [
            {"id": "characters", "name": "角色", "type": "image", "items": []},
            {"id": "scenes", "name": "场景", "type": "image", "items": []},
            *[dict(cat, items=[]) for cat in GENERATED_ASSET_CATEGORIES],
            {"id": "workflows", "name": "工作流", "type": "workflow", "items": []},
        ],
        "updated_at": now_ms(),
    }

def ensure_generated_asset_categories(lib):
    cats = lib.get("categories") if isinstance(lib.get("categories"), list) else []
    lib["categories"] = cats
    by_id = {cat.get("id"): cat for cat in cats if isinstance(cat, dict)}
    changed = False
    for template in GENERATED_ASSET_CATEGORIES:
        cat = by_id.get(template["id"])
        if not cat:
            cats.append(dict(template, items=[]))
            changed = True
            continue
        if cat.get("name") != template["name"]:
            cat["name"] = template["name"]
            changed = True
        if cat.get("type") != "image":
            cat["type"] = "image"
            changed = True
        if not isinstance(cat.get("items"), list):
            cat["items"] = []
            changed = True
    return changed

def generated_asset_category_id(record):
    record_type = str((record or {}).get("type") or "").strip().lower()
    if record_type in GENERATED_ASSET_CATEGORY_BY_TYPE:
        return GENERATED_ASSET_CATEGORY_BY_TYPE[record_type]
    workflow = str((record or {}).get("workflow_json") or "").strip().lower()
    if workflow:
        if "enhance" in workflow or "upscale" in workflow:
            return "generated_enhance"
        if "klein" in workflow:
            return "generated_edit"
        if "2511" in workflow:
            return "generated_angle"
        if "ltx" in workflow:
            return "generated_canvas"
        if "z-image" in workflow or "zimage" in workflow:
            return "generated_zimage"
    return "generated_other"

def generated_asset_created_at(record):
    try:
        value = float((record or {}).get("timestamp") or time.time())
    except (TypeError, ValueError):
        value = time.time()
    if value < 100000000000:
        value *= 1000
    return int(value)

def is_local_generated_image_url(value):
    if not isinstance(value, str) or not value:
        return False
    path = output_file_from_url(value)
    return bool(path and content_type_for_path(path).startswith("image/"))

def generated_asset_urls(record):
    urls = []
    seen = set()

    def add(value):
        if not value:
            return
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        if isinstance(value, dict):
            kind = str(value.get("kind") or value.get("type") or "").lower()
            url = value.get("url") or value.get("path") or value.get("src") or value.get("uri")
            if url and (not kind or kind == "image" or is_local_generated_image_url(url)):
                add(url)
            for key in ("images", "items", "outputs", "urls", "data", "result"):
                add(value.get(key))
            return
        if isinstance(value, str) and is_local_generated_image_url(value):
            clean = value.split("?", 1)[0]
            if clean not in seen:
                seen.add(clean)
                urls.append(value)

    for key in ("images", "items", "outputs", "urls", "data", "result", "output", "url"):
        add((record or {}).get(key))
    return urls

def asset_library_source_keys(lib):
    keys = set()
    for cat in lib.get("categories", []):
        if not isinstance(cat, dict):
            continue
        for item in cat.get("items", []):
            if not isinstance(item, dict):
                continue
            for key in ("source_url", "url"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    keys.add(value.split("?", 1)[0])
    return keys

def generated_asset_item(url, record, category_id):
    path = output_file_from_url(url)
    if not path or not content_type_for_path(path).startswith("image/"):
        return None
    prompt = str((record or {}).get("prompt") or "").strip()
    basename = os.path.basename(path) or "asset"
    title = re.sub(r"\s+", " ", prompt)[:48].strip() if prompt else ""
    name = sanitize_asset_name(title or os.path.splitext(basename)[0], "asset")
    item = {
        "id": f"asset_{uuid.uuid4().hex[:12]}",
        "name": name,
        "url": url,
        "source_url": url,
        "source": str((record or {}).get("type") or category_id),
        "kind": "image",
        "auto_generated": True,
        "created_at": generated_asset_created_at(record),
    }
    for key in ("prompt", "model", "workflow_json", "task_id", "prompt_id", "provider_id", "provider_name"):
        value = (record or {}).get(key)
        if value is not None:
            item[key] = str(value)[:800 if key == "prompt" else 180]
    return item

def append_generated_assets_from_record(lib, record):
    urls = generated_asset_urls(record)
    if not urls:
        return 0
    ensure_generated_asset_categories(lib)
    category_id = generated_asset_category_id(record)
    category = find_asset_category(lib, category_id) or find_asset_category(lib, "generated_other")
    if not category:
        return 0
    source_keys = asset_library_source_keys(lib)
    count = 0
    for url in urls:
        clean = url.split("?", 1)[0]
        if clean in source_keys:
            continue
        item = generated_asset_item(url, record, category_id)
        if not item:
            continue
        category.setdefault("items", []).append(item)
        source_keys.add(clean)
        source_keys.add(item["url"].split("?", 1)[0])
        count += 1
    return count

def record_generated_assets(record):
    if not isinstance(record, dict) or not generated_asset_urls(record):
        return
    lib = load_asset_library()
    if append_generated_assets_from_record(lib, record):
        save_asset_library(lib)

def backfill_generated_assets_from_history(lib):
    if lib.get("generated_history_backfilled_at"):
        return False
    count = 0
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
        if isinstance(history, list):
            for record in reversed(history):
                if isinstance(record, dict):
                    count += append_generated_assets_from_record(lib, record)
    lib["generated_history_backfilled_at"] = now_ms()
    lib["generated_history_backfill_count"] = count
    return True

def load_asset_library():
    changed = False
    if not os.path.exists(ASSET_LIBRARY_PATH):
        lib = default_asset_library()
        changed = True
    else:
        try:
            with open(ASSET_LIBRARY_PATH, "r", encoding="utf-8") as f:
                lib = json.load(f)
        except Exception:
            lib = default_asset_library()
            changed = True
    cats = lib.get("categories") if isinstance(lib.get("categories"), list) else []
    if not any(c.get("type") == "workflow" for c in cats):
        cats.append({"id": "workflows", "name": "工作流", "type": "workflow", "items": []})
        changed = True
    lib["categories"] = cats
    changed = ensure_generated_asset_categories(lib) or changed
    if backfill_generated_assets_from_history(lib):
        changed = True
    lib["updated_at"] = int(lib.get("updated_at") or now_ms())
    sort_asset_library_items(lib)
    if changed:
        save_asset_library(lib)
    return lib

def sort_asset_library_items(lib):
    for cat in lib.get("categories", []):
        items = cat.get("items")
        if isinstance(items, list):
            def created_at_key(item):
                if not isinstance(item, dict):
                    return 0
                try:
                    return int(float(item.get("created_at") or 0))
                except (TypeError, ValueError):
                    return 0
            items.sort(key=created_at_key, reverse=True)
    return lib

def save_asset_library(lib):
    sort_asset_library_items(lib)
    lib["updated_at"] = now_ms()
    os.makedirs(DATA_DIR, exist_ok=True)
    with ASSET_LIBRARY_LOCK:
        with open(ASSET_LIBRARY_PATH, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, indent=2)

def find_asset_category(lib, category_id):
    for cat in lib.get("categories", []):
        if cat.get("id") == category_id:
            return cat
    return None

def sanitize_asset_name(name, fallback="asset"):
    name = re.sub(r'[\\/:*?"<>|]+', "_", str(name or fallback)).strip()
    return name[:120] or fallback

def content_type_for_path(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in [".mp4", ".m4v"]:
        return "video/mp4"
    if ext == ".webm":
        return "video/webm"
    if ext == ".mov":
        return "video/quicktime"
    if ext == ".mp3":
        return "audio/mpeg"
    if ext == ".wav":
        return "audio/wav"
    if ext == ".m4a":
        return "audio/mp4"
    if ext == ".aac":
        return "audio/aac"
    if ext == ".ogg":
        return "audio/ogg"
    if ext == ".flac":
        return "audio/flac"
    if ext == ".gif":
        return "image/gif"
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    if ext == ".txt":
        return "text/plain; charset=utf-8"
    if ext == ".json":
        return "application/json; charset=utf-8"
    if ext == ".csv":
        return "text/csv; charset=utf-8"
    if ext == ".md":
        return "text/markdown; charset=utf-8"
    if ext == ".srt":
        return "application/x-subrip; charset=utf-8"
    if ext == ".vtt":
        return "text/vtt; charset=utf-8"
    if ext == ".svg":
        return "image/svg+xml"
    if ext == ".png":
        return "image/png"
    return "application/octet-stream"

def asset_library_media_kind(path, content_type=""):
    ctype = (content_type or content_type_for_path(path or "") or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("audio/"):
        return "audio"
    return "file"

def normalize_asset_libraries(lib):
    cats = lib.get("categories") if isinstance(lib.get("categories"), list) else []
    lib["categories"] = cats
    libraries = lib.get("libraries") if isinstance(lib.get("libraries"), list) else []
    if not libraries:
        libraries = [{
            "id": "default",
            "name": "默认资产库",
            "type": "asset",
            "categories": cats,
            "created_at": lib.get("created_at") or now_ms(),
            "updated_at": lib.get("updated_at") or now_ms(),
        }]
        lib["libraries"] = libraries
    if not lib.get("active_library_id"):
        lib["active_library_id"] = libraries[0].get("id") if libraries else "default"
    default_library = next((item for item in libraries if item.get("id") == "default"), None)
    if default_library is not None and not isinstance(default_library.get("categories"), list):
        default_library["categories"] = cats
    return lib

def asset_library_response(lib):
    normalize_asset_libraries(lib)
    return lib

def find_asset_library_compat(lib, library_id=""):
    normalize_asset_libraries(lib)
    library_id = str(library_id or "").strip()
    if not library_id:
        library_id = lib.get("active_library_id") or "default"
    for item in lib.get("libraries", []):
        if item.get("id") == library_id:
            return item
    return lib.get("libraries", [None])[0] if lib.get("libraries") else None

def iter_asset_categories_compat(lib):
    seen = set()
    cats = lib.get("categories") if isinstance(lib.get("categories"), list) else []
    for cat in cats:
        marker = id(cat)
        if marker not in seen:
            seen.add(marker)
            yield cat
    for library in lib.get("libraries", []) or []:
        for cat in library.get("categories") or []:
            marker = id(cat)
            if marker not in seen:
                seen.add(marker)
                yield cat

def find_asset_category_compat(lib, category_id, library_id=""):
    category_id = str(category_id or "").strip()
    if not category_id:
        return None
    library = find_asset_library_compat(lib, library_id)
    if library:
        for cat in library.get("categories") or []:
            if cat.get("id") == category_id:
                return cat
    for cat in lib.get("categories", []) or []:
        if cat.get("id") == category_id:
            return cat
    return None

def make_asset_library_item(src_path, name=""):
    if not src_path or not os.path.isfile(src_path):
        raise HTTPException(status_code=404, detail="Asset source file not found")
    ext = os.path.splitext(src_path)[1].lower() or ".bin"
    safe_name = sanitize_asset_name(name or os.path.basename(src_path), "asset")
    filename = f"{now_ms()}_{uuid.uuid4().hex[:8]}_{safe_name}"
    if not filename.lower().endswith(ext):
        filename += ext
    os.makedirs(ASSET_LIBRARY_DIR, exist_ok=True)
    dst = os.path.join(ASSET_LIBRARY_DIR, filename)
    shutil.copy2(src_path, dst)
    ctype = content_type_for_path(dst)
    return {
        "id": f"asset_{uuid.uuid4().hex[:12]}",
        "name": name or os.path.basename(src_path),
        "url": f"/assets/library/{filename}",
        "kind": asset_library_media_kind(dst, ctype),
        "content_type": ctype,
        "created_at": now_ms(),
    }

def prompt_template_markdown_path():
    return os.path.join(STATIC_DIR, "system-prompts", "infinite-canvas-prompt-templates.md")

def parse_prompt_template_markdown():
    path = prompt_template_markdown_path()
    items = []
    if not os.path.exists(path):
        return items
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return items
    current_title = ""
    current_lines = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_title and current_lines:
                prompt = "\n".join(current_lines).strip()
                items.append({
                    "id": f"template_{uuid.uuid5(uuid.NAMESPACE_URL, current_title).hex[:12]}",
                    "name": current_title,
                    "title": current_title,
                    "positive": prompt,
                    "prompt": prompt,
                    "negative": "",
                    "category": "system",
                    "scene": "系统模板",
                })
            current_title = line[3:].strip()
            current_lines = []
        elif current_title:
            current_lines.append(line)
    if current_title and current_lines:
        prompt = "\n".join(current_lines).strip()
        items.append({
            "id": f"template_{uuid.uuid5(uuid.NAMESPACE_URL, current_title).hex[:12]}",
            "name": current_title,
            "title": current_title,
            "positive": prompt,
            "prompt": prompt,
            "negative": "",
            "category": "system",
            "scene": "系统模板",
        })
    return items

def default_prompt_libraries():
    return {
        "libraries": [{
            "id": "system",
            "name": "系统提示词",
            "readonly": False,
            "categories": [{"id": "system", "name": "系统模板"}, {"id": "custom", "name": "自定义"}],
            "items": parse_prompt_template_markdown(),
            "created_at": now_ms(),
            "updated_at": now_ms(),
        }],
        "active_library_id": "system",
        "updated_at": now_ms(),
    }

def load_prompt_libraries():
    if not os.path.exists(PROMPT_LIBRARY_PATH):
        data = default_prompt_libraries()
        save_prompt_libraries(data)
        return data
    try:
        with open(PROMPT_LIBRARY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = default_prompt_libraries()
    if not isinstance(data.get("libraries"), list) or not data.get("libraries"):
        data = default_prompt_libraries()
    for library in data.get("libraries", []):
        library.setdefault("categories", [{"id": "custom", "name": "自定义"}])
        library.setdefault("items", [])
        library.setdefault("updated_at", now_ms())
    return data

def save_prompt_libraries(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    data["updated_at"] = now_ms()
    with PROMPT_LIBRARY_LOCK:
        with open(PROMPT_LIBRARY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def find_prompt_library(data, library_id=""):
    library_id = str(library_id or "").strip() or data.get("active_library_id") or "system"
    for library in data.get("libraries", []):
        if library.get("id") == library_id:
            return library
    return data.get("libraries", [None])[0] if data.get("libraries") else None

def normalize_prompt_payload(payload):
    if isinstance(payload, BaseModel):
        data = payload.dict()
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {}
    name = data.get("name") or data.get("title") or data.get("scene") or "提示词"
    positive = data.get("positive") or data.get("prompt") or ""
    return {
        "id": data.get("id") or f"prompt_{uuid.uuid4().hex[:12]}",
        "name": str(name).strip() or "提示词",
        "title": str(name).strip() or "提示词",
        "positive": positive,
        "prompt": positive,
        "negative": data.get("negative") or "",
        "category": data.get("category") or data.get("category_id") or "custom",
        "scene": data.get("scene") or "",
        "tags": data.get("tags") if isinstance(data.get("tags"), list) else [],
        "created_at": data.get("created_at") or now_ms(),
        "updated_at": now_ms(),
    }

LOCAL_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
    ".json", ".zip", ".svg", ".txt", ".md"
}

def safe_rel_fragment(value):
    value = str(value or "").replace("\\", "/").strip().strip("/")
    parts = []
    for part in value.split("/"):
        clean = sanitize_asset_name(part, "")
        if clean and clean not in (".", ".."):
            parts.append(clean)
    return "/".join(parts)

def local_upload_abs(rel_path=""):
    rel = safe_rel_fragment(rel_path)
    base = os.path.abspath(LOCAL_UPLOAD_DIR)
    target = os.path.abspath(os.path.join(base, rel))
    if os.path.commonpath([base, target]) != base:
        raise HTTPException(status_code=400, detail="Invalid local asset path")
    return target

def local_upload_url(rel_path):
    return f"/assets/uploads/{safe_rel_fragment(rel_path)}"

def local_upload_caption_path(rel_path):
    path = local_upload_abs(rel_path)
    root, _ = os.path.splitext(path)
    return f"{root}.caption.txt"

def read_local_upload_caption(rel_path):
    caption_path = local_upload_caption_path(rel_path)
    if os.path.exists(caption_path):
        try:
            with open(caption_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return ""

def local_upload_item(rel_path):
    abs_path = local_upload_abs(rel_path)
    if not os.path.isfile(abs_path):
        return None
    name = os.path.basename(abs_path)
    ctype = content_type_for_path(abs_path)
    caption = read_local_upload_caption(rel_path) if ctype.startswith("image/") else ""
    stat = os.stat(abs_path)
    return {
        "id": safe_rel_fragment(rel_path),
        "name": name,
        "file": safe_rel_fragment(rel_path),
        "path": safe_rel_fragment(rel_path),
        "url": local_upload_url(rel_path),
        "kind": asset_library_media_kind(abs_path, ctype),
        "content_type": ctype,
        "caption": caption,
        "caption_file": f"{safe_rel_fragment(rel_path)}.caption.txt" if caption else "",
        "size": stat.st_size,
        "created_at": int(stat.st_ctime * 1000),
        "updated_at": int(stat.st_mtime * 1000),
    }

def local_upload_tree_and_items():
    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    items = []
    root = {"name": "uploads", "path": "", "children": [], "count": 0}
    folder_nodes = {"": root}
    for dirpath, dirnames, filenames in os.walk(LOCAL_UPLOAD_DIR):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, LOCAL_UPLOAD_DIR)
        rel_dir = "" if rel_dir == "." else rel_dir.replace("\\", "/")
        node = folder_nodes.setdefault(rel_dir, {
            "name": os.path.basename(dirpath),
            "path": rel_dir,
            "children": [],
            "count": 0,
        })
        for dirname in dirnames:
            child_rel = safe_rel_fragment(f"{rel_dir}/{dirname}" if rel_dir else dirname)
            child = folder_nodes.setdefault(child_rel, {
                "name": dirname,
                "path": child_rel,
                "children": [],
                "count": 0,
            })
            if child not in node["children"]:
                node["children"].append(child)
        for filename in filenames:
            if filename.endswith(".caption.txt"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in LOCAL_ASSET_EXTS:
                continue
            rel_file = safe_rel_fragment(f"{rel_dir}/{filename}" if rel_dir else filename)
            item = local_upload_item(rel_file)
            if item:
                item["folder"] = rel_dir
                items.append(item)
                node["count"] += 1
    items.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return root, items

SHARED_MEDIA_EXTS = LOCAL_ASSET_EXTS
SHARED_SCAN_MAX_ENTRIES = 1000

def load_shared_folders():
    if not os.path.exists(SHARED_FOLDERS_FILE):
        return {"folders": []}
    try:
        with open(SHARED_FOLDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"folders": []}
    if not isinstance(data.get("folders"), list):
        data["folders"] = []
    return data

def save_shared_folders(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with SHARED_FOLDERS_LOCK:
        with open(SHARED_FOLDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def shared_folder_by_id(data, folder_id):
    for folder in data.get("folders", []):
        if folder.get("id") == folder_id:
            return folder
    return None

def shared_child_abs(folder, rel_path=""):
    base = os.path.abspath(folder.get("path") or "")
    target = os.path.abspath(os.path.join(base, safe_rel_fragment(rel_path)))
    if not base or not os.path.isdir(base) or os.path.commonpath([base, target]) != base:
        raise HTTPException(status_code=400, detail="Invalid shared folder path")
    return target

def scan_shared_tree(folder):
    base = os.path.abspath(folder.get("path") or "")
    if not os.path.isdir(base):
        return {"name": folder.get("name") or "folder", "path": "", "children": [], "files": []}
    count = 0
    def scan(path, rel=""):
        nonlocal count
        node = {"name": os.path.basename(path) or folder.get("name") or "folder", "path": rel, "children": [], "files": []}
        if count > SHARED_SCAN_MAX_ENTRIES:
            return node
        try:
            entries = sorted(os.scandir(path), key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        except OSError:
            return node
        for entry in entries:
            if entry.name.startswith("."):
                continue
            next_rel = safe_rel_fragment(f"{rel}/{entry.name}" if rel else entry.name)
            if entry.is_dir(follow_symlinks=False):
                node["children"].append(scan(entry.path, next_rel))
            elif os.path.splitext(entry.name)[1].lower() in SHARED_MEDIA_EXTS:
                count += 1
                stat = entry.stat()
                node["files"].append({
                    "name": entry.name,
                    "path": next_rel,
                    "url": f"/api/shared-folders/{folder.get('id')}/file?path={urllib.parse.quote(next_rel)}",
                    "content_type": content_type_for_path(entry.path),
                    "size": stat.st_size,
                    "updated_at": int(stat.st_mtime * 1000),
                })
        return node
    return scan(base)

def is_image_reference_value(value):
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("data:image/"):
        return True
    if value.startswith("data:"):
        return False
    if value.startswith("/output/") or value.startswith("/assets/"):
        path = output_file_from_url(value)
        return bool(path and content_type_for_path(path).startswith("image/"))
    clean = value.split("?", 1)[0].lower()
    if re.search(r"\.(mp4|webm|mov|m4v|mp3|wav|m4a|aac|ogg|flac)$", clean):
        return False
    return True

def convert_output_to_jpg(url, quality=88):
    path = output_file_from_url(url)
    if not path:
        return url
    root, ext = os.path.splitext(path)
    if ext.lower() in [".jpg", ".jpeg"]:
        return url
    jpg_path = f"{root}.jpg"
    try:
        with Image.open(path) as img:
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            img.save(jpg_path, "JPEG", quality=quality, optimize=True)
        try:
            root = ASSETS_DIR if os.path.commonpath([os.path.abspath(ASSETS_DIR), os.path.abspath(jpg_path)]) == os.path.abspath(ASSETS_DIR) else OUTPUT_DIR
        except ValueError:
            root = OUTPUT_DIR
        rel = os.path.relpath(jpg_path, root).replace("\\", "/")
        prefix = "/assets" if root == ASSETS_DIR else "/output"
        return f"{prefix}/{rel}"
    except Exception as e:
        print(f"转换 JPG 失败: {e}")
        return url

def reference_to_data_url(ref, max_size=None):
    """把本地输出文件转为 data URL（base64）。max_size 限制最长边像素，避免 payload 过大。"""
    path = output_file_from_url(ref.get("url", ""))
    if not path:
        return ref.get("url", "")
    if max_size:
        try:
            with Image.open(path) as img:
                img.load()
                w, h = img.size
                if max(w, h) > max_size:
                    img.thumbnail((max_size, max_size), Image.LANCZOS)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                buf = BytesIO()
                fmt = "PNG" if img.mode == "RGBA" else "JPEG"
                img.save(buf, format=fmt, quality=88 if fmt == "JPEG" else None)
                encoded = base64.b64encode(buf.getvalue()).decode("ascii")
                mime = "image/png" if fmt == "PNG" else "image/jpeg"
                return f"data:{mime};base64,{encoded}"
        except Exception as e:
            print(f"reference resize failed, fallback to raw: {e}")
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{content_type_for_path(path)};base64,{encoded}"

def compress_data_url_image(value, max_size=1536, jpeg_quality=88):
    if not isinstance(value, str) or not value.startswith("data:image/") or ";base64," not in value:
        return value
    header, encoded = value.split(";base64,", 1)
    try:
        raw = base64.b64decode(encoded)
        with Image.open(BytesIO(raw)) as img:
            img.load()
            if max_size and max(img.size) > max_size:
                img.thumbnail((max_size, max_size), Image.LANCZOS)
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if has_alpha:
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                fmt, mime = "PNG", "image/png"
            else:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                fmt, mime = "JPEG", "image/jpeg"
            buf = BytesIO()
            if fmt == "JPEG":
                img.save(buf, format=fmt, quality=jpeg_quality, optimize=True)
            else:
                img.save(buf, format=fmt, optimize=True)
            return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception as e:
        print(f"data url image compress failed, fallback to raw: {e}")
        return value

def modelscope_image_url(value, max_size=1536):
    if not value:
        return value
    if isinstance(value, str) and (value.startswith("/output/") or value.startswith("/assets/")):
        return reference_to_data_url({"url": value}, max_size=max_size)
    if isinstance(value, str) and value.startswith("data:image/"):
        return compress_data_url_image(value, max_size=max_size)
    return value

def valid_video_image_input(value: str) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    return (
        value.startswith("http://") or
        value.startswith("https://") or
        value.startswith("asset://") or
        (value.startswith("data:image/") and ";base64," in value)
    )

def valid_apimart_video_image_input(value: str) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    return value.startswith("http://") or value.startswith("https://") or value.startswith("asset://")

def is_apimart_veo31_model(model: str) -> bool:
    return str(model or "").strip().lower().startswith("veo3.1")

def apimart_veo31_model(model: str) -> str:
    value = str(model or "").strip().lower()
    aliases = {
        "veo3.1": "veo3.1-fast",
        "veo3.1-pro": "veo3.1-quality",
        "veo3.1-preview": "veo3.1-fast",
    }
    value = aliases.get(value, value or "veo3.1-fast")
    allowed = {"veo3.1-fast", "veo3.1-quality", "veo3.1-lite"}
    return value if value in allowed else "veo3.1-fast"

def apimart_veo31_aspect(aspect: str) -> str:
    value = str(aspect or "16:9").strip()
    return value if value in {"16:9", "9:16"} else "16:9"

def apimart_veo31_resolution(resolution: str) -> str:
    value = str(resolution or "").strip().lower()
    aliases = {"": "720p", "auto": "720p", "480p": "720p", "780p": "720p", "1080": "1080p", "4k": "4k"}
    value = aliases.get(value, value)
    return value if value in {"720p", "1080p", "4k"} else "720p"

def apimart_upload_file_payload(path: str):
    """Return (filename, bytes, content_type), keeping APIMart VEO images under the documented 10MB limit."""
    max_bytes = 9_500_000
    size = os.path.getsize(path)
    if size <= max_bytes:
        with open(path, "rb") as fh:
            return os.path.basename(path), fh.read(), content_type_for_path(path)
    with Image.open(path) as img:
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        quality = 92
        while quality >= 62:
            buf = BytesIO()
            bg.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                name = os.path.splitext(os.path.basename(path))[0] + ".jpg"
                return name, data, "image/jpeg"
            quality -= 8
    raise ValueError("图片超过 10MB，且压缩后仍无法满足 VEO3.1 图片限制")

def invalid_video_image_preview(value: str) -> str:
    text = str(value or "")
    if text.startswith("data:"):
        return text.split(";base64,", 1)[0] + ";base64,..."
    return text[:120]

def extract_apimart_asset_url(payload):
    if isinstance(payload, list):
        for item in payload:
            found = extract_apimart_asset_url(item)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""
    url_keys = ("url", "asset_url", "assetUrl", "uri", "file_url", "fileUrl")
    for key in url_keys:
        value = str(payload.get(key) or "").strip()
        if valid_apimart_video_image_input(value):
            return value
    id_keys = ("asset_id", "assetId", "file_id", "fileId", "id")
    for key in id_keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value if value.startswith("asset://") else f"asset://{value}"
    for key in ("data", "file", "asset", "result"):
        found = extract_apimart_asset_url(payload.get(key))
        if found:
            return found
    return ""

def apimart_upload_payload_from_bytes(data: bytes, mime: str, name_hint: str = "image"):
    """把内存中的图片字节按 APIMart 的 10MB 限制压缩为可上传 payload。"""
    max_bytes = 9_500_000
    ext = mimetypes.guess_extension(mime or "image/png") or ".png"
    if len(data) <= max_bytes and (mime or "").lower() in ("image/png", "image/jpeg", "image/webp"):
        return f"{name_hint}{ext}", data, (mime or "image/png")
    with Image.open(BytesIO(data)) as img:
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        if has_alpha:
            base = img.convert("RGBA")
            bg = Image.new("RGB", base.size, (255, 255, 255))
            bg.paste(base, mask=base.split()[-1])
            target = bg
        else:
            target = img.convert("RGB")
        quality = 92
        while quality >= 62:
            buf = BytesIO()
            target.save(buf, format="JPEG", quality=quality, optimize=True)
            payload = buf.getvalue()
            if len(payload) <= max_bytes:
                return f"{name_hint}.jpg", payload, "image/jpeg"
            quality -= 8
    raise ValueError("data URL 图片超过 10MB，且压缩后仍无法满足 APIMart 限制")

async def upload_image_for_apimart(client, provider, ref_url: str) -> str:
    """把本地图片转成上游可接受的输入。
    按 APIMart 文档上传到 /v1/uploads/images，拿到可用于生成接口的 http/https URL。
    绝不把 /output/* 或 /assets/* 这类本地路径直接传给上游。
    返回上游可用 URL；返回值以 "ERR:" 开头表示具体失败原因（供前端展示）。"""
    ref_url = str(ref_url or "").strip()
    if not ref_url:
        return "ERR:空地址"
    # 已经是网络 URL 或 asset:// → 直接可用，无需上传
    if ref_url.startswith("http://") or ref_url.startswith("https://") or ref_url.startswith("asset://"):
        return ref_url
    base_url = video_api_root(provider)
    upload_url = f"{base_url}/v1/uploads/images"
    # data URL: 解码后直接上传到 APIMart
    if ref_url.startswith("data:"):
        try:
            if ";base64," not in ref_url:
                return "ERR:不支持的 data URL（缺少 base64 段）"
            header, encoded = ref_url.split(";base64,", 1)
            mime = header.split(":", 1)[1].split(";", 1)[0] if ":" in header else "image/png"
            raw = base64.b64decode(encoded)
            filename, content, ct = apimart_upload_payload_from_bytes(raw, mime, name_hint="canvas_image")
            files = {"file": (filename, content, ct)}
            resp = await client.post(upload_url, headers=api_headers(json_body=False, provider=provider), files=files, timeout=60)
            if resp.status_code in (200, 201):
                rj = resp.json()
                url = extract_apimart_asset_url(rj)
                if valid_apimart_video_image_input(url):
                    return url
                print(f"APIMart 上传 data URL 返回中未找到可用 asset/url: {str(rj)[:300]}")
                return "ERR:APIMart 上传响应未包含可用 URL"
            print(f"APIMart 上传 data URL 失败 ({resp.status_code}): {resp.text[:300]}")
            return f"ERR:APIMart 上传失败({resp.status_code})"
        except ValueError as e:
            return f"ERR:{e}"
        except Exception as e:
            print(f"APIMart 上传 data URL 异常: {e}")
            return f"ERR:上传异常 {e}"
    # 本地 /output/ 或 /assets/ 路径：先确认文件存在再上传
    if ref_url.startswith("/output/") or ref_url.startswith("/assets/"):
        path = output_file_from_url(ref_url)
        if not path:
            print(f"APIMart 上传跳过：本地文件不存在 {ref_url}")
            return "ERR:本地文件不存在或已被删除"
        try:
            filename, content, ct = apimart_upload_file_payload(path)
            files = {"file": (filename, content, ct)}
            resp = await client.post(upload_url, headers=api_headers(json_body=False, provider=provider), files=files, timeout=60)
            if resp.status_code in (200, 201):
                rj = resp.json()
                url = extract_apimart_asset_url(rj)
                if valid_apimart_video_image_input(url):
                    return url
                print(f"APIMart 文件上传返回中未找到可用 asset/url: {str(rj)[:300]}")
                return "ERR:APIMart 上传响应未包含可用 URL"
            print(f"APIMart 文件上传失败 ({resp.status_code}): {resp.text[:300]}")
            return f"ERR:APIMart 上传失败({resp.status_code})"
        except ValueError as e:
            return f"ERR:{e}"
        except Exception as e:
            print(f"APIMart 文件上传异常: {e}")
            return f"ERR:上传异常 {e}"
    return "ERR:不支持的图片来源（仅支持 http/https/asset/data 或本地 /output/ /assets/ 路径）"

async def save_ai_image_to_output(image_data, prefix="online_", category="output"):
    filename = f"{prefix}{uuid.uuid4().hex[:10]}.png"
    path = output_path_for(filename, category)
    if image_data["type"] == "b64":
        mime_type = str(image_data.get("mime_type") or "").lower()
        if "jpeg" in mime_type or "jpg" in mime_type:
            filename = filename[:-4] + ".jpg"
            path = output_path_for(filename, category)
        elif "webp" in mime_type:
            filename = filename[:-4] + ".webp"
            path = output_path_for(filename, category)
        with open(path, "wb") as f:
            f.write(base64.b64decode(image_data["value"]))
        return output_url_for(filename, category)
    value = image_data["value"]
    if value.startswith("/output/") or value.startswith("/assets/"):
        return value
    try:
        timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(value)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "jpeg" in content_type or "jpg" in content_type:
                filename = filename[:-4] + ".jpg"
                path = output_path_for(filename, category)
            elif "webp" in content_type:
                filename = filename[:-4] + ".webp"
                path = output_path_for(filename, category)
            with open(path, "wb") as f:
                f.write(response.content)
            return output_url_for(filename, category)
    except Exception as e:
        print(f"保存上游图片失败: {e}")
        return value

async def save_remote_video_to_output(url, prefix="video_", category="output"):
    if not url:
        return ""
    if url.startswith("/output/") or url.startswith("/assets/"):
        return url
    filename = f"{prefix}{uuid.uuid4().hex[:10]}.mp4"
    path = output_path_for(filename, category)
    try:
        async with httpx.AsyncClient(timeout=VIDEO_POLL_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            clean_path = urllib.parse.urlparse(url).path
            ext = os.path.splitext(clean_path)[1].lower()
            if ext in {".mp4", ".webm", ".mov"}:
                filename = filename[:-4] + ext
                path = output_path_for(filename, category)
            elif "webm" in content_type:
                filename = filename[:-4] + ".webm"
                path = output_path_for(filename, category)
            elif "quicktime" in content_type or "mov" in content_type:
                filename = filename[:-4] + ".mov"
                path = output_path_for(filename, category)
            with open(path, "wb") as f:
                f.write(response.content)
            return output_url_for(filename, category)
    except Exception as e:
        print(f"保存上游视频失败: {e}")
        return url

def parse_size_pair(size):
    match = re.fullmatch(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*", str(size or ""))
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))

GPT_IMAGE2_MAX_EDGE = 3840
GPT_IMAGE2_MAX_PIXELS = 8_294_400
GPT_IMAGE2_MIN_PIXELS = 655_360

def is_gpt_image_2_model(model):
    return str(model or "").strip().lower() == "gpt-image-2"

def normalize_gpt_image_2_size(size):
    width, height = parse_size_pair(size)
    if not width or not height:
        return size or "auto"
    if width == height and (width > 2048 or width * height > 4_194_304):
        return "3840x2160"
    ratio = width / height
    if ratio > 3:
        width = height * 3
    elif ratio < 1 / 3:
        height = width * 3
    scale = min(
        1.0,
        GPT_IMAGE2_MAX_EDGE / max(width, height),
        (GPT_IMAGE2_MAX_PIXELS / max(1, width * height)) ** 0.5,
    )
    width = max(16, int((width * scale) // 16) * 16)
    height = max(16, int((height * scale) // 16) * 16)
    if width * height < GPT_IMAGE2_MIN_PIXELS:
        grow = (GPT_IMAGE2_MIN_PIXELS / max(1, width * height)) ** 0.5
        width = int((width * grow + 15) // 16) * 16
        height = int((height * grow + 15) // 16) * 16
    return f"{width}x{height}"

def apimart_size_resolution(size):
    width, height = parse_size_pair(size)
    if not width or not height:
        raw = str(size or "").strip().lower()
        if raw in {"1k", "2k", "4k"}:
            return "1:1", raw
        if re.fullmatch(r"(auto|\d+\s*:\s*\d+)", raw):
            return raw.replace(" ", ""), "1k"
        return "1:1", "1k"
    long_edge = max(width, height)
    pixels = width * height
    if long_edge >= 3000 or pixels > 4_500_000:
        resolution = "4k"
    elif long_edge >= 1800 or pixels > 1_800_000:
        resolution = "2k"
    else:
        resolution = "1k"
    common = [
        (1, 1, "1:1"), (3, 2, "3:2"), (2, 3, "2:3"), (4, 3, "4:3"), (3, 4, "3:4"),
        (5, 4, "5:4"), (4, 5, "4:5"), (16, 9, "16:9"), (9, 16, "9:16"),
        (2, 1, "2:1"), (1, 2, "1:2"), (3, 1, "3:1"), (1, 3, "1:3"),
        (21, 9, "21:9"), (9, 21, "9:21"),
    ]
    ratio = width / height
    best = min(common, key=lambda item: abs(ratio - item[0] / item[1]))
    return best[2], resolution

async def generate_modelscope_provider_image(prompt, size, model, reference_images=None, provider=None):
    clean_token = MODELSCOPE_API_KEY.strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未配置 ModelScope API Key，请在 API 设置中填写。")
    width, height = parse_size_pair(size)
    refs = []
    for ref in (reference_images or [])[:4]:
        if not ref.get("url"):
            continue
        # 把参考图压缩为 data URL，避免 base64 payload 过大导致 MS 内部任务失败
        refs.append(modelscope_image_url(ref.get("url", ""), max_size=1536))
    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true",
    }
    payload = {
        "model": selected_model(model, "Tongyi-MAI/Z-Image-Turbo"),
        "prompt": prompt.strip(),
    }
    if width and height:
        payload["width"] = width
        payload["height"] = height
        payload["size"] = f"{width}x{height}"
    if refs:
        payload["image_url"] = refs

    base_root = ((provider or {}).get("base_url") or MODELSCOPE_CHAT_BASE_URL).rstrip("/")
    api_root = base_root if base_root.endswith("/v1") else f"{base_root}/v1"
    async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
        submit_res = await client.post(f"{api_root}/images/generations", headers=headers, json=payload)
        submit_res.raise_for_status()
        raw = submit_res.json()
        task_id = raw.get("task_id")
        if not task_id:
            try:
                return extract_image(raw), raw
            except HTTPException:
                raise HTTPException(status_code=502, detail=f"ModelScope 未返回 task_id：{raw}")

        deadline = time.monotonic() + AI_REQUEST_TIMEOUT
        last_payload = raw
        while time.monotonic() < deadline:
            await asyncio.sleep(IMAGE_POLL_INTERVAL)
            result = await client.get(
                f"{api_root}/tasks/{task_id}",
                headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
            )
            result.raise_for_status()
            data = result.json()
            last_payload = data
            status = str(data.get("task_status") or "").upper()
            if status == "SUCCEED":
                images = data.get("output_images") or []
                if not images:
                    raise HTTPException(status_code=502, detail=f"ModelScope 成功但没有返回图片：{data}")
                return {"type": "url", "value": images[0]}, data
            if status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}:
                detail = data.get("error_info") or data.get("message") or data.get("detail") or str(data)
                raise HTTPException(status_code=502, detail=f"ModelScope 任务失败：{detail}")
        raise HTTPException(status_code=504, detail=f"ModelScope 生图任务超时：{last_payload}")

def gemini_model_name(model):
    value = selected_model(model, "gemini-3-pro-image-preview").strip()
    return value[len("models/"):] if value.startswith("models/") else value

def gemini_endpoint_url(provider, model):
    model_name = urllib.parse.quote(gemini_model_name(model), safe="")
    return provider_endpoint_url(provider, "image_generation_endpoint", f"/v1beta/models/{model_name}:generateContent")

def gemini_image_config(size):
    width, height = parse_size_pair(size)
    if not width or not height:
        raw = str(size or "").strip().upper()
        if raw in {"1K", "2K", "4K"}:
            return {"aspectRatio": "1:1", "imageSize": raw}
        if re.fullmatch(r"\d+\s*:\s*\d+", raw):
            return {"aspectRatio": raw.replace(" ", ""), "imageSize": "1K"}
        return {"aspectRatio": "1:1", "imageSize": "2K"}
    aspect_ratio, resolution = apimart_size_resolution(size)
    return {"aspectRatio": aspect_ratio, "imageSize": resolution.upper()}

def gemini_reference_part(ref):
    value = reference_to_data_url(ref, max_size=1536)
    if not value:
        return None
    if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
        header, encoded = value.split(";base64,", 1)
        mime_type = header.replace("data:", "", 1) or "image/png"
        return {"inlineData": {"mimeType": mime_type, "data": encoded}}
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return {"fileData": {"mimeType": "image/png", "fileUri": value}}
    return None

async def generate_gemini_provider_image(prompt, size, model, reference_images=None, provider=None):
    model_name = gemini_model_name(model)
    endpoint = gemini_endpoint_url(provider, model_name)
    parts = [{"text": prompt.strip()}]
    for ref in (reference_images or [])[:16]:
        part = gemini_reference_part(ref)
        if part:
            parts.append(part)
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": gemini_image_config(size),
        },
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=1800.0, write=120.0, pool=20.0)) as client:
        response = await client.post(endpoint, headers=api_headers(provider=provider), json=body)
        response.raise_for_status()
        raw = response.json()
        return extract_image(raw), raw

def volcengine_endpoint_url(provider):
    return provider_endpoint_url(provider, "image_generation_endpoint", "/api/v3/images/generations")

def volcengine_image_payload(ref):
    value = reference_to_data_url(ref, max_size=1536)
    if not value:
        return None
    return value

async def generate_volcengine_provider_image(prompt, size, model, reference_images=None, provider=None):
    endpoint = volcengine_endpoint_url(provider)
    body = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
    }
    images = [volcengine_image_payload(ref) for ref in (reference_images or [])[:10]]
    images = [value for value in images if value]
    if images:
        body["image"] = images
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=1800.0, write=120.0, pool=20.0)) as client:
        response = await client.post(endpoint, headers=api_headers(provider=provider), json=body)
        response.raise_for_status()
        raw = response.json()
        return extract_image(raw), raw

def runninghub_api_headers(provider):
    api_key = runninghub_api_key(provider)
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 RunningHub API Key，请在 API 设置中填写。")
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"}

def runninghub_provider():
    return get_api_provider_exact("runninghub")

def runninghub_api_key(provider=None, use_wallet=False, prefer_wallet=False):
    provider = provider or runninghub_provider()
    free_key = os.getenv(provider_key_env(provider["id"]), "")
    wallet_key = os.getenv(runninghub_wallet_key_env(), "")
    api_key = wallet_key if (use_wallet or prefer_wallet) and wallet_key else free_key
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 RunningHub API Key，请在 RH 设置中填写。")
    return api_key

def runninghub_app_headers(json_body=True, use_wallet=False):
    headers = {"Host": "www.runninghub.cn"}
    provider = runninghub_provider()
    if provider:
        free_key = os.getenv(provider_key_env(provider["id"]), "")
        wallet_key = os.getenv(runninghub_wallet_key_env(), "")
        api_key = wallet_key if use_wallet and wallet_key else free_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers

def runninghub_local_asset_path(url):
    text = str(url or "").strip()
    if not text:
        return None
    if text.startswith("/assets/input/") or text.startswith("/input/"):
        clean = urllib.parse.unquote(text.split("?", 1)[0]).replace("\\", "/")
        rel = clean[len("/assets/input/"):] if clean.startswith("/assets/input/") else clean[len("/input/"):]
        root = OUTPUT_INPUT_DIR
    elif text.startswith("/assets/output/"):
        clean = urllib.parse.unquote(text.split("?", 1)[0]).replace("\\", "/")
        rel = clean[len("/assets/output/"):]
        root = OUTPUT_OUTPUT_DIR
    elif text.startswith("/output/") or text.startswith("/assets/"):
        return output_file_from_url(text)
    else:
        return None
    rel = rel.lstrip("/")
    if not rel:
        return None
    path = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if os.path.commonpath([root_abs, path]) != root_abs or not os.path.exists(path):
        return None
    return path

def runninghub_output_ext(remote, content_type=""):
    tail = str(remote or "").split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(tail)[1].lower().strip(".")
    allowed = {"png","jpg","jpeg","webp","gif","bmp","mp4","webm","mov","m4v","mkv","mp3","wav","ogg","m4a","flac","aac"}
    if ext in allowed:
        return ext
    ct = str(content_type or "").lower()
    if "mp4" in ct:
        return "mp4"
    if "webm" in ct:
        return "webm"
    if "quicktime" in ct:
        return "mov"
    if "mpeg" in ct:
        return "mp3"
    if "wav" in ct:
        return "wav"
    if "ogg" in ct:
        return "ogg"
    if "webp" in ct:
        return "webp"
    if "jpeg" in ct:
        return "jpg"
    return "png"

def runninghub_extract_outputs(data):
    arr = []
    if isinstance(data, list):
        arr = data
    elif isinstance(data, dict):
        for key in ("outputs", "results", "files", "data"):
            value = data.get(key)
            if isinstance(value, list):
                arr = value
                break
        if not arr and (data.get("fileUrl") or data.get("url")):
            arr = [data]
    outputs = []
    for item in arr:
        if isinstance(item, str):
            outputs.append(item)
        elif isinstance(item, dict):
            url = item.get("fileUrl") or item.get("file_url") or item.get("url") or item.get("downloadUrl") or item.get("download_url")
            if isinstance(url, list):
                outputs.extend([str(u) for u in url if u])
            elif url:
                outputs.append(str(url))
    return outputs

async def runninghub_store_remote_output(client, remote):
    if not str(remote or "").startswith(("http://", "https://")):
        return remote
    response = await client.get(remote, follow_redirects=True)
    if not response.is_success:
        return remote
    ext = runninghub_output_ext(remote, response.headers.get("content-type", ""))
    filename = f"rh_{uuid.uuid4().hex[:12]}.{ext}"
    path = output_path_for(filename, "output")
    with open(path, "wb") as f:
        f.write(response.content)
    return output_url_for(filename, "output")

def runninghub_fail_reason(raw):
    data = raw.get("data") if isinstance(raw, dict) else None
    values = []
    if isinstance(data, dict):
        values.extend([data.get("failedReason"), data.get("failReason"), data.get("message"), data.get("error")])
    if isinstance(raw, dict):
        values.extend([raw.get("msg"), raw.get("message"), raw.get("error")])
    for value in values:
        if not value:
            continue
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("exception_message") or value.get("message") or json.dumps(value, ensure_ascii=False)
        return str(value)
    return ""

def runninghub_infer_workflow_field_type(field_name, field_value):
    key = f"{field_name or ''} {field_value or ''}".lower()
    if re.search(r"\b(image|img|mask|photo|picture)\b", key) or re.search(r"\.(png|jpe?g|webp|gif|bmp)(\?|$)", key, re.I):
        return "IMAGE"
    if re.search(r"\b(video|movie|mp4)\b", key) or re.search(r"\.(mp4|webm|mov|m4v|mkv)(\?|$)", key, re.I):
        return "VIDEO"
    if re.search(r"\b(audio|sound|music|voice)\b", key) or re.search(r"\.(mp3|wav|ogg|m4a|flac|aac)(\?|$)", key, re.I):
        return "AUDIO"
    text = str(field_value or "").strip()
    if text.lower() in {"true", "false"}:
        return "BOOLEAN"
    try:
        if text:
            float(text)
            return "NUMBER"
    except Exception:
        pass
    return "TEXT"

def runninghub_is_workflow_link_value(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )

def runninghub_workflow_node_info_list(workflow_json):
    result = []
    if not isinstance(workflow_json, dict):
        return result
    for node_id, node_content in workflow_json.items():
        inputs = node_content.get("inputs") if isinstance(node_content, dict) else None
        if not isinstance(inputs, dict):
            continue
        for field_name, raw_value in inputs.items():
            if runninghub_is_workflow_link_value(raw_value):
                continue
            if isinstance(raw_value, (dict, list)):
                field_value = json.dumps(raw_value, ensure_ascii=False)
            elif raw_value is None:
                field_value = ""
            else:
                field_value = str(raw_value)
            result.append({
                "nodeId": str(node_id),
                "fieldName": str(field_name),
                "fieldValue": field_value,
                "fieldType": runninghub_infer_workflow_field_type(field_name, field_value),
                "source": "workflow",
            })
    return result

def runninghub_task_endpoint(provider, model):
    model_path = str(model or "").strip().strip("/")
    if not model_path:
        model_path = RUNNINGHUB_DEFAULT_IMAGE_MODELS[0]
    if model_path.startswith("/openapi/"):
        return runninghub_endpoint_url(provider, model_path)
    if model_path.startswith("openapi/"):
        return runninghub_endpoint_url(provider, f"/{model_path}")
    return runninghub_endpoint_url(provider, f"/openapi/v2/{model_path}")

def runninghub_query_status(raw):
    if not isinstance(raw, dict):
        return ""
    values = [
        raw.get("status"),
        raw.get("state"),
        raw.get("taskStatus"),
        raw.get("task_status"),
    ]
    data = raw.get("data")
    if isinstance(data, dict):
        values.extend([data.get("status"), data.get("state"), data.get("taskStatus"), data.get("task_status")])
    for value in values:
        if value is not None:
            return str(value).lower()
    return ""

def runninghub_extract_task_id(raw):
    if not isinstance(raw, dict):
        return ""
    for key in ("taskId", "task_id", "id"):
        if raw.get(key):
            return str(raw[key])
    data = raw.get("data")
    if isinstance(data, dict):
        for key in ("taskId", "task_id", "id"):
            if data.get(key):
                return str(data[key])
    return ""

def runninghub_extract_image(raw):
    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail="RunningHub 返回格式不是 JSON 对象")
    containers = [raw]
    data = raw.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        results = container.get("results") or container.get("result") or container.get("outputs") or container.get("output")
        if isinstance(results, dict):
            results = [results]
        if isinstance(results, list):
            for item in results:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    return {"type": "url", "value": item}
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "url" and item.get("value"):
                    return {"type": "url", "value": item["value"]}
                if item.get("type") == "b64" and item.get("value"):
                    return {"type": "b64", "value": item["value"], "mime_type": item.get("mime_type") or "image/png"}
                url = item.get("url") or item.get("fileUrl") or item.get("file_url") or item.get("download_url") or item.get("imageUrl") or item.get("image_url")
                if isinstance(url, list) and url:
                    url = url[0]
                if isinstance(url, str) and url:
                    return {"type": "url", "value": url}
    return extract_image(raw)

async def runninghub_upload_reference(client, provider, ref):
    path = output_file_from_url(ref.get("url", ""))
    if not path:
        value = ref.get("url", "")
        return value if str(value).startswith(("http://", "https://")) else ""
    upload_url = runninghub_endpoint_url(provider, "/openapi/v2/media/upload/binary")
    api_key = os.getenv(provider_key_env(provider["id"]), "")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    with open(path, "rb") as fh:
        files = {"file": (os.path.basename(path), fh, content_type_for_path(path))}
        response = await client.post(upload_url, headers=headers, files=files, timeout=120)
    response.raise_for_status()
    raw = response.json()
    data = raw.get("data") if isinstance(raw, dict) else None
    candidates = [raw, data] if isinstance(data, dict) else [raw]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        value = item.get("download_url") or item.get("downloadUrl") or item.get("url") or item.get("fileUrl") or item.get("file_url")
        if value:
            return str(value)
    raise HTTPException(status_code=502, detail=f"RunningHub 上传图片未返回 download_url：{raw}")

async def wait_for_runninghub_image_task(client, provider, task_id):
    query_url = runninghub_endpoint_url(provider, "/openapi/v2/query")
    deadline = time.monotonic() + 1800
    last_payload = None
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        response = await client.post(query_url, headers=runninghub_api_headers(provider), json={"taskId": task_id})
        response.raise_for_status()
        raw = response.json()
        last_payload = raw
        status = runninghub_query_status(raw)
        if status in {"success", "succeeded", "completed", "complete", "finished", "finish", "done", "3"}:
            return raw
        if status in {"failed", "fail", "error", "canceled", "cancelled", "4"}:
            raise HTTPException(status_code=502, detail=f"RunningHub 任务失败：{raw}")
        try:
            return {"data": {"results": [runninghub_extract_image(raw)]}}
        except HTTPException:
            pass
    raise HTTPException(status_code=504, detail=f"RunningHub 生图任务超时：{last_payload}")

async def generate_runninghub_provider_image(prompt, size, model, reference_images=None, provider=None):
    endpoint = runninghub_task_endpoint(provider, model)
    width, height = parse_size_pair(size)
    body = {"prompt": prompt}
    if width and height:
        body.update({"width": width, "height": height})
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=1800.0, write=180.0, pool=20.0)) as client:
        image_urls = []
        for ref in (reference_images or [])[:10]:
            url = await runninghub_upload_reference(client, provider, ref)
            if url:
                image_urls.append(url)
        if image_urls:
            body["imageUrls"] = image_urls
        response = await client.post(endpoint, headers=runninghub_api_headers(provider), json=body)
        response.raise_for_status()
        raw = response.json()
        try:
            return runninghub_extract_image(raw), raw
        except HTTPException:
            task_id = runninghub_extract_task_id(raw)
            if not task_id:
                raise HTTPException(status_code=502, detail=f"RunningHub 未返回 taskId 或图片结果：{raw}")
        result = await wait_for_runninghub_image_task(client, provider, task_id)
        return runninghub_extract_image(result), result

async def generate_ai_image(prompt, size, quality, model, reference_images=None, provider_id="comfly"):
    provider = get_api_provider(provider_id)
    if provider["id"] == "modelscope":
        return await generate_modelscope_provider_image(prompt, size, model, reference_images, provider)
    if is_runninghub_provider(provider):
        return await generate_runninghub_provider_image(prompt, size, model, reference_images, provider)
    if is_gemini_provider(provider):
        return await generate_gemini_provider_image(prompt, size, model, reference_images, provider)
    if is_volcengine_provider(provider):
        return await generate_volcengine_provider_image(prompt, size, model, reference_images, provider)
    is_gpt2 = is_gpt_image_2_model(model)
    is_apimart = is_apimart_provider(provider)
    quality = str(quality or "").strip().lower()
    if quality not in {"low", "medium", "high"}:
        quality = ""
    if is_gpt_image_2_model(model) and not is_apimart:
        size = normalize_gpt_image_2_size(size)
    base_url = (provider.get("base_url") or AI_BASE_URL).rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    gen_url = provider_endpoint_url(provider, "image_generation_endpoint", "/v1/images/generations")
    edit_url = provider_endpoint_url(provider, "image_edit_endpoint", "/v1/images/edits")
    refs = [ref for ref in (reference_images or []) if ref.get("url")]
    mask_refs = [ref for ref in refs if str(ref.get("role") or "").strip().lower() == "mask" or str(ref.get("name") or "").lower().endswith("_mask.png")]
    image_refs = [ref for ref in refs if ref not in mask_refs]
    request_timeout = httpx.Timeout(connect=20.0, read=1800.0, write=120.0, pool=20.0) if (is_gpt2 or is_apimart) else AI_REQUEST_TIMEOUT
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        response = None
        async def post_openai_edits(edit_files=None):
            data = {"model": model, "prompt": prompt, "size": size}
            if quality:
                data["quality"] = quality
            return await client.post(
                edit_url,
                headers=api_headers(json_body=False, provider=provider),
                data=data,
                files=edit_files if edit_files is not None else {},
            )

        if is_apimart:
            apimart_size, resolution = apimart_size_resolution(size)
            # APIMart 的 GPT-Image-2 图生图仍走 /images/generations，
            # 通过 image_urls 传参考图，不使用 OpenAI multipart /images/edits。
            body = {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "size": apimart_size,
                "resolution": resolution,
                "official_fallback": False,
            }
            if image_refs:
                body["image_urls"] = [reference_to_data_url(ref, max_size=1536) for ref in image_refs[:16]]
            response = await client.post(gen_url, headers=api_headers(provider=provider), json=body)
        elif is_gpt2 and not image_refs and not mask_refs:
            body = {"model": model, "prompt": prompt, "size": size}
            if quality:
                body["quality"] = quality
            response = await client.post(gen_url, headers=api_headers(provider=provider), json=body)
            if response.status_code >= 400 and images_api_unsupported(response):
                response = await post_openai_edits()
        elif image_refs:
            # 1) OpenAI 协议的图生图/编辑用 multipart 提交到 /images/edits；
            # GPT-Image-2 参考图不能走 /images/generations JSON，否则部分平台会忽略原图或报 Images API unsupported。
            files = []
            opened = []
            edit_failed_status = None
            edit_failed_text = ""
            try:
                for ref in image_refs[:4]:
                    path = output_file_from_url(ref.get("url", ""))
                    if not path:
                        continue
                    fh = open(path, "rb")
                    opened.append(fh)
                    files.append(("image", (os.path.basename(path), fh, content_type_for_path(path))))
                if mask_refs:
                    mask_path = output_file_from_url(mask_refs[0].get("url", ""))
                    if mask_path:
                        fh = open(mask_path, "rb")
                        opened.append(fh)
                        files.append(("mask", (os.path.basename(mask_path), fh, content_type_for_path(mask_path))))
                try:
                    response = await post_openai_edits(files)
                    if response.status_code >= 400:
                        edit_failed_status = response.status_code
                        edit_failed_text = response.text[:500]
                        response = None
                except httpx.HTTPError as e:
                    edit_failed_status = -1
                    edit_failed_text = str(e)
                    response = None
            finally:
                for fh in opened:
                    fh.close()
            # 2) edits 失败 → 非 GPT-Image-2 可回退到 /images/generations + JSON image:[urls/base64]（grsai 风格）
            if response is None:
                if is_gpt2:
                    raise HTTPException(
                        status_code=502,
                        detail=f"GPT-Image-2 编辑接口 /images/edits 调用失败：{edit_failed_text[:300] or edit_failed_status}。已停止自动重试，避免上游可能已扣费后再次请求。"
                    )
                print(f"/images/edits failed ({edit_failed_status}): {edit_failed_text[:200]} → 回退到 /images/generations + image:[] JSON")
                image_payload = [reference_to_data_url(ref, max_size=1536) for ref in image_refs[:4]]
                body = {
                    "model": model, "prompt": prompt, "size": size,
                    "response_format": "url", "n": 1,
                    "image": image_payload,
                }
                if quality:
                    body["quality"] = quality
                response = await client.post(gen_url, headers=api_headers(provider=provider), json=body)
                if response.status_code >= 400 and images_api_unsupported(response):
                    raise HTTPException(
                        status_code=502,
                        detail=f"编辑接口 /images/edits 调用失败，且该平台不支持 /images/generations：{edit_failed_text[:300] or edit_failed_status}"
                    )
        else:
            body = {"model": model, "prompt": prompt, "size": size, "response_format": "url", "n": 1}
            if quality:
                body["quality"] = quality
            response = await client.post(
                gen_url,
                headers=api_headers(provider=provider),
                json=body,
            )
            if response.status_code >= 400 and images_api_unsupported(response):
                response = await post_openai_edits()
        response.raise_for_status()
        raw = response.json()
        try:
            return extract_image(raw), raw
        except HTTPException:
            task_id = extract_task_id(raw)
            if not task_id:
                raise
        task_result = await wait_for_image_task(client, task_id, provider)
        return extract_image(task_result), task_result

def upstream_message_from_record(item):
    role = item.get("role")
    if role not in {"user", "assistant"} or item.get("type") == "image":
        return None
    refs = item.get("attachments") or []
    if refs and role == "user":
        content = [{"type": "text", "text": item.get("content", "")}]
        for ref in refs[:4]:
            url = reference_to_data_url(ref)
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": role, "content": content}
    return {"role": role, "content": item.get("content", "")}

# --- 路由接口 ---

@app.get("/")
async def index():
    return static_html_response("index.html")

@app.get("/api/view")
def view_image(filename: str, type: str = "input", subfolder: str = ""):
    # 先按原逻辑去各 ComfyUI 后端找
    for addr in COMFYUI_INSTANCES:
        try:
            url = f"http://{addr}/view"
            params = {"filename": filename, "type": type, "subfolder": subfolder}
            r = requests.get(url, params=params, timeout=1)
            if r.status_code == 200:
                return Response(content=r.content, media_type=r.headers.get('Content-Type'))
        except Exception:
            continue
    # 后端都拿不到时回退本地 assets/<input|output>/
    # 适用场景：画布通过 /api/ai/upload 把参考图直接落到本地 assets/input/，
    # 但 ComfyUI 的 input 可能因为重启/清理而丢失，导致 enhance/klein 等页面预览对比图 404
    if not subfolder and type in ("input", "output"):
        safe_name = os.path.basename(filename or "")
        if safe_name:
            local_path = output_path_for(safe_name, "input" if type == "input" else "output")
            if os.path.isfile(local_path):
                return FileResponse(local_path, media_type=content_type_for_path(local_path))
    raise HTTPException(status_code=404, detail="Image not found on any available backend")

@app.get("/api/download-output")
def download_output(url: str, name: str = ""):
    path = output_file_from_url(url)
    if not path:
        raise HTTPException(status_code=404, detail="文件不存在")
    filename = os.path.basename(name) if name else os.path.basename(path)
    return FileResponse(path, media_type=content_type_for_path(path), filename=filename)

@app.post("/api/upload")
async def upload_image(files: List[UploadFile] = File(...)):
    uploaded_files = []
    files_content = []
    for file in files:
        content = await file.read()
        files_content.append((file, content))

    for file, content in files_content:
        success_count = 0
        last_result = None
        for addr in COMFYUI_INSTANCES:
            try:
                files_data = {'image': (file.filename, content, file.content_type)}
                response = requests.post(f"http://{addr}/upload/image", files=files_data, timeout=5)
                if response.status_code == 200:
                    last_result = response.json()
                    success_count += 1
            except Exception as e:
                print(f"Upload error for {addr}: {e}")

        if success_count > 0 and last_result:
            uploaded_files.append({"comfy_name": last_result.get("name", file.filename)})
        else:
            raise HTTPException(status_code=500, detail="Failed to upload to any backend")

    return {"files": uploaded_files}

@app.post("/api/ai/upload")
async def upload_ai_reference(files: List[UploadFile] = File(...)):
    uploaded = []
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    video_exts = {".mp4", ".webm", ".mov", ".m4v"}
    audio_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    for file in files:
        content = await file.read()
        if not content:
            continue
        ext = os.path.splitext(file.filename or "")[1].lower()
        content_type = (file.content_type or "").lower()
        kind = "image"
        if ext in video_exts or content_type.startswith("video/"):
            kind = "video"
            if ext not in video_exts:
                ext = ".webm" if "webm" in content_type else ".mov" if "quicktime" in content_type else ".mp4"
        elif ext in audio_exts or content_type.startswith("audio/"):
            kind = "audio"
            if ext not in audio_exts:
                ext = ".wav" if "wav" in content_type else ".ogg" if "ogg" in content_type else ".m4a" if "mp4" in content_type else ".mp3"
        elif ext in image_exts or content_type.startswith("image/"):
            kind = "image"
            if ext not in image_exts:
                ext = ".jpg" if "jpeg" in content_type else ".webp" if "webp" in content_type else ".gif" if "gif" in content_type else ".png"
        else:
            continue
        filename = f"ai_ref_{uuid.uuid4().hex[:12]}{ext}"
        path = output_path_for(filename, "input")
        with open(path, "wb") as f:
            f.write(content)
        uploaded.append({"url": output_url_for(filename, "input"), "name": file.filename or filename, "kind": kind})
    return {"files": uploaded}

@app.post("/api/vectorize")
async def vectorize_image(
    file: UploadFile = File(...),
    preprocess_enabled: str = Form("true"),
    background_mode: str = Form("keep"),
    gray_layers: int = Form(12),
    bg_threshold: int = Form(248),
    blur_kernel: int = Form(3),
    colormode: str = Form("color"),
    hierarchical: str = Form("stacked"),
    mode: str = Form("polygon"),
    filter_speckle: int = Form(3),
    color_precision: int = Form(8),
    layer_difference: int = Form(6),
    gradient_step: Optional[int] = Form(None),
    corner_threshold: int = Form(70),
    length_threshold: float = Form(4.0),
    segment_length: Optional[float] = Form(None),
    max_iterations: int = Form(10),
    splice_threshold: int = Form(45),
    path_precision: int = Form(5),
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="请选择一张位图")
    if len(content) > LOCAL_IMAGE_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="图片文件过大")

    ext = os.path.splitext(file.filename or "")[1].lower()
    content_type = (file.content_type or "").lower()
    if ext == ".svg" or "svg" in content_type:
        raise HTTPException(status_code=400, detail="请输入 PNG、JPG、WebP 等位图文件")
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="文件类型不是图片")

    effective_layer_difference = gradient_step if gradient_step is not None else layer_difference
    effective_length_threshold = segment_length if segment_length is not None else length_threshold
    options = {
        "preprocess_enabled": normalized_bool(preprocess_enabled, True),
        "background_mode": normalized_choice(background_mode, {"keep", "remove"}, "keep"),
        "gray_layers": clamp_int(gray_layers, 12, 2, 32),
        "bg_threshold": clamp_int(bg_threshold, 248, 0, 255),
        "blur_kernel": normalize_blur_kernel(blur_kernel),
        "colormode": normalized_choice(colormode, {"color", "binary"}, "color"),
        "hierarchical": normalized_choice(hierarchical, {"stacked", "cutout"}, "stacked"),
        "mode": normalized_choice(mode, {"spline", "polygon", "none"}, "polygon"),
        "filter_speckle": clamp_int(filter_speckle, 3, 0, 256),
        "color_precision": clamp_int(color_precision, 8, 1, 8),
        "layer_difference": clamp_int(effective_layer_difference, 6, 0, 255),
        "corner_threshold": clamp_int(corner_threshold, 70, 0, 180),
        "length_threshold": clamp_float(effective_length_threshold, 4.0, 0.0, 20.0),
        "max_iterations": clamp_int(max_iterations, 10, 1, 32),
        "splice_threshold": clamp_int(splice_threshold, 45, 0, 180),
        "path_precision": clamp_int(path_precision, 5, 0, 8),
    }
    try:
        result = await asyncio.to_thread(vectorize_image_bytes, content, file.filename, options)
    except RuntimeError as e:
        if str(e) == "vtracer_missing":
            raise HTTPException(status_code=503, detail="vtracer 未安装，请先在画布服务 Python 环境安装依赖")
        if str(e) == "preprocess_missing":
            raise HTTPException(status_code=503, detail="OpenCV / NumPy 未安装，请先安装预处理依赖")
        raise HTTPException(status_code=500, detail="矢量化失败，请换一张图片或调整参数")
    except Exception as e:
        print(f"Vectorize error: {e}")
        raise HTTPException(status_code=500, detail="矢量化失败，请换一张图片或调整参数")
    return result

@app.post("/api/ai/import-local-image")
async def import_local_ai_reference(payload: LocalImageImportRequest, request: Request):
    ensure_same_origin_request(request)
    requested = [payload.path] if payload.path else []
    requested.extend(payload.paths or [])
    requested = [p for p in requested if str(p or "").strip()][:20]
    if not requested:
        raise HTTPException(status_code=400, detail="没有可导入的本地图片")
    return {"files": [import_local_image_file(normalize_local_image_path(path)) for path in requested]}

@app.get("/api/runninghub/app-info")
async def runninghub_app_info(webappId: str = ""):
    webapp_id = str(webappId or "").strip()
    if not webapp_id:
        raise HTTPException(status_code=400, detail="webappId 必填")
    provider = runninghub_provider()
    api_key = runninghub_api_key(provider)
    url = runninghub_endpoint_url(provider, f"/api/webapp/apiCallDemo?apiKey={urllib.parse.quote(api_key)}&webappId={urllib.parse.quote(webapp_id)}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=20.0)) as client:
        try:
            response = await client.get(url, headers=runninghub_app_headers(False))
            raw = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text[:500]) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"请求 RunningHub 应用信息失败：{exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=json.dumps(raw, ensure_ascii=False)[:500])
    if isinstance(raw, dict) and raw.get("code") not in (0, "0", None):
        raise HTTPException(status_code=400, detail=raw.get("msg") or f"RunningHub 查询失败 code={raw.get('code')}")
    data = raw.get("data") if isinstance(raw, dict) else {}
    return {"success": True, "data": data or {}}

@app.post("/api/runninghub/submit")
async def runninghub_submit(payload: RunningHubSubmitRequest):
    webapp_id = str(payload.webappId or "").strip()
    if not webapp_id:
        raise HTTPException(status_code=400, detail="webappId 必填")
    provider = runninghub_provider()
    api_key = runninghub_api_key(provider, use_wallet=payload.useWallet)
    body = {
        "apiKey": api_key,
        "webappId": webapp_id,
        "nodeInfoList": payload.nodeInfoList or [],
    }
    instance_type = str(payload.instanceType or "").strip()
    if instance_type:
        body["instanceType"] = instance_type
    url = runninghub_endpoint_url(provider, "/task/openapi/ai-app/run")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=180.0, write=120.0, pool=20.0)) as client:
        try:
            response = await client.post(url, headers=runninghub_app_headers(True, payload.useWallet), json=body)
            raw = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"提交 RunningHub 任务失败：{exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=json.dumps(raw, ensure_ascii=False)[:800])
    if isinstance(raw, dict) and raw.get("code") in (0, "0"):
        task_id = raw.get("data", {}).get("taskId") if isinstance(raw.get("data"), dict) else ""
        if not task_id:
            raise HTTPException(status_code=502, detail=f"RunningHub 未返回 taskId：{raw}")
        return {"success": True, "data": {"taskId": task_id, "raw": raw}}
    raise HTTPException(status_code=400, detail=(raw.get("msg") if isinstance(raw, dict) else "") or f"RunningHub 提交失败：{raw}")

@app.post("/api/runninghub/workflow-submit")
async def runninghub_workflow_submit(payload: RunningHubWorkflowSubmitRequest):
    workflow_id = str(payload.workflowId or "").strip()
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflowId 必填")
    provider = runninghub_provider()
    api_key = runninghub_api_key(provider, use_wallet=payload.useWallet)
    body = {
        "apiKey": api_key,
        "workflowId": workflow_id,
        "addMetadata": True,
    }
    if payload.nodeInfoList:
        body["nodeInfoList"] = payload.nodeInfoList
    workflow_payload = payload.workflow
    if workflow_payload:
        if isinstance(workflow_payload, (dict, list)):
            body["workflow"] = json.dumps(workflow_payload, ensure_ascii=False)
        else:
            body["workflow"] = str(workflow_payload)
    url = runninghub_endpoint_url(provider, "/task/openapi/create")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=180.0, write=120.0, pool=20.0)) as client:
        try:
            response = await client.post(url, headers=runninghub_app_headers(True, payload.useWallet), json=body)
            raw = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"提交 RunningHub 工作流失败：{exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=json.dumps(raw, ensure_ascii=False)[:800])
    if isinstance(raw, dict) and raw.get("code") in (0, "0"):
        task_id = raw.get("data", {}).get("taskId") if isinstance(raw.get("data"), dict) else ""
        if not task_id:
            raise HTTPException(status_code=502, detail=f"RunningHub 工作流未返回 taskId：{raw}")
        return {"success": True, "data": {"taskId": task_id, "raw": raw}}
    raise HTTPException(status_code=400, detail=(raw.get("msg") if isinstance(raw, dict) else "") or f"RunningHub 工作流提交失败：{raw}")

@app.get("/api/runninghub/workflow-info")
async def runninghub_workflow_info(workflowId: str = ""):
    workflow_id = str(workflowId or "").strip()
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflowId 必填")
    provider = runninghub_provider()
    api_key = runninghub_api_key(provider)
    url = runninghub_endpoint_url(provider, "/api/openapi/getJsonApiFormat")
    body = {"apiKey": api_key, "workflowId": workflow_id}
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=180.0, write=60.0, pool=20.0)) as client:
        try:
            response = await client.post(url, headers=runninghub_app_headers(True), json=body)
            raw = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"拉取 RunningHub 工作流参数失败：{exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=json.dumps(raw, ensure_ascii=False)[:800])
    if not isinstance(raw, dict) or raw.get("code") not in (0, "0"):
        raise HTTPException(status_code=400, detail=(raw.get("msg") if isinstance(raw, dict) else "") or f"RunningHub 工作流参数拉取失败：{raw}")
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    prompt = data.get("prompt")
    workflow_json = {}
    if isinstance(prompt, str) and prompt.strip():
        try:
            workflow_json = json.loads(prompt)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"RunningHub 工作流 JSON 解析失败：{exc}") from exc
    elif isinstance(prompt, dict):
        workflow_json = prompt
    node_info_list = runninghub_workflow_node_info_list(workflow_json)
    return {"success": True, "data": {"workflowId": workflow_id, "nodeInfoList": node_info_list, "raw": raw}}

@app.get("/api/runninghub/workflows")
def list_runninghub_workflows():
    with RUNNINGHUB_WORKFLOW_LOCK:
        store = load_runninghub_workflow_store()
    merged = {workflow_id: cfg for workflow_id, cfg in store.items() if isinstance(cfg, dict)}
    for provider in load_api_providers():
        if provider.get("id") != "runninghub":
            continue
        for entry in provider.get("rh_workflows") or []:
            workflow_id = runninghub_workflow_store_key(entry.get("workflowId") or entry.get("id"))
            if not workflow_id:
                continue
            provider_cfg = runninghub_provider_workflow_config(workflow_id, require_payload=False)
            if provider_cfg:
                merged[workflow_id] = runninghub_select_workflow_config(merged.get(workflow_id), provider_cfg)
    items = []
    for workflow_id, cfg in merged.items():
        if not isinstance(cfg, dict):
            continue
        items.append({
            "workflowId": workflow_id,
            "title": cfg.get("title") or workflow_id,
            "fieldCount": len(cfg.get("fields") or []),
            "configured": runninghub_workflow_config_has_payload(cfg),
            "updatedAt": cfg.get("updatedAt"),
            "description": cfg.get("description") or "",
        })
    items.sort(key=lambda item: item["title"])
    return {"workflows": items}

@app.get("/api/runninghub/workflows/{workflow_id:path}")
def get_runninghub_workflow(workflow_id: str):
    key = runninghub_workflow_store_key(workflow_id)
    if not key:
        raise HTTPException(status_code=400, detail="workflowId 必填")
    with RUNNINGHUB_WORKFLOW_LOCK:
        store = load_runninghub_workflow_store()
    cfg = store.get(key)
    provider_cfg = runninghub_provider_workflow_config(key, require_payload=False)
    cfg = runninghub_select_workflow_config(cfg, provider_cfg)
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=404, detail="RunningHub 工作流未找到")
    cfg["configured"] = runninghub_workflow_config_has_payload(cfg)
    return {"workflow": cfg}

@app.post("/api/runninghub/workflows/fetch")
async def fetch_runninghub_workflow(payload: RunningHubWorkflowConfig):
    workflow_id = runninghub_workflow_store_key(payload.workflowId)
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflowId 必填")
    provider = runninghub_provider()
    api_key = runninghub_api_key(provider)
    url = runninghub_endpoint_url(provider, "/api/openapi/getJsonApiFormat")
    body = {"apiKey": api_key, "workflowId": workflow_id}
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=180.0, write=60.0, pool=20.0)) as client:
        try:
            response = await client.post(url, headers=runninghub_app_headers(True), json=body)
            raw = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch RunningHub workflow parameters: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=json.dumps(raw, ensure_ascii=False)[:800])
    if not isinstance(raw, dict) or raw.get("code") not in (0, "0"):
        raise HTTPException(status_code=400, detail=(raw.get("msg") if isinstance(raw, dict) else "") or f"RunningHub workflow fetch failed: {raw}")
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    prompt = data.get("prompt")
    workflow_json = {}
    if isinstance(prompt, str) and prompt.strip():
        try:
            workflow_json = json.loads(prompt)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to parse RunningHub workflow JSON: {exc}") from exc
    elif isinstance(prompt, dict):
        workflow_json = prompt
    fields = runninghub_collect_workflow_fields(workflow_json)
    return {"success": True, "data": {"workflowId": workflow_id, "title": payload.title or workflow_id, "description": payload.description or "", "fields": fields, "workflowJson": workflow_json, "raw": raw}}

@app.put("/api/runninghub/workflows/{workflow_id:path}")
def save_runninghub_workflow(workflow_id: str, payload: RunningHubWorkflowConfig):
    key = runninghub_workflow_store_key(workflow_id)
    if not key:
        raise HTTPException(status_code=400, detail="workflowId 必填")
    fields = [
        field for field in (runninghub_normalize_field(item) for item in (payload.fields or []))
        if not runninghub_is_saved_link_field(field)
    ]
    cfg = {
        "workflowId": key,
        "title": (payload.title or key).strip() or key,
        "description": payload.description or "",
        "fields": fields,
        "workflowJson": payload.workflowJson or {},
        "optionalImageMode": payload.optionalImageMode or "prune-workflow",
        "raw": payload.raw or {},
        "updatedAt": now_ms(),
    }
    with RUNNINGHUB_WORKFLOW_LOCK:
        store = load_runninghub_workflow_store()
        store[key] = cfg
        save_runninghub_workflow_store(store)
    sync_runninghub_workflow_to_provider(cfg)
    return {"success": True, "workflow": cfg}

@app.delete("/api/runninghub/workflows/{workflow_id:path}")
def delete_runninghub_workflow(workflow_id: str):
    key = runninghub_workflow_store_key(workflow_id)
    if not key:
        raise HTTPException(status_code=400, detail="workflowId 必填")
    with RUNNINGHUB_WORKFLOW_LOCK:
        store = load_runninghub_workflow_store()
        provider_cfg = runninghub_provider_workflow_config(key)
        if key not in store and not provider_cfg:
            raise HTTPException(status_code=404, detail="RunningHub 工作流未找到")
        store.pop(key, None)
        save_runninghub_workflow_store(store)
    remove_runninghub_workflow_from_provider(key)
    return {"success": True}

@app.get("/api/runninghub/query")
async def runninghub_query(taskId: str = ""):
    task_id = str(taskId or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="taskId 必填")
    provider = runninghub_provider()
    api_key = runninghub_api_key(provider)
    url = runninghub_endpoint_url(provider, "/task/openapi/outputs")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=240.0, write=30.0, pool=20.0)) as client:
        try:
            response = await client.post(url, headers=runninghub_app_headers(True), json={"apiKey": api_key, "taskId": task_id})
            raw = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"查询 RunningHub 任务失败：{exc}") from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=json.dumps(raw, ensure_ascii=False)[:800])
        code = raw.get("code") if isinstance(raw, dict) else None
        status = "PENDING"
        urls = []
        if code in (0, "0"):
            status = "SUCCESS"
            for remote in runninghub_extract_outputs(raw.get("data")):
                try:
                    urls.append(await runninghub_store_remote_output(client, remote))
                except Exception:
                    urls.append(remote)
        elif code in (804, "804"):
            status = "RUNNING"
        elif code in (813, "813"):
            status = "QUEUED"
        elif code in (805, "805"):
            status = "FAILED"
        else:
            status = "UNKNOWN"
        return {"success": True, "data": {"status": status, "urls": urls, "failReason": runninghub_fail_reason(raw), "code": code, "raw": raw}}

@app.post("/api/runninghub/upload-asset")
async def runninghub_upload_asset(payload: RunningHubUploadAssetRequest):
    source_url = str(payload.url or "").strip()
    if not source_url:
        raise HTTPException(status_code=400, detail="url 必填")
    provider = runninghub_provider()
    api_key = runninghub_api_key(provider, use_wallet=payload.useWallet)
    filename = "asset.bin"
    content_type = "application/octet-stream"
    content = b""
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=240.0, write=240.0, pool=20.0), follow_redirects=True) as client:
        path = runninghub_local_asset_path(source_url)
        if path:
            filename = os.path.basename(path)
            content_type = content_type_for_path(path)
            with open(path, "rb") as f:
                content = f.read()
        elif source_url.startswith(("http://", "https://")):
            response = await client.get(source_url)
            if not response.is_success:
                raise HTTPException(status_code=400, detail=f"下载素材失败 HTTP {response.status_code}")
            content = response.content
            content_type = response.headers.get("content-type") or content_type
            filename = os.path.basename(urllib.parse.urlsplit(source_url).path) or filename
        else:
            raise HTTPException(status_code=400, detail=f"不支持的素材地址：{source_url}")
        if not content:
            raise HTTPException(status_code=400, detail="素材为空，无法上传到 RunningHub")
        upload_url = runninghub_endpoint_url(provider, "/task/openapi/upload")
        files = {"file": (filename, content, content_type)}
        data = {"apiKey": api_key, "fileType": "input"}
        try:
            response = await client.post(upload_url, headers=runninghub_app_headers(False, payload.useWallet), data=data, files=files)
            raw = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"上传素材到 RunningHub 失败：{exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=json.dumps(raw, ensure_ascii=False)[:800])
    if isinstance(raw, dict) and raw.get("code") in (0, "0") and isinstance(raw.get("data"), dict) and raw["data"].get("fileName"):
        return {"success": True, "data": {"fileName": raw["data"]["fileName"], "fileType": raw["data"].get("fileType") or content_type}}
    raise HTTPException(status_code=400, detail=(raw.get("msg") if isinstance(raw, dict) else "") or f"RunningHub 上传失败：{raw}")

@app.get("/api/config")
async def ai_config():
    preferred_chat_model = next((m for m in CHAT_MODELS if m == "gpt-5.5"), CHAT_MODELS[0] if CHAT_MODELS else CHAT_MODEL)
    providers = [public_provider(p) for p in load_api_providers()]
    return {
        "base_url": AI_BASE_URL,
        "chat_model": preferred_chat_model,
        "image_model": IMAGE_MODEL,
        "chat_models": CHAT_MODELS,
        "image_models": IMAGE_MODELS,
        "video_models": VIDEO_MODELS,
        "comfy_instances": COMFYUI_INSTANCES,
        "api_providers": providers,
        "has_api_key": bool(AI_API_KEY),
        "ms_chat_models": MODELSCOPE_CHAT_MODELS,
        "has_ms_key": bool(MODELSCOPE_API_KEY),
    }

@app.get("/api/models")
async def ai_models():
    return {"chat_models": CHAT_MODELS, "image_models": IMAGE_MODELS, "video_models": VIDEO_MODELS}

@app.get("/api/providers")
async def api_providers():
    return {"providers": [public_provider(p) for p in load_api_providers()]}

@app.put("/api/providers")
async def save_providers(payload: List[ApiProviderPayload]):
    providers = []
    env_updates = {}
    # 收集每个 item 的 primary 字段
    raw_primary_flags = [bool(getattr(item, "primary", False)) for item in payload]
    for item in payload:
        provider = normalize_provider(item.dict(exclude={"api_key"}))
        if provider["id"] == "runninghub":
            provider = preserve_runninghub_hidden_overrides(provider)
        if any(existing["id"] == provider["id"] for existing in providers):
            raise HTTPException(status_code=400, detail=f"API 平台 ID 重复：{provider['id']}")
        providers.append(provider)
        key_env = provider_key_env(provider["id"])
        if item.clear_key:
            env_updates[key_env] = ""
        elif item.api_key is not None and item.api_key.strip():
            env_updates[key_env] = item.api_key.strip()
        if provider["id"] == "runninghub":
            wallet_env = runninghub_wallet_key_env()
            if item.clear_wallet_key:
                env_updates[wallet_env] = ""
            elif item.wallet_api_key is not None and item.wallet_api_key.strip():
                env_updates[wallet_env] = item.wallet_api_key.strip()
        if provider["id"] == "comfly":
            env_updates["COMFLY_BASE_URL"] = provider["base_url"]
            env_updates["IMAGE_MODELS"] = ",".join(provider["image_models"])
            env_updates["CHAT_MODELS"] = ",".join(provider["chat_models"])
            env_updates["VIDEO_MODELS"] = ",".join(provider.get("video_models") or [])
        if provider["id"] == "modelscope":
            env_updates["MODELSCOPE_CHAT_MODELS"] = ",".join(provider["chat_models"])
        if provider["id"] == "runninghub":
            provider["protocol"] = "runninghub"
    if not providers:
        raise HTTPException(status_code=400, detail="至少保留一个 API 平台")
    # 强制最多一个 primary（取最后被标记的；都没标记则保持原样不强制）
    primary_indices = [i for i, flag in enumerate(raw_primary_flags) if flag]
    if primary_indices:
        winner = primary_indices[-1]
        for i, p in enumerate(providers):
            p["primary"] = (i == winner)
    save_api_providers(providers)
    if env_updates:
        update_env_values(env_updates)
        reload_env_globals()   # 立即将最新 env 值同步回模块全局变量，无需重启
    return {"providers": [public_provider(p) for p in providers]}

# --- ModelScope Token (从 env 读取，不再支持通过 UI 修改) ---

@app.get("/api/config/token")
async def get_global_token():
    # 优先读 env，回退到 global_config.json（兼容旧数据）
    if MODELSCOPE_API_KEY:
        return {"token": MODELSCOPE_API_KEY}
    if os.path.exists(GLOBAL_CONFIG_FILE):
        try:
            with open(GLOBAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return {"token": config.get("modelscope_token", "")}
        except:
            pass
    return {"token": ""}

# --- 在线生图 (COMFLY) ---

class TestConnectionPayload(BaseModel):
    base_url: str = ""
    api_key: str = ""
    provider_id: str = ""
    protocol: str = "openai"

def protocol_from_payload(payload):
    protocol = str(getattr(payload, "protocol", "") or "openai").strip().lower()
    return protocol if protocol in SUPPORTED_PROVIDER_PROTOCOLS else "openai"

def upstream_models_url(base_url: str, protocol: str):
    if protocol == "gemini":
        return f"{base_url}/models" if base_url.endswith("/v1beta") else f"{base_url}/v1beta/models"
    if protocol == "volcengine":
        return f"{base_url}/models" if base_url.endswith("/api/v3") else f"{base_url}/api/v3/models"
    if protocol == "runninghub":
        return f"{base_url}/openapi/v2/models"
    return f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"

def upstream_model_headers(api_key: str, protocol: str):
    if protocol == "gemini":
        return {"x-goog-api-key": api_key, "Accept": "application/json"}
    if protocol == "runninghub":
        return {"Authorization": api_key, "Accept": "application/json"}
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

def classify_upstream_model(mid):
    lc = str(mid or "").lower()
    video_keys = ["veo", "sora", "wan2", "wanx", "doubao-seedance", "doubao-1", "kling", "hailuo", "video", "t2v-", "i2v-", "s2v"]
    if any(k in lc for k in video_keys):
        return "video"
    if is_image_to_image_model_name(mid):
        return "image"
    image_keys = ["banana", "image", "dalle", "dall-e", "imagen", "flux", "stable", "sdxl", "midjourney", "nano-banana", "ideogram", "fal-ai", "z-image", "qwen-image", "klein", "seedream", "doubao-seedream", "text-to-image", "image-to-image"]
    if any(k in lc for k in image_keys):
        return "image"
    return "chat"

def parse_upstream_models(raw, protocol="openai"):
    items = raw.get("data") if isinstance(raw, dict) else None
    if not items and isinstance(raw, dict):
        items = raw.get("models") or raw.get("list") or []
    if not isinstance(items, list):
        items = []
    ids = []
    for it in items:
        if isinstance(it, str):
            mid = it
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
        else:
            mid = ""
        if mid:
            mid = str(mid)
            if protocol == "gemini" and mid.startswith("models/"):
                mid = mid[len("models/"):]
            ids.append(mid)
    ids = dedupe_model_ids(ids)
    grouped = {"image": [], "text_image": [], "image_to_image": [], "chat": [], "video": []}
    for mid in ids:
        cat = classify_upstream_model(mid)
        if cat == "image":
            grouped["image"].append(mid)
            grouped["image_to_image" if is_image_to_image_model_name(mid) else "text_image"].append(mid)
        else:
            grouped[cat].append(mid)
    return grouped, ids

@app.post("/api/providers/test-connection")
async def test_provider_connection(payload: TestConnectionPayload):
    """测试请求地址是否可用：调上游 /v1/models。验证通过时同时把模型清单按类别返回，避免再调一次拉取接口。"""
    base_url = (payload.base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写请求地址")
    if not re.match(r"^https?://", base_url):
        raise HTTPException(status_code=400, detail="请求地址必须以 http:// 或 https:// 开头")
    api_key = (payload.api_key or "").strip()
    if not api_key and payload.provider_id:
        api_key = os.getenv(runninghub_wallet_key_env(), "") if payload.provider_id == "runninghub" else ""
        if not api_key:
            api_key = os.getenv(provider_key_env(payload.provider_id), "")
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写或保存 API Key")
    protocol = protocol_from_payload(payload)
    url = upstream_models_url(base_url, protocol)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=upstream_model_headers(api_key, protocol))
        if resp.status_code >= 400:
            return {"ok": False, "status": resp.status_code, "message": resp.text[:300]}
        data = resp.json() if resp.text else {}
        grouped, ids = parse_upstream_models(data, protocol)
        return {
            "ok": True,
            "status": resp.status_code,
            "model_count": len(ids),
            "text_image_models": grouped["text_image"],
            "image_to_image_models": grouped["image_to_image"],
            "image_models": grouped["image"],
            "chat_models": grouped["chat"],
            "video_models": grouped["video"],
            "all": ids,
        }
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "message": str(e)[:300]}

@app.post("/api/providers/probe-async")
async def probe_async_endpoint(payload: TestConnectionPayload):
    """验证异步协议：用假 task_id 请求 GET /v1/tasks/{fake_id}。
    收到 400 Invalid task ID = 端点存在且 Key 有效；401/403 = Key 无效；404/连接失败 = 不支持异步端点。"""
    base_url = (payload.base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写请求地址")
    api_key = (payload.api_key or "").strip()
    if not api_key and payload.provider_id:
        api_key = os.getenv(runninghub_wallet_key_env(), "") if payload.provider_id == "runninghub" else ""
        if not api_key:
            api_key = os.getenv(provider_key_env(payload.provider_id), "")
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写或保存 API Key")
    tasks_base = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
    probe_url = f"{tasks_base}/tasks/healthcheck_probe_do_not_submit"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(probe_url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        sc = resp.status_code
        # 判断结果
        err_msg = ""
        if isinstance(body, dict):
            err = body.get("error") or {}
            if isinstance(err, dict):
                err_msg = str(err.get("message") or "").lower()
            else:
                err_msg = str(err).lower()
        # 400 + "invalid task id" → 端点存在，Key 有效
        if sc == 400 and "invalid task id" in err_msg:
            return {"ok": True, "status_code": sc, "message": "异步任务端点可用，API Key 已通过认证", "raw": body}
        # 401 / 403 → Key 无效
        if sc in (401, 403):
            return {"ok": False, "status_code": sc, "message": "API Key 无效或无权限", "raw": body}
        # 404 + 没有结构化错误 → 平台不支持此端点
        if sc == 404:
            return {"ok": False, "status_code": sc, "message": "平台不支持 /v1/tasks/ 端点，可能不是 APIMart 异步协议", "raw": body}
        # 其他 400 系 → 返回原始信息供参考
        if 400 <= sc < 500:
            return {"ok": None, "status_code": sc, "message": f"端点返回 {sc}，请查看原始响应判断", "raw": body}
        # 2xx → 意外成功（不太可能）
        if sc < 300:
            return {"ok": True, "status_code": sc, "message": f"端点返回 {sc}（意外成功）", "raw": body}
        return {"ok": False, "status_code": sc, "message": f"服务端错误 {sc}", "raw": body}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=str(e)[:300])

async def fetch_models_from_upstream(base_url: str, api_key: str, protocol: str = "openai"):
    """从上游模型列表端点拉取模型，并按名称做轻量分类。"""
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写请求地址")
    if not re.match(r"^https?://", base_url):
        raise HTTPException(status_code=400, detail="请求地址必须以 http:// 或 https:// 开头")
    api_key = (api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写或保存 API Key")
    protocol = protocol if protocol in SUPPORTED_PROVIDER_PROTOCOLS else "openai"
    url = upstream_models_url(base_url, protocol)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=upstream_model_headers(api_key, protocol))
            if resp.status_code >= 400:
                endpoint_label = "/v1beta/models" if protocol == "gemini" else "/api/v3/models" if protocol == "volcengine" else "/openapi/v2/models" if protocol == "runninghub" else "/v1/models"
                raise HTTPException(status_code=resp.status_code, detail=f"上游 {endpoint_label} 失败：{resp.text[:300]}")
            raw = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"请求上游模型列表失败：{e}")
    grouped, ids = parse_upstream_models(raw, protocol)
    source = "upstream"
    if protocol == "openai" and is_modelscope_base_url(base_url):
        library_grouped = await fetch_modelscope_library_models(base_url, api_key)
        grouped = {
            "text_image": dedupe_model_ids([*library_grouped["text_image"], *grouped["text_image"]]),
            "image_to_image": dedupe_model_ids([*library_grouped["image_to_image"], *grouped["image_to_image"]]),
            "chat": grouped["chat"],
            "video": grouped["video"],
        }
        grouped["image"] = dedupe_model_ids([*grouped["text_image"], *grouped["image_to_image"]])
        ids = dedupe_model_ids([*grouped["image"], *grouped["chat"], *grouped["video"]])
        source = "modelscope_hub"
    return {
        "total": len(ids),
        "source": source,
        "text_image_models": grouped["text_image"],
        "image_to_image_models": grouped["image_to_image"],
        "image_models": grouped["image"],
        "chat_models": grouped["chat"],
        "video_models": grouped["video"],
        "all": ids,
    }

@app.post("/api/providers/fetch-models")
async def fetch_upstream_models_from_payload(payload: TestConnectionPayload):
    """按页面当前表单值拉取模型，支持新增平台未保存时直接使用临时 Base URL / Key。"""
    api_key = (payload.api_key or "").strip()
    if not api_key and payload.provider_id:
        api_key = os.getenv(runninghub_wallet_key_env(), "") if payload.provider_id == "runninghub" else ""
        if not api_key:
            api_key = os.getenv(provider_key_env(payload.provider_id), "")
    return await fetch_models_from_upstream(payload.base_url, api_key, protocol_from_payload(payload))

@app.get("/api/providers/{provider_id}/fetch-models")
async def fetch_upstream_models(provider_id: str):
    """从已保存的上游 OpenAI 兼容接口拉取 /v1/models 列表，按名称智能分类为 image/chat/video。"""
    provider = get_api_provider_exact(provider_id)
    api_key = os.getenv(runninghub_wallet_key_env(), "") if provider["id"] == "runninghub" else ""
    if not api_key:
        api_key = os.getenv(provider_key_env(provider["id"]), "")
    if not api_key:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider_id} 未配置 API Key")
    return await fetch_models_from_upstream(provider.get("base_url") or "", api_key, provider_protocol(provider))

async def build_online_image_result(payload: OnlineImageRequest):
    provider = get_api_provider(payload.provider_id)
    default_model = (provider.get("text_image_models") or provider.get("image_models") or [IMAGE_MODEL])[0]
    model = selected_model(payload.model, default_model)
    refs = [ref.dict() for ref in payload.reference_images if ref.url]
    count = max(1, min(8, int(payload.n or 1)))
    async def generate_one():
        image_data, raw_item = await generate_ai_image(payload.prompt, payload.size, payload.quality, model, refs, provider["id"])
        local_url = await save_ai_image_to_output(image_data, prefix="online_")
        return local_url, raw_item
    try:
        generated = await asyncio.gather(*(generate_one() for _ in range(count)))
    except httpx.HTTPStatusError as exc:
        text = exc.response.text or ''
        # 把上游英文错误转成中文友好提示
        friendly = None
        m = re.search(r"longest edge must be less than or equal to (\d+)", text)
        if m:
            limit = m.group(1)
            friendly = f"该模型不支持当前分辨率：最长边超过 {limit}px。请把图片分辨率调低（例如换到 2K 或更小），或更换支持高分辨率的模型。"
        elif "Invalid size" in text or "invalid_value" in text:
            friendly = f"该模型不支持当前尺寸：{payload.size}。请尝试更换分辨率或模型。"
        elif "rate limit" in text.lower() or "429" in text:
            friendly = "请求过于频繁，已被上游限流，请稍后再试。"
        elif "Unauthorized" in text or "401" in text:
            friendly = "API Key 无效或已过期，请到「API 设置」检查 Key。"
        elif "model_not_found" in text or "channel not found" in text:
            friendly = f"上游平台找不到模型「{model}」可用通道。可能该模型未在此账号开通，请换一个已开通的模型。"
        detail = friendly or f"上游生图接口错误：{text[:300]}"
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"请求上游生图接口失败：{exc}") from exc

    local_urls = [url for url, _raw in generated if url]
    raw = generated[0][1] if generated else {}
    result = {
        "prompt": payload.prompt,
        "images": local_urls,
        "timestamp": time.time(),
        "type": "online",
        "model": model,
        "provider_id": provider["id"],
        "provider_name": provider.get("name") or provider["id"],
        "task_id": extract_task_id(raw) if isinstance(raw, dict) else None,
        "request_id": raw.get("id") if isinstance(raw, dict) else None,
        "params": {"provider_id": provider["id"], "model": model, "size": payload.size, "quality": payload.quality, "n": count, "reference_images": refs},
        "raw_usage": raw.get("usage") if isinstance(raw, dict) else None,
    }
    save_to_history(result)
    if GLOBAL_LOOP:
        asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(result), GLOBAL_LOOP)
    return result

@app.post("/api/online-image")
async def online_image(payload: OnlineImageRequest):
    return await build_online_image_result(payload)

async def run_canvas_image_task(task_id: str, payload: OnlineImageRequest):
    with CANVAS_TASK_LOCK:
        if task_id in CANVAS_TASKS:
            CANVAS_TASKS[task_id]["status"] = "running"
            CANVAS_TASKS[task_id]["updated_at"] = time.time()
    try:
        result = await build_online_image_result(payload)
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "succeeded",
                "result": result,
                "error": "",
                "updated_at": time.time(),
            })
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        status_code = getattr(exc, "status_code", 500)
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "failed",
                "error": str(detail),
                "status_code": status_code,
                "updated_at": time.time(),
            })

@app.post("/api/canvas-image-tasks")
async def create_canvas_image_task(payload: OnlineImageRequest):
    task_id = f"canvas_img_{uuid.uuid4().hex}"
    with CANVAS_TASK_LOCK:
        CANVAS_TASKS[task_id] = {
            "id": task_id,
            "type": "online-image",
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "error": "",
        }
    asyncio.create_task(run_canvas_image_task(task_id, payload))
    return {"task_id": task_id, "status": "queued"}

@app.get("/api/canvas-image-tasks/{task_id}")
async def get_canvas_image_task(task_id: str):
    with CANVAS_TASK_LOCK:
        task = dict(CANVAS_TASKS.get(task_id) or {})
    if not task:
        raise HTTPException(status_code=404, detail="画布任务不存在，可能服务已重启或任务已过期")
    return task

# --- Canvas Video ---

VIDEO_URL_KEYS = (
    "url", "video_url", "videoUrl", "mp4_url", "mp4Url",
    "output", "output_url", "outputUrl", "download_url", "downloadUrl",
    "video", "src", "uri", "preview_url", "previewUrl", "path",
)

def _collect_video_url(value, urls):
    if not value:
        return
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://") or value.startswith("/output/") or value.startswith("/assets/"):
            urls.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_video_url(item, urls)
        return
    if isinstance(value, dict):
        for key in ("videos", "outputs", "data", "result"):
            if key in value:
                _collect_video_url(value.get(key), urls)
        for key in VIDEO_URL_KEYS:
            if key in value:
                _collect_video_url(value.get(key), urls)

def video_output_urls(raw):
    urls = []
    if not isinstance(raw, dict):
        return urls
    candidates = [raw]
    data = raw.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                candidates.append(item)
    for node in list(candidates):
        result = node.get("result") if isinstance(node, dict) else None
        if isinstance(result, dict):
            candidates.append(result)
        elif isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    candidates.append(item)
    for node in candidates:
        if not isinstance(node, dict):
            continue
        for key in ("videos", "outputs"):
            value = node.get(key)
            if value:
                _collect_video_url(value, urls)
        for key in VIDEO_URL_KEYS:
            if key in node:
                _collect_video_url(node.get(key), urls)
    deduped = []
    for url in urls:
        if isinstance(url, str) and url and url not in deduped:
            deduped.append(url)
    return deduped

def video_api_root(provider):
    base_url = (provider.get("base_url") or AI_BASE_URL).rstrip("/")
    if base_url.endswith("/v1") or base_url.endswith("/v2"):
        base_url = base_url.rsplit("/", 1)[0]
    return base_url

VIDEO_TASK_SUCCESS_STATUSES = {
    "SUCCESS", "SUCCEED", "SUCCEEDED", "COMPLETED", "COMPLETE",
    "DONE", "FINISHED", "FINISH", "OK", "READY",
}
VIDEO_TASK_FAILURE_STATUSES = {
    "FAILURE", "FAILED", "FAIL", "ERROR", "ERRORED",
    "CANCELED", "CANCELLED", "TIMEOUT", "TIMEDOUT", "REJECTED", "EXPIRED",
}

async def wait_for_video_task(client, provider, task_id):
    base_url = video_api_root(provider)
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    if is_apimart_provider(provider):
        task_path = f"{base_url}/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/tasks/{task_id}"
        task_url = f"{task_path}?language=zh"
    else:
        task_url = f"{base_url}/v2/videos/generations/{task_id}"
    deadline = time.monotonic() + VIDEO_POLL_TIMEOUT
    delay = max(2.0, IMAGE_POLL_INTERVAL)
    last_payload = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        response = await client.get(task_url, headers=api_headers(provider=provider))
        response.raise_for_status()
        raw = response.json()
        last_payload = raw
        task_data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        status = str(task_data.get("status") or task_data.get("task_status") or raw.get("status") or raw.get("task_status") or "").upper()
        if status in VIDEO_TASK_SUCCESS_STATUSES:
            return raw
        # 部分上游不返回标准 status 字段，但已经返回了视频 URL —— 直接当成功处理
        if not status and video_output_urls(raw):
            return raw
        if status in VIDEO_TASK_FAILURE_STATUSES:
            error = task_data.get("error") if isinstance(task_data.get("error"), dict) else {}
            reason = task_data.get("fail_reason") or task_data.get("message") or error.get("message") or raw.get("error") or raw.get("message") or str(raw)
            raise HTTPException(status_code=502, detail=f"视频生成任务失败：{reason}")
        delay = min(delay * 1.6, 12)
    raise HTTPException(status_code=504, detail=f"视频生成任务超时：{last_payload or task_id}")

def apimart_video_size(size):
    value = str(size or "16:9").strip()
    if value == "keep_ratio":
        return "adaptive"
    allowed = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"}
    return value if value in allowed else "16:9"

@app.post("/api/canvas-video")
async def canvas_video(payload: CanvasVideoRequest):
    provider = get_api_provider(payload.provider_id)
    base_url = video_api_root(provider)
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    api_key = os.getenv(provider_key_env(provider["id"]), "")
    if not api_key:
        raise HTTPException(status_code=400, detail=f"未配置 {provider.get('name') or provider['id']} 的 API Key，请在 API 设置中填写。")
    is_apimart = is_apimart_provider(provider)
    submit_url = f"{base_url}/videos/generations" if is_apimart and base_url.endswith("/v1") else f"{base_url}/v1/videos/generations" if is_apimart else f"{base_url}/v2/videos/generations"
    requested_model = selected_model(payload.model, "veo3-fast")
    is_veo31 = is_apimart and is_apimart_veo31_model(requested_model)
    try:
        async with httpx.AsyncClient(timeout=VIDEO_POLL_TIMEOUT) as client:
            # --- 构造图片载荷 ---
            if is_apimart:
                # APIMart 只接受 http/https 或 asset:// URL，先上传本地图片取回网络 URL
                image_with_roles = []
                invalid_images = []  # 每项为 (原始 URL, 失败原因)
                apimart_model = apimart_veo31_model(requested_model) if is_veo31 else ""
                if apimart_model == "veo3.1-lite" and payload.images:
                    raise HTTPException(status_code=400, detail="veo3.1-lite 不支持图片输入，请改用 veo3.1-fast 或 veo3.1-quality。")
                image_limit = 0 if apimart_model == "veo3.1-lite" else (3 if is_veo31 else 9)
                for ref in payload.images[:image_limit]:
                    if not ref.url:
                        continue
                    role = str(ref.role or "").strip()
                    if not is_veo31 and role in {"first_frame", "last_frame", "reference_image"}:
                        up_url = await upload_image_for_apimart(client, provider, ref.url)
                        if valid_apimart_video_image_input(up_url):
                            image_with_roles.append({"url": up_url, "role": role})
                        else:
                            reason = up_url[4:] if isinstance(up_url, str) and up_url.startswith("ERR:") else "未知错误"
                            invalid_images.append((ref.url, reason))
                image_payload = []
                if not image_with_roles:
                    for ref in payload.images[:image_limit]:
                        if not ref.url:
                            continue
                        up_url = await upload_image_for_apimart(client, provider, ref.url)
                        if valid_apimart_video_image_input(up_url):
                            image_payload.append(up_url)
                        else:
                            reason = up_url[4:] if isinstance(up_url, str) and up_url.startswith("ERR:") else "未知错误"
                            invalid_images.append((ref.url, reason))
                if payload.images and not image_with_roles and not image_payload:
                    first_url, first_reason = invalid_images[0] if invalid_images else ("", "未知错误")
                    sample = invalid_video_image_preview(first_url)
                    raise HTTPException(status_code=400, detail=f"输入图片无法转换为视频接口支持的格式：{sample}\n原因：{first_reason}\n请确认本地文件存在且不超过 10MB；VEO3.1 需要图片是 APIMart 可访问的 http/https / asset:// / data URL。")
                # --- APIMart 请求体 ---
                if is_veo31:
                    model = apimart_model
                    body = {
                        "prompt": payload.prompt,
                        "model": model,
                        "duration": 8,
                        "aspect_ratio": apimart_veo31_aspect(payload.aspect_ratio),
                        "resolution": apimart_veo31_resolution(payload.resolution),
                    }
                    if image_payload and model != "veo3.1-lite":
                        video_images = image_payload[:3]
                        if model == "veo3.1-quality" and len(video_images) > 2:
                            video_images = video_images[:2]
                        body["image_urls"] = video_images
                        if len(video_images) == 2:
                            body["generation_type"] = "frame"
                        elif len(video_images) >= 3 and model != "veo3.1-quality":
                            body["generation_type"] = "reference"
                    if model != "veo3.1-lite":
                        body["official_fallback"] = False
                else:
                    body = {
                        "prompt": payload.prompt,
                        "model": selected_model(payload.model, "doubao-seedance-2.0"),
                        "duration": payload.duration,
                        "size": apimart_video_size(payload.aspect_ratio or payload.size),
                        "resolution": payload.resolution or "480p",
                    }
                    if image_with_roles:
                        body["image_with_roles"] = image_with_roles
                    elif image_payload:
                        body["image_urls"] = image_payload[:9]
                    if payload.videos:
                        body["video_urls"] = [v for v in payload.videos if v][:3]
                    if payload.seed is not None:
                        body["seed"] = payload.seed
                    if payload.return_last_frame:
                        body["return_last_frame"] = True
                    if payload.generate_audio:
                        body["generate_audio"] = True
            else:
                # 非 APIMart：data URL 方式（OpenAI / ComflyAI 接口）
                image_payload = []
                for ref in payload.images[:4]:
                    if ref.url:
                        image_payload.append(reference_to_data_url(ref.dict(), max_size=1536))
                body = {
                    "prompt": payload.prompt,
                    "model": selected_model(payload.model, "veo3-fast"),
                    "duration": payload.duration,
                    "watermark": payload.watermark,
                }
                if payload.aspect_ratio:
                    body["aspect_ratio"] = payload.aspect_ratio
                    body["ratio"] = payload.aspect_ratio
                if payload.size:
                    body["size"] = payload.size
                if payload.resolution:
                    body["resolution"] = payload.resolution
                if image_payload:
                    body["images"] = image_payload
                if payload.videos:
                    body["videos"] = [v for v in payload.videos if v]
                if payload.enhance_prompt:
                    body["enhance_prompt"] = True
                if payload.enable_upsample:
                    body["enable_upsample"] = True
                if payload.seed is not None:
                    body["seed"] = payload.seed
                if payload.camerafixed:
                    body["camerafixed"] = True
                if payload.return_last_frame:
                    body["return_last_frame"] = True
                if payload.generate_audio:
                    body["generate_audio"] = True
            # --- 发起视频生成请求 ---
            response = await client.post(submit_url, headers=api_headers(provider=provider), json=body)
            response.raise_for_status()
            try:
                raw = response.json()
            except Exception:
                # 上游返回了 HTML 错误页面或非 JSON 响应
                resp_text = response.text[:500]
                raise HTTPException(status_code=502, detail=f"上游视频接口返回非 JSON 响应（状态 {response.status_code}）：{resp_text}")
            task_id = extract_task_id(raw) or raw.get("task_id") or raw.get("id")
            result = raw
            if task_id and not video_output_urls(raw):
                result = await wait_for_video_task(client, provider, task_id)
            urls = video_output_urls(result)
            if not urls:
                raise HTTPException(status_code=502, detail=f"视频生成成功但没有返回视频：{result}")
            local_urls = [await save_remote_video_to_output(url) for url in urls]
            return {"videos": local_urls, "task_id": task_id, "raw": result}
    except httpx.HTTPStatusError as exc:
        text = exc.response.text
        try:
            requested_model = body.get("model", "") or payload.model or ""
        except NameError:
            requested_model = payload.model or ""
        provider_name = provider.get('name') or provider['id']
        # 1) 模型名不在上游支持范围 → 从错误信息里抽取合法列表展示
        valid_models_match = re.search(r"not in\s*\[([^\]]+)\]", text)
        if valid_models_match:
            valid_models = [m.strip() for m in valid_models_match.group(1).split(",") if m.strip()]
            sample = valid_models[:30]
            more = f"（共 {len(valid_models)} 个，仅显示前 {len(sample)} 个）" if len(valid_models) > len(sample) else ""
            hint = (
                f"上游「{provider_name}」不识别模型「{requested_model}」。\n\n"
                f"上游支持的视频模型清单{more}：\n  {', '.join(sample)}\n\n"
                f"请到「API 设置」里把视频模型改成上面列表中的一个。"
            )
            raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
        # 2) 模型名合法但账号没开通通道
        if "channel not found" in text or "model_not_found" in text:
            hint = (
                f"上游「{provider_name}」识别了模型「{requested_model}」，但你的 API Key 账号下**没有该模型的可用通道**。\n\n"
                f"原因：你的账号没开通这个模型的访问权限（付费/订阅相关）。\n\n"
                f"解决方法：\n"
                f"  1. 登录 {provider.get('base_url') or '上游平台'} 控制台，开通该模型 / 充值；\n"
                f"  2. 或在「API 设置」里把视频模型改成你账号已开通的型号（如 veo3-fast / veo2-fast / sora-2 等）。"
            )
            raise HTTPException(status_code=exc.response.status_code, detail=hint) from exc
        raise HTTPException(status_code=exc.response.status_code, detail=f"上游视频接口错误：{text}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"请求上游视频接口失败：{exc}") from exc

# --- Canvas LLM ---

@app.post("/api/canvas-llm")
async def canvas_llm(payload: CanvasLLMRequest):
    chat_base, chat_hdrs, model = resolve_chat_provider(payload.provider, payload.model, payload.ms_model)
    # 判断协议：APIMart 异步 vs 标准 OpenAI
    _llm_provider = get_api_provider(payload.provider) if payload.provider not in ("modelscope",) else {}
    _is_apimart = is_apimart_provider(_llm_provider)
    system_prompt = (payload.system_prompt or "").strip()
    upstream_messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
    for item in payload.messages[-MAX_HISTORY_MESSAGES:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and content:
            upstream_messages.append({"role": role, "content": content})
    # 构造用户消息：有图片时用 OpenAI vision 多模态格式
    image_inputs = [img for img in (payload.images or []) if is_image_reference_value(img)]
    if image_inputs:
        content_parts = [{"type": "text", "text": payload.message}]
        ok_imgs = 0
        for img in image_inputs[:8]:
            if not img or not isinstance(img, str):
                continue
            # 本地 /output/* 或 /assets/* 路径转为 data URL；http(s) 或 data URL 直接用
            if img.startswith("/output/") or img.startswith("/assets/"):
                ref_url = reference_to_data_url({"url": img}, max_size=1024)
            else:
                ref_url = img
            if not ref_url:
                continue
            content_parts.append({"type": "image_url", "image_url": {"url": ref_url}})
            ok_imgs += 1
        print(f"[canvas-llm] model={model} provider={payload.provider} text_len={len(payload.message)} images={ok_imgs}/{len(payload.images)}")
        upstream_messages.append({"role": "user", "content": content_parts})
    else:
        upstream_messages.append({"role": "user", "content": payload.message})
    raw = None
    try:
        async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
            req_body = {"model": model, "messages": upstream_messages}
            if _is_apimart:
                req_body["stream"] = False   # APIMart 默认流式，强制关闭
            response = await client.post(
                f"{chat_base}/chat/completions",
                headers=chat_hdrs,
                json=req_body,
            )
            response.raise_for_status()
            if not response.content:
                raise HTTPException(status_code=502, detail="上游接口返回了空响应")
            raw = response.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text or ""
        raise HTTPException(status_code=exc.response.status_code, detail=f"上游接口错误：{body}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"请求上游接口失败：{exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"解析上游响应失败：{exc}") from exc
    try:
        text = text_from_chat_response(raw).strip() if isinstance(raw, dict) else ""
        text = text or "接口返回了空回复。"
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"解析回复内容失败：{exc}") from exc
    raw_data = unwrap_apimart_response(raw) if isinstance(raw, dict) else {}
    return {"text": text, "model": model, "raw_usage": raw_data.get("usage")}

# --- 对话管理 ---

@app.get("/api/conversations")
async def conversations(request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"user_id": user_id, "conversations": list_conversations(user_id)}

@app.post("/api/conversations")
async def create_conversation(payload: ConversationCreateRequest, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"conversation": new_conversation(user_id, payload.title)}

@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    return {"conversation": load_conversation(user_id, conversation_id)}

@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    path = conversation_path(user_id, conversation_id)
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True}

# --- 画布管理 ---

@app.get("/api/canvases")
async def canvases():
    return {"canvases": list_canvases()}

@app.get("/api/canvases/trash")
async def trashed_canvases():
    return {"canvases": list_deleted_canvases(), "retention_days": 30}

@app.post("/api/canvases")
async def create_canvas(payload: CanvasCreateRequest):
    return {"canvas": new_canvas(payload.title, payload.icon, payload.kind)}

@app.get("/api/canvases/{canvas_id}/meta")
async def get_canvas_meta(canvas_id: str):
    canvas = load_canvas(canvas_id)
    return {
        "id": canvas.get("id"),
        "updated_at": canvas.get("updated_at", 0),
        "title": canvas.get("title", "未命名画布"),
        "icon": canvas.get("icon", "layers"),
        "kind": normalize_canvas_kind(canvas.get("kind")),
    }

@app.get("/api/canvases/{canvas_id}")
async def get_canvas(canvas_id: str):
    return {"canvas": load_canvas(canvas_id)}

@app.post("/api/canvas-assets/check")
async def check_canvas_assets(payload: CanvasAssetCheckRequest):
    result = {}
    for url in payload.urls[:3000]:
        text = str(url or "").strip()
        if not text:
            continue
        if text.startswith("/output/") or text.startswith("/assets/"):
            result[text] = bool(output_file_from_url(text))
        else:
            result[text] = True
    return {"exists": result}

@app.post("/api/canvas-assets/download")
async def download_canvas_assets(payload: CanvasAssetDownloadRequest):
    buffer = BytesIO()
    used_names = set()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for url in payload.urls[:1000]:
            text = str(url or "").strip()
            if not text or not (text.startswith("/output/") or text.startswith("/assets/")):
                continue
            path = output_file_from_url(text)
            if not path or not os.path.isfile(path):
                continue
            base = os.path.basename(path) or f"image-{count + 1}.png"
            name, ext = os.path.splitext(base)
            archive_name = base
            suffix = 2
            while archive_name in used_names:
                archive_name = f"{name}-{suffix}{ext}"
                suffix += 1
            used_names.add(archive_name)
            zf.write(path, archive_name)
            count += 1
    if count <= 0:
        raise HTTPException(status_code=404, detail="没有可下载的本地图片")
    buffer.seek(0)
    filename = re.sub(r'[\\/:*?"<>|]+', "_", payload.filename or "canvas-output-images.zip")
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    encoded = urllib.parse.quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    return Response(buffer.getvalue(), media_type="application/zip", headers=headers)

def sanitize_export_filename(name: str, fallback: str) -> str:
    base = os.path.basename(str(name or "").strip()) or fallback
    base = re.sub(r'[\\/:*?"<>|]+', "_", base)
    return base or fallback

def smart_group_export_folder(folder: str, group_name: str) -> str:
    text = str(folder or "").strip()
    if text:
        path = os.path.abspath(os.path.expanduser(text))
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_group = sanitize_export_filename(group_name or "group", "group")
        path = os.path.abspath(os.path.join(OUTPUT_DIR, "smart-groups", f"{safe_group}-{stamp}"))
    os.makedirs(path, exist_ok=True)
    return path

@app.post("/api/smart-canvas/group-export")
async def export_smart_canvas_group(payload: SmartCanvasGroupExportRequest):
    target_dir = smart_group_export_folder(payload.folder, payload.group_name)
    used_names = set()
    count = 0
    text_index = 1
    for item in payload.items[:2000]:
        kind = str(item.kind or "").lower()
        if kind == "text":
            text = str(item.text or "")
            if not text.strip():
                continue
            base = sanitize_export_filename(item.name or f"{text_index}.txt", f"{text_index}.txt")
            if not base.lower().endswith(".txt"):
                base += ".txt"
            text_index += 1
            name, ext = os.path.splitext(base)
            out_name = base
            suffix = 2
            while out_name in used_names:
                out_name = f"{name}-{suffix}{ext}"
                suffix += 1
            used_names.add(out_name)
            with open(os.path.join(target_dir, out_name), "w", encoding="utf-8") as f:
                f.write(text)
            count += 1
            continue
        src = output_file_from_url(item.url)
        if not src or not os.path.isfile(src):
            continue
        base = sanitize_export_filename(item.name or os.path.basename(src), os.path.basename(src) or f"asset-{count + 1}")
        name, ext = os.path.splitext(base)
        if not ext:
            _, src_ext = os.path.splitext(src)
            ext = src_ext or ".bin"
            base = name + ext
        out_name = base
        suffix = 2
        while out_name in used_names:
            out_name = f"{name}-{suffix}{ext}"
            suffix += 1
        used_names.add(out_name)
        shutil.copy2(src, os.path.join(target_dir, out_name))
        count += 1
    if count <= 0:
        raise HTTPException(status_code=404, detail="没有可导出的内容")
    return {"ok": True, "folder": target_dir, "count": count}

@app.get("/api/asset-library")
async def get_asset_library():
    return {"library": asset_library_response(load_asset_library())}

@app.post("/api/asset-library/categories")
async def create_asset_library_category(payload: AssetLibraryCategoryRequest):
    lib = load_asset_library()
    cat_type = "workflow" if str(payload.type or "").lower() == "workflow" else "image"
    category = {"id": f"cat_{uuid.uuid4().hex[:12]}", "name": sanitize_asset_name(payload.name, "新文件夹"), "type": cat_type, "items": []}
    library = find_asset_library_compat(lib, payload.library_id)
    target_categories = library.setdefault("categories", []) if library else lib.setdefault("categories", [])
    target_categories.append(category)
    if not payload.library_id or (library and library.get("id") == "default"):
        lib["categories"] = target_categories
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "category": category}

@app.patch("/api/asset-library/categories/{category_id}")
async def rename_asset_library_category(category_id: str, payload: AssetLibraryRenameRequest):
    lib = load_asset_library()
    cat = find_asset_category_compat(lib, category_id, payload.library_id)
    if not cat:
        raise HTTPException(status_code=404, detail="分类不存在")
    cat["name"] = sanitize_asset_name(payload.name, cat.get("name") or "新文件夹")
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "category": cat}

@app.delete("/api/asset-library/categories/{category_id}")
async def delete_asset_library_category(category_id: str, library_id: str = ""):
    lib = load_asset_library()
    cat = find_asset_category_compat(lib, category_id, library_id)
    if not cat:
        raise HTTPException(status_code=404, detail="分类不存在")
    if cat.get("type") == "workflow" and category_id == "workflows":
        raise HTTPException(status_code=400, detail="默认工作流分类不能删除")
    if library_id:
        library = find_asset_library_compat(lib, library_id)
        if library:
            library["categories"] = [c for c in library.get("categories", []) if c.get("id") != category_id]
    else:
        for library in lib.get("libraries", []) or []:
            library["categories"] = [c for c in library.get("categories", []) if c.get("id") != category_id]
    lib["categories"] = [c for c in lib.get("categories", []) if c.get("id") != category_id]
    save_asset_library(lib)
    return {"library": asset_library_response(lib)}

@app.post("/api/asset-library/items")
async def add_asset_library_item(payload: AssetLibraryAddRequest):
    lib = load_asset_library()
    cat = find_asset_category_compat(lib, payload.category_id, payload.library_id)
    if not cat:
        raise HTTPException(status_code=404, detail="分类不存在")
    if cat.get("type") != "image":
        raise HTTPException(status_code=400, detail="该分类暂不支持添加图片")
    src = output_file_from_url(payload.url)
    if not src:
        raise HTTPException(status_code=400, detail="只支持保存本地 /assets 或 /output 图片")
    ext = os.path.splitext(src)[1].lower() or ".png"
    if ext not in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
        ext = ".png"
    safe_name = sanitize_asset_name(payload.name or os.path.basename(src), "asset")
    if not os.path.splitext(safe_name)[1]:
        safe_name += ext
    dest_name = f"lib_{uuid.uuid4().hex[:12]}_{safe_name}"
    dest_path = os.path.join(ASSET_LIBRARY_DIR, dest_name)
    shutil.copy2(src, dest_path)
    item = {"id": f"asset_{uuid.uuid4().hex[:12]}", "name": os.path.splitext(safe_name)[0][:120], "url": f"/assets/library/{dest_name}", "created_at": now_ms()}
    cat.setdefault("items", []).append(item)
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "item": item}

@app.patch("/api/asset-library/items/{item_id}")
async def rename_asset_library_item(item_id: str, payload: AssetLibraryRenameRequest):
    lib = load_asset_library()
    for cat in iter_asset_categories_compat(lib):
        for item in cat.get("items", []):
            if item.get("id") == item_id:
                item["name"] = sanitize_asset_name(payload.name, item.get("name") or "asset")
                save_asset_library(lib)
                return {"library": asset_library_response(lib), "item": item}
    raise HTTPException(status_code=404, detail="资产不存在")

@app.delete("/api/asset-library/items/{item_id}")
async def delete_asset_library_item(item_id: str):
    lib = load_asset_library()
    removed = None
    for cat in iter_asset_categories_compat(lib):
        keep = []
        for item in cat.get("items", []):
            if item.get("id") == item_id:
                removed = item
            else:
                keep.append(item)
        cat["items"] = keep
    if not removed:
        raise HTTPException(status_code=404, detail="资产不存在")
    save_asset_library(lib)
    return {"library": asset_library_response(lib)}

@app.post("/api/asset-library/items/{item_id}/register-avatar")
async def register_asset_library_avatar(item_id: str, payload: dict):
    lib = load_asset_library()
    provider_id = str(payload.get("provider_id") or payload.get("providerId") or "")
    platform = str(payload.get("platform") or provider_id or "local")
    for cat in iter_asset_categories_compat(lib):
        for item in cat.get("items", []):
            if item.get("id") == item_id:
                regs = item.setdefault("registrations", {})
                regs[platform] = {
                    "status": "Unsupported",
                    "task_id": "",
                    "provider_id": provider_id,
                    "detail": "当前后端未配置数字人素材审核接口",
                    "updated_at": now_ms(),
                }
                save_asset_library(lib)
                return {"library": asset_library_response(lib), "item": item}
    raise HTTPException(status_code=404, detail="Asset item not found")

@app.post("/api/asset-library/items/{item_id}/avatar-status")
async def get_asset_library_avatar_status(item_id: str, payload: dict):
    lib = load_asset_library()
    provider_id = str(payload.get("provider_id") or payload.get("providerId") or "")
    platform = str(payload.get("platform") or provider_id or "local")
    for cat in iter_asset_categories_compat(lib):
        for item in cat.get("items", []):
            if item.get("id") == item_id:
                regs = item.setdefault("registrations", {})
                regs.setdefault(platform, {
                    "status": "Unsupported",
                    "task_id": "",
                    "provider_id": provider_id,
                    "detail": "当前后端未配置数字人素材审核接口",
                    "updated_at": now_ms(),
                })
                save_asset_library(lib)
                return {"library": asset_library_response(lib), "item": item}
    raise HTTPException(status_code=404, detail="Asset item not found")

@app.post("/api/asset-library/items/batch")
async def add_asset_library_items_batch(payload: AssetLibraryBatchAddRequest):
    lib = load_asset_library()
    cat = find_asset_category_compat(lib, payload.category_id, payload.library_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    added = []
    for entry in (payload.items or [])[:300]:
        src = output_file_from_url(entry.url)
        if not src or not os.path.isfile(src):
            continue
        item = make_asset_library_item(src, entry.name or os.path.basename(src))
        cat.setdefault("items", []).append(item)
        added.append(item)
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "items": added, "count": len(added)}

@app.post("/api/asset-library/items/delete")
async def delete_asset_library_items_batch(payload: AssetLibraryBatchDeleteRequest):
    ids = set(payload.ids or [])
    lib = load_asset_library()
    removed = 0
    for cat in iter_asset_categories_compat(lib):
        before = len(cat.get("items", []))
        cat["items"] = [item for item in cat.get("items", []) if item.get("id") not in ids]
        removed += before - len(cat["items"])
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "removed": removed}

@app.post("/api/asset-library/items/move")
async def move_asset_library_items(payload: AssetLibraryBatchMoveRequest):
    ids = set(payload.ids or [])
    lib = load_asset_library()
    target = find_asset_category_compat(lib, payload.target_category_id, payload.library_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target category not found")
    moved = []
    for cat in iter_asset_categories_compat(lib):
        keep = []
        for item in cat.get("items", []):
            if item.get("id") in ids:
                moved.append(item)
            else:
                keep.append(item)
        cat["items"] = keep
    target.setdefault("items", []).extend(moved)
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "moved": len(moved)}

@app.post("/api/asset-library/libraries")
async def create_asset_library_library(payload: AssetLibraryRequest):
    lib = load_asset_library()
    normalize_asset_libraries(lib)
    library = {
        "id": f"lib_{uuid.uuid4().hex[:12]}",
        "name": sanitize_asset_name(payload.name, "新资产库"),
        "type": payload.type or "asset",
        "categories": [],
        "created_at": now_ms(),
        "updated_at": now_ms(),
    }
    lib.setdefault("libraries", []).append(library)
    lib["active_library_id"] = library["id"]
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "item": library}

@app.patch("/api/asset-library/libraries/{library_id}")
async def rename_asset_library_library(library_id: str, payload: AssetLibraryRequest):
    lib = load_asset_library()
    library = find_asset_library_compat(lib, library_id)
    if not library:
        raise HTTPException(status_code=404, detail="Asset library not found")
    library["name"] = sanitize_asset_name(payload.name, library.get("name") or "资产库")
    library["updated_at"] = now_ms()
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "item": library}

@app.delete("/api/asset-library/libraries/{library_id}")
async def delete_asset_library_library(library_id: str):
    lib = load_asset_library()
    normalize_asset_libraries(lib)
    if library_id == "default":
        raise HTTPException(status_code=400, detail="Default asset library cannot be deleted")
    before = len(lib.get("libraries", []))
    lib["libraries"] = [item for item in lib.get("libraries", []) if item.get("id") != library_id]
    if len(lib["libraries"]) == before:
        raise HTTPException(status_code=404, detail="Asset library not found")
    lib["active_library_id"] = lib["libraries"][0].get("id") if lib.get("libraries") else "default"
    save_asset_library(lib)
    return {"library": asset_library_response(lib)}

@app.post("/api/asset-library/workflows/upload")
async def upload_asset_library_workflows(
    files: List[UploadFile] = File(...),
    library_id: str = Form(""),
    category_id: str = Form("")
):
    lib = load_asset_library()
    library = find_asset_library_compat(lib, library_id)
    cat = find_asset_category_compat(lib, category_id, library_id) if category_id else None
    if not cat:
        categories = library.get("categories") if library else lib.get("categories", [])
        cat = next((c for c in categories if c.get("type") == "workflow"), None)
    if not cat:
        cat = {"id": "workflows", "name": "工作流", "type": "workflow", "items": []}
        target = library.setdefault("categories", []) if library else lib.setdefault("categories", [])
        target.append(cat)
        if library and library.get("id") == "default":
            lib["categories"] = target
    target_dir = os.path.join(ASSET_LIBRARY_DIR, "workflows")
    os.makedirs(target_dir, exist_ok=True)
    added = []
    for upload in (files or [])[:80]:
        raw_name = upload.filename or "workflow.json"
        ext = os.path.splitext(raw_name)[1].lower()
        if ext not in (".json", ".zip"):
            continue
        safe_name = sanitize_asset_name(raw_name, "workflow.json")
        filename = f"{now_ms()}_{uuid.uuid4().hex[:8]}_{safe_name}"
        dst = os.path.join(target_dir, filename)
        with open(dst, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        item = {
            "id": f"workflow_{uuid.uuid4().hex[:12]}",
            "name": os.path.splitext(raw_name)[0],
            "url": f"/assets/library/workflows/{filename}",
            "kind": "workflow",
            "content_type": content_type_for_path(dst),
            "created_at": now_ms(),
        }
        cat.setdefault("items", []).append(item)
        added.append(item)
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "items": added, "count": len(added)}

@app.get("/api/local-assets")
async def list_local_assets():
    tree, items = local_upload_tree_and_items()
    return {"tree": tree, "items": items}

@app.post("/api/local-assets/upload")
async def upload_local_assets(files: List[UploadFile] = File(...), folder: str = Form("")):
    folder_rel = safe_rel_fragment(folder)
    target_dir = local_upload_abs(folder_rel)
    os.makedirs(target_dir, exist_ok=True)
    saved = []
    for upload in (files or [])[:200]:
        raw_name = upload.filename or "asset.bin"
        ext = os.path.splitext(raw_name)[1].lower()
        if ext not in LOCAL_ASSET_EXTS:
            continue
        safe_name = sanitize_asset_name(raw_name, "asset")
        filename = f"{now_ms()}_{uuid.uuid4().hex[:8]}_{safe_name}"
        rel_path = safe_rel_fragment(f"{folder_rel}/{filename}" if folder_rel else filename)
        dst = local_upload_abs(rel_path)
        with open(dst, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        item = local_upload_item(rel_path)
        if item:
            saved.append(item)
    tree, items = local_upload_tree_and_items()
    return {"tree": tree, "items": items, "files": saved, "count": len(saved)}

@app.post("/api/local-assets/delete")
async def delete_local_assets(payload: LocalAssetDeleteRequest):
    requested = []
    if payload.path:
        requested.append(payload.path)
    requested.extend(payload.paths or [])
    if payload.name:
        requested.append(payload.name)
    requested.extend(payload.names or [])
    removed = 0
    for rel in requested[:300]:
        path = local_upload_abs(rel)
        caption = local_upload_caption_path(rel)
        if os.path.isfile(path):
            os.remove(path)
            removed += 1
        if os.path.isfile(caption):
            os.remove(caption)
    tree, items = local_upload_tree_and_items()
    return {"tree": tree, "items": items, "removed": removed}

@app.post("/api/local-assets/folders")
async def create_local_asset_folder(payload: LocalAssetFolderRequest):
    parent = safe_rel_fragment(getattr(payload, "parent", "") or payload.folder)
    name = sanitize_asset_name(payload.name, "新文件夹")
    rel = safe_rel_fragment(f"{parent}/{name}" if parent else name)
    os.makedirs(local_upload_abs(rel), exist_ok=True)
    tree, items = local_upload_tree_and_items()
    return {"tree": tree, "items": items, "folder": {"name": name, "path": rel}}

@app.patch("/api/local-assets/folders")
async def rename_local_asset_folder(payload: LocalAssetFolderRequest):
    current = safe_rel_fragment(getattr(payload, "path", "") or payload.folder)
    if not current:
        raise HTTPException(status_code=400, detail="Root folder cannot be renamed")
    src = local_upload_abs(current)
    if not os.path.isdir(src):
        raise HTTPException(status_code=404, detail="Folder not found")
    parent = os.path.dirname(current).replace("\\", "/")
    name = sanitize_asset_name(payload.name, "新文件夹")
    rel = safe_rel_fragment(f"{parent}/{name}" if parent else name)
    dst = local_upload_abs(rel)
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="Target folder already exists")
    os.rename(src, dst)
    tree, items = local_upload_tree_and_items()
    return {"tree": tree, "items": items, "folder": {"name": name, "path": rel}}

@app.post("/api/local-assets/caption")
async def create_local_asset_caption(payload: LocalAssetCaptionRequest):
    requested = []
    if payload.path:
        requested.append(payload.path)
    if payload.name:
        requested.append(payload.name)
    requested.extend(payload.names or [])
    results = []
    for rel in requested[:100]:
        item = local_upload_item(rel)
        if not item:
            results.append({"name": rel, "ok": False, "error": "File not found"})
            continue
        caption = payload.caption or item.get("caption") or ""
        if caption:
            with open(local_upload_caption_path(rel), "w", encoding="utf-8") as f:
                f.write(caption)
        results.append({"name": rel, "ok": True, "caption": caption})
    return {"items": results, "count": len([r for r in results if r.get("ok")])}

@app.patch("/api/local-assets/caption")
async def update_local_asset_caption(payload: LocalAssetCaptionRequest):
    rel = payload.path or payload.name
    if not rel:
        raise HTTPException(status_code=400, detail="name is required")
    if not os.path.exists(local_upload_abs(rel)):
        raise HTTPException(status_code=404, detail="File not found")
    with open(local_upload_caption_path(rel), "w", encoding="utf-8") as f:
        f.write(payload.caption or "")
    return {"name": rel, "caption": payload.caption or "", "caption_file": f"{safe_rel_fragment(rel)}.caption.txt"}

@app.get("/api/shared-folders")
async def list_shared_folders():
    return load_shared_folders()

@app.post("/api/shared-folders")
async def add_shared_folder(payload: SharedFolderRegister):
    path = os.path.abspath(os.path.expanduser(payload.path or ""))
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="Folder not found")
    data = load_shared_folders()
    folder = {
        "id": f"shared_{uuid.uuid4().hex[:12]}",
        "name": payload.name or os.path.basename(path) or path,
        "path": path,
        "created_at": now_ms(),
    }
    data.setdefault("folders", []).append(folder)
    save_shared_folders(data)
    return data

@app.delete("/api/shared-folders/{folder_id}")
async def remove_shared_folder(folder_id: str):
    data = load_shared_folders()
    data["folders"] = [folder for folder in data.get("folders", []) if folder.get("id") != folder_id]
    save_shared_folders(data)
    return data

@app.get("/api/shared-folders/{folder_id}/tree")
async def get_shared_folder_tree(folder_id: str):
    data = load_shared_folders()
    folder = shared_folder_by_id(data, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Shared folder not found")
    return {"folder": folder, "tree": scan_shared_tree(folder)}

@app.get("/api/shared-folders/{folder_id}/file")
async def get_shared_folder_file(folder_id: str, path: str = ""):
    data = load_shared_folders()
    folder = shared_folder_by_id(data, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Shared folder not found")
    abs_path = shared_child_abs(folder, path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(abs_path, media_type=content_type_for_path(abs_path), filename=os.path.basename(abs_path))

@app.post("/api/shared-folders/import")
async def import_shared_folder_assets(payload: SharedFolderImportRequest):
    data = load_shared_folders()
    folder = shared_folder_by_id(data, payload.folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Shared folder not found")
    lib = load_asset_library()
    cat = find_asset_category_compat(lib, payload.category_id, payload.library_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    added = []
    for rel in (payload.paths or [])[:200]:
        abs_path = shared_child_abs(folder, rel)
        if not os.path.isfile(abs_path):
            continue
        item = make_asset_library_item(abs_path, os.path.basename(abs_path))
        cat.setdefault("items", []).append(item)
        added.append(item)
    save_asset_library(lib)
    return {"library": asset_library_response(lib), "items": added, "count": len(added)}

@app.get("/api/prompt-libraries")
async def get_prompt_libraries():
    return load_prompt_libraries()

@app.post("/api/prompt-libraries")
async def create_prompt_library(payload: PromptLibraryRequest):
    data = load_prompt_libraries()
    library = {
        "id": f"promptlib_{uuid.uuid4().hex[:12]}",
        "name": sanitize_asset_name(payload.name, "新提示词库"),
        "readonly": False,
        "categories": [{"id": "custom", "name": "自定义"}],
        "items": [],
        "created_at": now_ms(),
        "updated_at": now_ms(),
    }
    data.setdefault("libraries", []).append(library)
    data["active_library_id"] = library["id"]
    save_prompt_libraries(data)
    return data

@app.patch("/api/prompt-libraries/{library_id}")
async def rename_prompt_library(library_id: str, payload: PromptLibraryRequest):
    data = load_prompt_libraries()
    library = find_prompt_library(data, library_id)
    if not library:
        raise HTTPException(status_code=404, detail="Prompt library not found")
    library["name"] = sanitize_asset_name(payload.name, library.get("name") or "提示词库")
    library["updated_at"] = now_ms()
    save_prompt_libraries(data)
    return data

@app.delete("/api/prompt-libraries/{library_id}")
async def delete_prompt_library(library_id: str):
    data = load_prompt_libraries()
    data["libraries"] = [library for library in data.get("libraries", []) if library.get("id") != library_id]
    if not data["libraries"]:
        data = default_prompt_libraries()
    data["active_library_id"] = data["libraries"][0].get("id")
    save_prompt_libraries(data)
    return data

@app.post("/api/prompt-libraries/categories")
async def create_prompt_library_category(payload: PromptLibraryCategoryRequest, library_id: str = ""):
    data = load_prompt_libraries()
    library = find_prompt_library(data, payload.library_id or library_id)
    if not library:
        raise HTTPException(status_code=404, detail="Prompt library not found")
    category = {"id": f"pcat_{uuid.uuid4().hex[:12]}", "name": sanitize_asset_name(payload.name, "新分类")}
    library.setdefault("categories", []).append(category)
    save_prompt_libraries(data)
    return data

@app.patch("/api/prompt-libraries/categories/{category_id}")
async def rename_prompt_library_category(category_id: str, payload: PromptLibraryCategoryRequest, library_id: str = ""):
    data = load_prompt_libraries()
    library = find_prompt_library(data, payload.library_id or library_id)
    if not library:
        raise HTTPException(status_code=404, detail="Prompt library not found")
    for category in library.get("categories", []):
        if category.get("id") == category_id:
            category["name"] = sanitize_asset_name(payload.name, category.get("name") or "分类")
            save_prompt_libraries(data)
            return data
    raise HTTPException(status_code=404, detail="Prompt category not found")

@app.delete("/api/prompt-libraries/categories/{category_id}")
async def delete_prompt_library_category(category_id: str, library_id: str = ""):
    data = load_prompt_libraries()
    library = find_prompt_library(data, library_id)
    if not library or (library_id == "" and not any(c.get("id") == category_id for c in library.get("categories", []))):
        library = next((lib for lib in data.get("libraries", []) if any(c.get("id") == category_id for c in lib.get("categories", []))), None)
    if not library:
        raise HTTPException(status_code=404, detail="Prompt library not found")
    library["categories"] = [category for category in library.get("categories", []) if category.get("id") != category_id]
    for item in library.get("items", []):
        if item.get("category") == category_id:
            item["category"] = "custom"
    save_prompt_libraries(data)
    return data

@app.post("/api/prompt-libraries/items")
async def create_prompt_library_item(payload: dict):
    data = load_prompt_libraries()
    library = find_prompt_library(data, payload.get("library_id") or payload.get("libraryId") or "")
    if not library:
        raise HTTPException(status_code=404, detail="Prompt library not found")
    item = normalize_prompt_payload(payload)
    library.setdefault("items", []).append(item)
    save_prompt_libraries(data)
    return data

@app.patch("/api/prompt-libraries/items/{item_id}")
async def update_prompt_library_item(item_id: str, payload: dict):
    data = load_prompt_libraries()
    library = find_prompt_library(data, payload.get("library_id") or payload.get("libraryId") or "")
    if not library:
        raise HTTPException(status_code=404, detail="Prompt library not found")
    for index, existing in enumerate(library.get("items", [])):
        if existing.get("id") == item_id:
            updated = normalize_prompt_payload({**existing, **payload, "id": item_id})
            library["items"][index] = updated
            save_prompt_libraries(data)
            return data
    raise HTTPException(status_code=404, detail="Prompt item not found")

@app.delete("/api/prompt-libraries/items/{item_id}")
async def delete_prompt_library_item(item_id: str):
    data = load_prompt_libraries()
    removed = 0
    for library in data.get("libraries", []):
        before = len(library.get("items", []))
        library["items"] = [item for item in library.get("items", []) if item.get("id") != item_id]
        removed += before - len(library["items"])
    save_prompt_libraries(data)
    return {**data, "removed": removed}

@app.post("/api/prompt-libraries/items/delete")
async def delete_prompt_library_items(payload: PromptLibraryBatchDeleteRequest):
    ids = set(payload.ids or [])
    data = load_prompt_libraries()
    removed = 0
    for library in data.get("libraries", []):
        before = len(library.get("items", []))
        library["items"] = [item for item in library.get("items", []) if item.get("id") not in ids]
        removed += before - len(library["items"])
    save_prompt_libraries(data)
    return {**data, "removed": removed}

@app.get("/api/smart-canvas/prompt-templates")
async def get_smart_canvas_prompt_templates():
    path = prompt_template_markdown_path()
    if not os.path.exists(path):
        return {"templates": [], "markdown": ""}
    with open(path, "r", encoding="utf-8") as f:
        markdown = f.read()
    return {"templates": parse_prompt_template_markdown(), "markdown": markdown}

@app.put("/api/canvases/{canvas_id}")
async def update_canvas(canvas_id: str, payload: CanvasSaveRequest):
    canvas = load_canvas(canvas_id)
    current_updated_at = int(canvas.get("updated_at") or 0)
    if payload.base_updated_at and current_updated_at and int(payload.base_updated_at) < current_updated_at:
        raise HTTPException(status_code=409, detail={
            "message": "画布已被其他页面更新，已拒绝旧版本覆盖。",
            "canvas": canvas,
            "updated_at": current_updated_at,
        })
    canvas["title"] = (payload.title or canvas.get("title") or "未命名画布")[:80]
    canvas["icon"] = (payload.icon or canvas.get("icon") or "layers")[:32]
    canvas["kind"] = normalize_canvas_kind(canvas.get("kind"))
    canvas["nodes"] = payload.nodes
    canvas["connections"] = payload.connections
    canvas["viewport"] = payload.viewport
    canvas["logs"] = payload.logs[-500:]
    canvas["settings"] = payload.settings or {}
    save_canvas(canvas)
    await manager.broadcast_canvas_updated(canvas_id, int(canvas.get("updated_at") or now_ms()), payload.client_id)
    return {"canvas": canvas}

@app.delete("/api/canvases/{canvas_id}")
async def delete_canvas(canvas_id: str):
    canvas = load_canvas_any(canvas_id)
    if not canvas.get("deleted_at"):
        canvas["deleted_at"] = now_ms()
        save_canvas(canvas)
    return {"ok": True}

@app.post("/api/canvases/{canvas_id}/restore")
async def restore_canvas(canvas_id: str):
    canvas = load_canvas_any(canvas_id)
    if canvas.get("deleted_at"):
        canvas.pop("deleted_at", None)
        save_canvas(canvas)
    return {"canvas": canvas}

@app.delete("/api/canvases/{canvas_id}/purge")
async def purge_canvas(canvas_id: str):
    path = canvas_path(canvas_id)
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True}

# --- GPT 对话 ---

@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request, x_user_id: str = Header(default="")):
    user_id = safe_user_id(x_user_id, request)
    conversation = (
        load_conversation(user_id, payload.conversation_id)
        if payload.conversation_id
        else new_conversation(user_id, display_title(payload.message))
    )
    if not conversation.get("messages"):
        conversation["title"] = display_title(payload.message)

    refs = [ref.dict() for ref in payload.reference_images if ref.url]
    user_message = {
        "id": uuid.uuid4().hex,
        "role": "user",
        "content": payload.message,
        "created_at": now_ms(),
        "attachments": refs,
        "mode": payload.mode,
    }
    conversation["messages"].append(user_message)
    conversation["updated_at"] = now_ms()
    save_conversation(user_id, conversation)

    if payload.mode == "image":
        image_provider_id = payload.provider if payload.provider not in {"modelscope"} else "comfly"
        provider = get_api_provider(image_provider_id)
        default_model = (provider.get("text_image_models") or provider.get("image_models") or [IMAGE_MODEL])[0]
        model = selected_model(payload.image_model or payload.model, default_model)
        try:
            image_data, raw = await generate_ai_image(payload.message, payload.size, payload.quality, model, refs, provider["id"])
            local_url = await save_ai_image_to_output(image_data, prefix="chat_")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=f"上游生图接口错误：{exc.response.text}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"请求上游生图接口失败：{exc}") from exc
        assistant_message = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "type": "image",
            "content": payload.message,
            "image_url": local_url,
            "created_at": now_ms(),
            "model": model,
            "raw_usage": raw.get("usage") if isinstance(raw, dict) else None,
        }
    else:
        chat_base, chat_hdrs, model = resolve_chat_provider(payload.provider, payload.model, payload.ms_model)
        _conv_provider = get_api_provider(payload.provider) if payload.provider not in ("modelscope",) else {}
        _conv_is_apimart = is_apimart_provider(_conv_provider)
        history = conversation["messages"][-MAX_HISTORY_MESSAGES:]
        upstream_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in history:
            msg = upstream_message_from_record(item)
            if msg:
                upstream_messages.append(msg)
        try:
            async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
                conv_req_body = {"model": model, "messages": upstream_messages}
                if _conv_is_apimart:
                    conv_req_body["stream"] = False
                response = await client.post(
                    f"{chat_base}/chat/completions",
                    headers=chat_hdrs,
                    json=conv_req_body,
                )
                response.raise_for_status()
                raw = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=f"上游接口错误：{exc.response.text}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"请求上游接口失败：{exc}") from exc
        raw_data = unwrap_apimart_response(raw) if isinstance(raw, dict) else raw
        assistant_message = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": text_from_chat_response(raw).strip() or "接口返回了空回复。",
            "created_at": now_ms(),
            "model": model,
            "raw_usage": raw_data.get("usage") if isinstance(raw_data, dict) else None,
        }

    conversation["messages"].append(assistant_message)
    conversation["updated_at"] = now_ms()
    save_conversation(user_id, conversation)
    return {"conversation": conversation, "message": assistant_message}

@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request, x_user_id: str = Header(default="")):
    if payload.mode == "image":
        raise HTTPException(status_code=400, detail="图片模式请使用 /api/chat")

    user_id = safe_user_id(x_user_id, request)
    conversation = (
        load_conversation(user_id, payload.conversation_id)
        if payload.conversation_id
        else new_conversation(user_id, display_title(payload.message))
    )
    if not conversation.get("messages"):
        conversation["title"] = display_title(payload.message)

    refs = [ref.dict() for ref in payload.reference_images if ref.url]
    user_message = {
        "id": uuid.uuid4().hex,
        "role": "user",
        "content": payload.message,
        "created_at": now_ms(),
        "attachments": refs,
        "mode": payload.mode,
    }
    conversation["messages"].append(user_message)
    conversation["updated_at"] = now_ms()
    save_conversation(user_id, conversation)

    chat_base, chat_hdrs, model = resolve_chat_provider(payload.provider, payload.model, payload.ms_model)
    history = conversation["messages"][-MAX_HISTORY_MESSAGES:]
    upstream_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history:
        msg = upstream_message_from_record(item)
        if msg:
            upstream_messages.append(msg)

    async def stream():
        content_parts = []
        raw_usage = None
        yield sse_event({"type": "meta", "conversation": conversation})
        try:
            async with httpx.AsyncClient(timeout=AI_REQUEST_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    f"{chat_base}/chat/completions",
                    headers=chat_hdrs,
                    json={"model": model, "messages": upstream_messages, "stream": True},
                ) as response:
                    if response.status_code >= 400:
                        detail = await response.aread()
                        yield sse_event({"type": "error", "detail": f"上游接口错误：{detail.decode('utf-8', errors='ignore')}"})
                        return
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            break
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(chunk, dict) and chunk.get("usage"):
                            raw_usage = chunk.get("usage")
                        delta = text_delta_from_chat_chunk(chunk)
                        if delta:
                            content_parts.append(delta)
                            yield sse_event({"type": "delta", "delta": delta})
        except httpx.HTTPError as exc:
            yield sse_event({"type": "error", "detail": f"请求上游接口失败：{exc}"})
            return

        assistant_message = {
            "id": uuid.uuid4().hex,
            "role": "assistant",
            "content": "".join(content_parts).strip() or "接口返回了空回复。",
            "created_at": now_ms(),
            "model": model,
            "raw_usage": raw_usage,
        }
        conversation["messages"].append(assistant_message)
        conversation["updated_at"] = now_ms()
        save_conversation(user_id, conversation)
        yield sse_event({"type": "done", "conversation": conversation, "message": assistant_message})

    return StreamingResponse(stream(), media_type="text/event-stream")

# --- 历史记录 ---

@app.get("/api/history")
async def get_history_api(type: str = None):
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if type:
                    data = [item for item in data if item.get("type", "zimage") == type]
                data = [item for item in data if item.get("images") and len(item["images"]) > 0]

                def sort_key(item):
                    ts = item.get("timestamp", 0)
                    if isinstance(ts, (int, float)):
                        return float(ts)
                    return 0

                data.sort(key=sort_key, reverse=True)
                return data
        except Exception as e:
            print(f"读取历史文件失败: {e}")
            return []
    return []

@app.get("/api/queue_status")
async def get_queue_status(client_id: str):
    with QUEUE_LOCK:
        total = len(QUEUE)
        positions = [i + 1 for i, t in enumerate(QUEUE) if t["client_id"] == client_id]
        position = positions[0] if positions else 0
    return {"total": total, "position": position}

@app.post("/api/history/delete")
async def delete_history(req: DeleteHistoryRequest):
    if not os.path.exists(HISTORY_FILE):
        return {"success": False, "message": "History file not found"}
    try:
        with HISTORY_LOCK:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
            target_record = None
            new_history = []
            for item in history:
                is_match = False
                item_ts = item.get("timestamp", 0)
                if isinstance(req.timestamp, (int, float)) and isinstance(item_ts, (int, float)):
                    if abs(float(item_ts) - float(req.timestamp)) < 0.001:
                        is_match = True
                elif str(item_ts) == str(req.timestamp):
                    is_match = True
                if is_match:
                    target_record = item
                else:
                    new_history.append(item)
            if target_record:
                with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(new_history, f, ensure_ascii=False, indent=4)

        if target_record:
            for img_url in target_record.get("images", []):
                file_path = output_file_from_url(img_url)
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        print(f"Failed to delete file {file_path}: {e}")
            return {"success": True}
        else:
            return {"success": False, "message": "Record not found"}
    except Exception as e:
        print(f"Delete history error: {e}")
        return {"success": False, "message": str(e)}

# --- ModelScope 角度控制 ---

@app.post("/api/angle/poll_status")
async def poll_angle_cloud(req: CloudPollRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    task_id = req.task_id
    print(f"Resuming polling for Angle Task: {task_id}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(300):
                await asyncio.sleep(2)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"cloud_angle_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception:
                            local_path = img_url

                        record = {"timestamp": time.time(), "prompt": f"Resumed {task_id}", "images": [local_path], "type": "angle"}
                        save_to_history(record)
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "SUCCEED", "task_id": task_id}, req.client_id)
                        return {"url": local_path}

                    elif status == "FAILED":
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "FAILED", "task_id": task_id}, req.client_id)
                        raise Exception(f"ModelScope task failed: {data}")

                    if i % 5 == 0 and req.client_id:
                        await manager.send_personal_message({
                            "type": "cloud_status", "status": f"{status} ({i}/300)",
                            "task_id": task_id, "progress": i, "total": 300
                        }, req.client_id)

                except Exception as loop_e:
                    print(f"Angle polling error: {loop_e}")
                    continue

            if req.client_id:
                await manager.send_personal_message({"type": "cloud_status", "status": "TIMEOUT", "task_id": task_id}, req.client_id)
            return {"status": "timeout", "task_id": task_id, "message": "Task still pending"}

    except Exception as e:
        print(f"Angle polling error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/angle/generate")
async def generate_angle_cloud(req: CloudGenRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    model = selected_model(req.model, "Qwen/Qwen-Image-Edit-2511")
    payload = {
        "model": model,
        "prompt": req.prompt.strip(),
        "image_url": [modelscope_image_url(url, max_size=1536) for url in req.image_urls]
    }
    if req.resolution:
        payload["size"] = modelscope_size(req.resolution)
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            submit_res = await client.post(f"{base_url}v1/images/generations", headers=headers, json=payload)
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            print(f"Angle Task submitted, ID: {task_id}")

            for i in range(300):
                await asyncio.sleep(2)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"cloud_angle_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception:
                            local_path = img_url

                        record = {"timestamp": time.time(), "prompt": req.prompt, "images": [local_path], "type": "angle"}
                        save_to_history(record)
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "SUCCEED", "task_id": task_id}, req.client_id)
                        if GLOBAL_LOOP:
                            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(record), GLOBAL_LOOP)
                        return {"url": local_path, "task_id": task_id}

                    elif status == "FAILED":
                        if req.client_id:
                            await manager.send_personal_message({"type": "cloud_status", "status": "FAILED", "task_id": task_id}, req.client_id)
                        raise Exception(f"ModelScope task failed: {data}")

                    if i % 5 == 0 and req.client_id:
                        await manager.send_personal_message({
                            "type": "cloud_status", "status": f"{status} ({i}/300)",
                            "task_id": task_id, "progress": i, "total": 300
                        }, req.client_id)

                except Exception as loop_e:
                    print(f"Angle polling error: {loop_e}")
                    continue

            if req.client_id:
                await manager.send_personal_message({"type": "cloud_status", "status": "TIMEOUT", "task_id": task_id}, req.client_id)
            return {"status": "timeout", "task_id": task_id, "message": "Task still pending"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Angle generation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- ModelScope Z-Image 云端生图 ---

@app.post("/generate")
async def generate_cloud(req: CloudGenRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
    }
    model = selected_model(req.model, MODELSCOPE_DEFAULT_IMAGE_MODEL)
    image_refs = list(req.image_urls or [])
    if isinstance(req.image_url, list):
        image_refs.extend(req.image_url)
    elif req.image_url:
        image_refs.append(req.image_url)
    image_refs = [str(url).strip() for url in image_refs if str(url or "").strip()]
    if is_modelscope_image_edit_model(model) and not image_refs:
        raise HTTPException(
            status_code=400,
            detail=f"{model} 是图像编辑模型，需要 image_url。请在图片编辑/角度控制中使用，或在文生图中选择 Tongyi-MAI/Z-Image-Turbo、Qwen/Qwen-Image-2512、FLUX.2-klein-9B。"
        )
    payload = {
        "model": model,
        "prompt": req.prompt.strip(),
        "size": modelscope_size(req.resolution),
        "n": 1
    }
    if image_refs:
        image_urls = [modelscope_image_url(url, max_size=1536) for url in image_refs]
        if image_urls:
            payload["image_url"] = image_urls
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            submit_res = await client.post(
                f"{base_url}v1/images/generations",
                headers={**headers, "X-ModelScope-Async-Mode": "true"},
                json=payload
            )
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            print(f"ModelScope image task submitted ({model}), ID: {task_id}")

            for i in range(200):
                await asyncio.sleep(3)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")

                    if i % 5 == 0:
                        print(f"Task {task_id} status check {i}: {status}")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"cloud_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception as dl_e:
                            print(f"Download error: {dl_e}")
                            local_path = img_url

                        record = {"timestamp": time.time(), "prompt": req.prompt, "images": [local_path], "type": "cloud", "model": model}
                        save_to_history(record)
                        try:
                            await manager.broadcast_new_image(record)
                        except Exception:
                            pass
                        return {"url": local_path, "model": model}

                    elif status == "FAILED":
                        raise Exception(f"ModelScope task failed: {data}")

                except Exception as loop_e:
                    print(f"Polling error (retrying): {loop_e}")
                    continue

            raise Exception("Cloud generation timeout")

    except HTTPException:
        raise
    except Exception as e:
        print(f"Cloud generation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- ModelScope 通用图片生成（支持图生图） ---

@app.post("/api/ms/generate")
async def ms_generate(req: MsGenerateRequest):
    base_url = 'https://api-inference.modelscope.cn/'
    clean_token = (req.api_key or MODELSCOPE_API_KEY).strip()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未配置 ModelScope API Key，请在 API 设置中填写，或重新保存 ModelScope Token。")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    ms_model, ms_loras = normalize_ms_enhance_request(req.model, req.loras)
    payload = {
        "model": ms_model,
        "prompt": req.prompt.strip(),
    }
    if req.width and req.height:
        payload["width"] = req.width
        payload["height"] = req.height
        payload["size"] = modelscope_size(req.size or f"{req.width}x{req.height}")
    elif req.size:
        payload["size"] = modelscope_size(req.size)
    if req.image_urls:
        payload["image_url"] = [modelscope_image_url(url, max_size=1536) for url in req.image_urls]
    if ms_loras is not None:
        payload["loras"] = ms_loras

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            submit_res = await client.post(
                f"{base_url}v1/images/generations",
                headers=headers,
                json=payload
            )
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            print(f"MS Generate Task submitted ({ms_model}), ID: {task_id}")

            TERMINAL_FAILED_STATUSES = {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}

            for i in range(300):
                await asyncio.sleep(2)
                try:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")
                    print(f"MS Task {task_id} poll {i}: status={status}")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        local_path = ""
                        try:
                            async with httpx.AsyncClient() as dl_client:
                                img_res = await dl_client.get(img_url)
                                if img_res.status_code == 200:
                                    filename = f"ms_{ms_model.replace('/', '_').replace(':', '_')}_{int(time.time())}.png"
                                    file_path = output_path_for(filename, "output")
                                    with open(file_path, "wb") as f:
                                        f.write(img_res.content)
                                    local_path = output_url_for(filename, "output")
                                else:
                                    local_path = img_url
                        except Exception:
                            local_path = img_url

                        record = {
                            "timestamp": time.time(),
                            "prompt": req.prompt,
                            "images": [local_path],
                            "type": "klein",
                            "model": ms_model,
                        }
                        save_to_history(record)
                        if GLOBAL_LOOP:
                            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(record), GLOBAL_LOOP)
                        return {"url": local_path, "task_id": task_id}

                    elif status in TERMINAL_FAILED_STATUSES:
                        error_info = data.get("error_info") or data.get("message") or data.get("detail") or str(data)
                        raise HTTPException(status_code=502, detail=f"MS task {status}: {error_info}")

                except HTTPException:
                    raise
                except Exception as loop_e:
                    print(f"MS polling error: {loop_e}")
                    continue

            raise HTTPException(status_code=504, detail="MS 生图超时")

    except HTTPException:
        raise
    except Exception as e:
        print(f"MS generate error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- 本地 ComfyUI 生图 ---

@app.post("/api/generate")
def generate(req: GenerateRequest):
    global NEXT_TASK_ID
    current_task = None
    target_backend = None
    with QUEUE_LOCK:
        task_id = NEXT_TASK_ID
        NEXT_TASK_ID += 1
        current_task = {"task_id": task_id, "client_id": req.client_id}
        QUEUE.append(current_task)

    try:
        required_images = collect_required_comfy_media(req.params)

        target_backend = get_best_backend(required_images)
        with LOAD_LOCK:
            BACKEND_LOCAL_LOAD[target_backend] += 1

        for image_name in required_images:
            need_sync = False
            try:
                check_url = f"http://{target_backend}/view?filename={urllib.parse.quote(image_name)}&type=input"
                resp = requests.get(check_url, stream=True, timeout=0.5)
                resp.close()
                if resp.status_code != 200:
                    need_sync = True
            except:
                need_sync = True

            if need_sync:
                image_content = None
                image_type = "image/png"
                for addr in COMFYUI_INSTANCES:
                    if addr == target_backend: continue
                    try:
                        src_url = f"http://{addr}/view?filename={urllib.parse.quote(image_name)}&type=input"
                        r = requests.get(src_url, timeout=5)
                        if r.status_code == 200:
                            image_content = r.content
                            image_type = r.headers.get("Content-Type", "image/png")
                            break
                    except: continue

                if image_content:
                    try:
                        files = {'image': (image_name, image_content, image_type)}
                        requests.post(f"http://{target_backend}/upload/image", files=files, timeout=10)
                    except Exception as e:
                        print(f"Sync upload failed: {e}")

        workflow_path = os.path.join(WORKFLOW_DIR, req.workflow_json)
        if not os.path.exists(workflow_path) and req.workflow_json in {ZIMAGE_DEFAULT_WORKFLOW, "Z-Image.json"}:
            workflow_path = WORKFLOW_PATH
        if not os.path.exists(workflow_path):
            raise Exception(f"Workflow file not found: {req.workflow_json}")

        with open(workflow_path, 'r', encoding='utf-8') as f:
            workflow = json.load(f)

        seed = random.randint(1, 10**15)

        apply_prompt_to_workflow(workflow, req.prompt, req.params)
        apply_dimensions_to_workflow(workflow, req.width, req.height)
        if "22" in workflow:
            workflow["22"]["inputs"]["seed"] = seed
        if "158" in workflow:
            workflow["158"]["inputs"]["noise_seed"] = seed
        for node_id in ["146", "181"]:
            if node_id in workflow and "inputs" in workflow[node_id] and "seed" in workflow[node_id]["inputs"]:
                workflow[node_id]["inputs"]["seed"] = seed
        if "184" in workflow and "inputs" in workflow["184"] and "seed" in workflow["184"]["inputs"]:
            workflow["184"]["inputs"]["seed"] = seed
        if "172" in workflow and "inputs" in workflow["172"] and "seed" in workflow["172"]["inputs"]:
            workflow["172"]["inputs"]["seed"] = seed % 4294967295
        if "14" in workflow and "inputs" in workflow["14"] and "seed" in workflow["14"]["inputs"]:
            workflow["14"]["inputs"]["seed"] = seed

        for node_id, node_inputs in req.params.items():
            if node_id in workflow:
                if "inputs" not in workflow[node_id]:
                    workflow[node_id]["inputs"] = {}
                for input_name, value in node_inputs.items():
                    workflow[node_id]["inputs"][input_name] = value

        p = {"prompt": workflow, "client_id": CLIENT_ID}
        data = json.dumps(p).encode('utf-8')
        try:
            post_req = urllib.request.Request(f"http://{target_backend}/prompt", data=data)
            prompt_id = json.loads(urllib.request.urlopen(post_req, timeout=10).read())['prompt_id']
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            raise Exception(f"HTTP Error {e.code}: {error_body}")

        history_data = None
        for i in range(COMFYUI_HISTORY_TIMEOUT):
            try:
                res = get_comfy_history(target_backend, prompt_id)
                if prompt_id in res:
                    history_data = res[prompt_id]
                    break
            except Exception:
                pass
            time.sleep(1)

        if not history_data:
            raise Exception("ComfyUI 渲染超时")

        local_images = []
        local_videos = []
        local_audios = []
        local_texts = []
        local_files = []
        local_items = []
        local_urls = []
        current_timestamp = time.time()
        if 'outputs' in history_data:
            for node_id in history_data['outputs']:
                node_output = history_data['outputs'][node_id]
                for output_key, item in collect_comfy_file_items(node_output):
                    prefix = f"{req.type}_{int(current_timestamp)}_"
                    kind = comfy_output_kind(item)
                    local_path = download_comfy_output(target_backend, item, prefix=prefix)
                    if kind == "image" and req.convert_to_jpg:
                        local_path = convert_output_to_jpg(local_path)
                    name = os.path.basename(str(item.get("filename") or "")) or os.path.basename(str(local_path).split("?", 1)[0])
                    entry = {
                        "url": local_path,
                        "kind": kind,
                        "name": name,
                        "node_id": str(node_id),
                        "output_key": str(output_key),
                    }
                    if kind == "image":
                        local_images.append(local_path)
                    elif kind == "video":
                        local_videos.append(local_path)
                    elif kind == "audio":
                        local_audios.append(local_path)
                    elif kind == "text":
                        local_texts.append(local_path)
                    else:
                        local_files.append(local_path)
                    local_items.append(entry)
                    local_urls.append(local_path)
                for text, name in comfy_text_values_from_output(node_output):
                    prefix = f"{req.type}_{int(current_timestamp)}_"
                    local_path = save_comfy_text_output(text, prefix=prefix, name=name)
                    entry = {
                        "url": local_path,
                        "kind": "text",
                        "name": os.path.basename(str(local_path).split("?", 1)[0]),
                        "node_id": str(node_id),
                        "output_key": "text",
                    }
                    local_texts.append(local_path)
                    local_items.append(entry)
                    local_urls.append(local_path)

        result = {
            "prompt": req.prompt if req.prompt else "Detail Enhance",
            "images": local_images,
            "videos": local_videos,
            "audios": local_audios,
            "texts": local_texts,
            "files": local_files,
            "items": local_items,
            "outputs": local_urls,
            "seed": seed,
            "timestamp": current_timestamp,
            "type": req.type,
            "workflow_json": req.workflow_json,
            "task_id": task_id,
            "prompt_id": prompt_id,
            "backend": target_backend,
            "params": req.params
        }
        save_to_history(result)
        if GLOBAL_LOOP:
            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(result), GLOBAL_LOOP)
        return result

    except Exception as e:
        return {"images": [], "error": str(e)}
    finally:
        if target_backend:
            with LOAD_LOCK:
                if BACKEND_LOCAL_LOAD.get(target_backend, 0) > 0:
                    BACKEND_LOCAL_LOAD[target_backend] -= 1
        if current_task:
            with QUEUE_LOCK:
                if current_task in QUEUE:
                    QUEUE.remove(current_task)

# --- ComfyUI 工作流管理 ---

BUILTIN_WORKFLOWS = {ZIMAGE_DEFAULT_WORKFLOW, "Z-Image.json", "Z-Image-Enhance.json", "2511.json", "klein-enhance.json", "Flux2-Klein.json", "upscale.json"}
CUSTOM_WORKFLOW_FOLDER = "custom"
LEGACY_CUSTOM_WORKFLOW_FOLDER = "自定义"
WORKFLOW_NAME_RE = re.compile(rf"^(?:(?:{CUSTOM_WORKFLOW_FOLDER}|{LEGACY_CUSTOM_WORKFLOW_FOLDER})/)?[a-zA-Z0-9_一-龥\.\-]+\.json$")

class WorkflowField(BaseModel):
    id: str
    node: str = ""
    input: str = ""
    name: str = ""
    type: str = "text"
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: List[str] = []
    random_enabled: bool = False

class WorkflowConfig(BaseModel):
    title: str = ""
    fields: List[WorkflowField] = []
    mini_cards: Dict[str, Any] = {}

class WorkflowUploadRequest(BaseModel):
    name: str
    workflow: Dict[str, Any]

class WorkflowRunRequest(BaseModel):
    fields: Dict[str, Any] = {}
    config: WorkflowConfig
    client_id: str = ""

class WorkflowAppPayload(BaseModel):
    id: str = ""
    title: str = ""
    description: str = ""
    workflow_name: str = ""
    framework: str = "react"
    slug: str = ""
    api_base_url: str = ""
    category: str = "通用"
    tags: List[str] = []
    cover_image: str = ""
    preview_images: List[str] = []
    author: str = ""

class WorkflowAppRunRequest(BaseModel):
    fields: Dict[str, Any] = {}
    client_id: str = ""

def workflow_path_from_name(name: str) -> str:
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    path = os.path.abspath(os.path.join(WORKFLOW_DIR, *name.split("/")))
    workflow_root = os.path.abspath(WORKFLOW_DIR)
    if os.path.commonpath([workflow_root, path]) != workflow_root:
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    return path

def workflow_config_path(name: str) -> str:
    return workflow_path_from_name(name).replace(".json", ".config.json")

def is_builtin_workflow(name: str) -> bool:
    return "/" not in name and os.path.basename(name) in BUILTIN_WORKFLOWS

COMFYUI_LORA_CACHE = {"items": [], "expires_at": 0.0}

def comfyui_http_base(addr: str) -> str:
    text = str(addr or "").strip().rstrip("/")
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = f"http://{text}"
    return text

def extract_comfyui_choice_list(raw) -> List[str]:
    if isinstance(raw, list):
        if raw and isinstance(raw[0], list):
            return [str(item).strip() for item in raw[0] if str(item).strip()]
        return [str(item).strip() for item in raw if isinstance(item, (str, int, float)) and str(item).strip()]
    if isinstance(raw, dict):
        for key in ("options", "choices", "values"):
            values = extract_comfyui_choice_list(raw.get(key))
            if values:
                return values
    return []

def fetch_comfyui_lora_names() -> List[str]:
    now = time.time()
    if COMFYUI_LORA_CACHE["items"] and now < COMFYUI_LORA_CACHE["expires_at"]:
        return list(COMFYUI_LORA_CACHE["items"])

    found: List[str] = []
    seen = set()
    for addr in COMFYUI_INSTANCES:
        base = comfyui_http_base(addr)
        if not base:
            continue
        try:
            for class_name in ("LoraLoader", "LoraLoaderModelOnly"):
                response = requests.get(f"{base}/object_info/{class_name}", timeout=1.5)
                if response.status_code != 200:
                    continue
                data = response.json()
                info = data.get(class_name) if isinstance(data, dict) else None
                if not isinstance(info, dict):
                    info = data if isinstance(data, dict) else {}
                required = ((info.get("input") or {}).get("required") or {}) if isinstance(info, dict) else {}
                for input_name in ("lora_name", "lora"):
                    for item in extract_comfyui_choice_list(required.get(input_name)):
                        key = item.lower()
                        if key in seen:
                            continue
                        seen.add(key)
                        found.append(item)
            if found:
                break
        except Exception:
            continue

    COMFYUI_LORA_CACHE["items"] = found
    COMFYUI_LORA_CACHE["expires_at"] = now + 15
    return list(found)

def is_lora_workflow_field(field: Dict[str, Any]) -> bool:
    input_name = str(field.get("input") or field.get("fieldName") or "").strip().lower()
    label = str(field.get("name") or field.get("label") or "").strip().lower()
    default = str(field.get("default") or field.get("fieldValue") or "").strip().lower()
    if input_name in {"lora_name", "lora"}:
        return True
    return "lora" in f"{input_name} {label}" and default.endswith((".safetensors", ".pt", ".ckpt"))

def with_runtime_lora_options(cfg: Dict[str, Any]) -> Dict[str, Any]:
    fields = cfg.get("fields")
    if not isinstance(fields, list):
        return cfg
    lora_names = fetch_comfyui_lora_names()
    for field in fields:
        if not isinstance(field, dict) or not is_lora_workflow_field(field):
            continue
        merged = []
        for item in [field.get("default"), *(field.get("options") or []), *lora_names]:
            text = str(item or "").strip()
            if text and text.lower() not in {value.lower() for value in merged}:
                merged.append(text)
        if merged:
            field["type"] = "dropdown"
            field["options"] = merged
    return cfg

def safe_workflow_app_slug(value: str, fallback: str = "workflow-app") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\.json$", "", text)
    text = re.sub(r"[^a-z0-9_.-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text:
        text = fallback
    return text[:80] or fallback

def workflow_app_path(slug: str) -> str:
    safe_slug = safe_workflow_app_slug(slug)
    path = os.path.abspath(os.path.join(WORKFLOW_APP_DIR, safe_slug))
    root = os.path.abspath(WORKFLOW_APP_DIR)
    if os.path.commonpath([root, path]) != root:
        raise HTTPException(status_code=400, detail="Invalid app slug")
    return path

def load_workflow_app_store() -> List[Dict[str, Any]]:
    if not os.path.exists(WORKFLOW_APP_STORE_FILE):
        return []
    try:
        with open(WORKFLOW_APP_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception:
        pass
    return []

def save_workflow_app_store(items: List[Dict[str, Any]]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WORKFLOW_APP_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

def workflow_app_tags(value) -> List[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = re.split(r"[,，\s]+", value)
    else:
        raw = []
    tags = []
    seen = set()
    for item in raw:
        text = str(item or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        tags.append(text[:32])
    return tags[:12]

def workflow_app_preview_images(value) -> List[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = re.split(r"[\n,，]+", value)
    else:
        raw = []
    images: List[str] = []
    seen = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        images.append(text[:500])
    return images[:24]

def workflow_app_category(value) -> str:
    text = str(value or "").strip()
    return (text or "通用")[:32]

def workflow_app_public_record(record: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(record or {})
    workflow_name = str(item.get("workflow_name") or "").strip()
    available = bool(workflow_name and WORKFLOW_NAME_RE.match(workflow_name) and os.path.exists(workflow_path_from_name(workflow_name)))
    config = {}
    if available:
        config = load_workflow_config_dict(workflow_name)
    item["title"] = str(item.get("title") or config.get("title") or workflow_name.replace(".json", "") or "ComfyUI App").strip()
    item["description"] = str(item.get("description") or "").strip()
    item["category"] = workflow_app_category(item.get("category") or config.get("category"))
    item["tags"] = workflow_app_tags(item.get("tags") or config.get("tags") or [])
    item["cover_image"] = str(item.get("cover_image") or config.get("cover_image") or "").strip()
    preview_images = workflow_app_preview_images(item.get("preview_images") or config.get("preview_images") or [])
    if not preview_images and item["cover_image"]:
        preview_images = [item["cover_image"]]
    item["preview_images"] = preview_images
    item["author"] = str(item.get("author") or config.get("author") or "Hanako").strip()
    config_fields = config.get("fields") if available else None
    if isinstance(config_fields, list):
        item["field_count"] = len(config_fields)
    else:
        try:
            item["field_count"] = int(item.get("field_count") or 0)
        except Exception:
            item["field_count"] = 0
    item["available"] = available
    app_id = str(item.get("id") or item.get("slug") or "").strip()
    if app_id:
        item["launch_url"] = f"/workflow-apps/{urllib.parse.quote(app_id, safe='')}"
    return item

def find_workflow_app_record(app_id_or_slug: str) -> Optional[Dict[str, Any]]:
    target = urllib.parse.unquote(str(app_id_or_slug or "").strip())
    if not target:
        return None
    for item in load_workflow_app_store():
        if str(item.get("id") or "") == target or str(item.get("slug") or "") == target:
            return item
    return None

def workflow_app_runner_html(public: Dict[str, Any], config: Dict[str, Any]) -> str:
    title = html.escape(str(public.get("title") or "ComfyUI Workflow App"))
    payload = json.dumps({"app": public, "config": config or {"fields": []}}, ensure_ascii=False).replace("</", "<\\/")
    template = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__TITLE__</title>
  <style>
    :root { --bg:#f7f8fb; --panel:#fff; --line:#e5eaf1; --soft:#f1f4f8; --text:#0f172a; --muted:#64748b; --accent:#0f172a; --danger:#dc2626; --shadow:0 18px 55px rgba(15,23,42,.07); }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:var(--bg); color:var(--text); font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased; }
    button,input,textarea,select { font:inherit; }
    button { border:0; cursor:pointer; }
    .page { min-height:100vh; padding:42px 24px 72px; }
    .wrap { max-width:1180px; margin:0 auto; }
    .top { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:24px; }
    .eyebrow { font-size:11px; font-weight:950; letter-spacing:.22em; color:var(--muted); text-transform:uppercase; }
    h1 { margin:8px 0; font-size:44px; line-height:1; font-weight:950; letter-spacing:-.05em; color:#06234a; }
    .desc { max-width:760px; color:var(--muted); font-size:13px; line-height:1.7; font-weight:720; }
    .back { min-height:40px; padding:0 14px; border:1px solid var(--line); border-radius:12px; background:var(--panel); color:var(--muted); font-size:12px; font-weight:900; }
    .layout { display:grid; grid-template-columns:minmax(320px,420px) minmax(0,1fr); gap:18px; align-items:start; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:20px; box-shadow:var(--shadow); }
    .form { padding:18px; display:flex; flex-direction:column; gap:14px; }
    .field { display:flex; flex-direction:column; gap:7px; }
    .field span, .check span { color:var(--muted); font-size:11px; font-weight:950; letter-spacing:.06em; text-transform:uppercase; }
    .field input, .field textarea, .field select { width:100%; border:1px solid var(--line); border-radius:13px; background:var(--soft); color:var(--text); outline:none; padding:12px 13px; font-size:13px; font-weight:760; }
    .field textarea { min-height:120px; resize:vertical; line-height:1.55; }
    .field input:focus, .field textarea:focus, .field select:focus { border-color:var(--text); background:var(--panel); }
    .check { display:flex; align-items:center; gap:10px; }
    .check input { width:18px; height:18px; }
    .run { min-height:50px; border-radius:14px; background:var(--accent); color:white; font-size:13px; font-weight:950; display:flex; align-items:center; justify-content:center; gap:8px; }
    .run:disabled { opacity:.55; cursor:not-allowed; }
    .status { min-height:22px; color:var(--muted); font-size:12px; line-height:1.5; font-weight:800; }
    .outputs { min-height:520px; padding:18px; display:flex; align-items:center; justify-content:center; }
    .empty { color:var(--muted); font-size:13px; font-weight:850; text-align:center; }
    .grid { width:100%; display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:14px; align-items:start; }
    .item { min-height:150px; border:1px solid var(--line); border-radius:16px; background:var(--soft); overflow:hidden; display:flex; align-items:center; justify-content:center; color:var(--muted); text-decoration:none; font-size:12px; font-weight:850; text-align:center; }
    .item img, .item video { width:100%; height:100%; object-fit:contain; display:block; }
    .file { padding:16px; word-break:break-all; }
    @media (max-width: 920px) { .layout { grid-template-columns:1fr; } h1 { font-size:36px; } .top { flex-direction:column; } }
  </style>
</head>
<body>
  <main class="page">
    <div class="wrap">
      <header class="top">
        <div>
          <div class="eyebrow">ComfyUI Workflow App</div>
          <h1 id="title"></h1>
          <div id="desc" class="desc"></div>
        </div>
        <button class="back" type="button" onclick="history.length > 1 ? history.back() : location.href='/static/workflow-apps.html'">返回应用仓库</button>
      </header>
      <section class="layout">
        <form id="form" class="panel form"></form>
        <section id="outputs" class="panel outputs"><div class="empty">运行后会在这里显示输出结果。</div></section>
      </section>
    </div>
  </main>
  <script>
    window.__WORKFLOW_APP__ = __DATA__;
    const state = window.__WORKFLOW_APP__;
    const app = state.app || {};
    const config = state.config || { fields: [] };
    const mediaTypes = new Set(['image', 'video', 'audio']);
    const values = {};
    const apiBase = String(app.api_base_url || window.location.origin).replace(/\/$/, '');
    const $ = id => document.getElementById(id);

    function esc(value) {
      return String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function endpoint(path) {
      if(/^https?:\/\//i.test(path || '')) return path;
      return apiBase + path;
    }
    function assetUrl(url) {
      const text = String(url || '').trim();
      if(!text) return '';
      if(/^https?:\/\//i.test(text) || text.startsWith('data:')) return text;
      return endpoint(text.startsWith('/') ? text : '/api/view?filename=' + encodeURIComponent(text));
    }
    function defaultValue(field) {
      if(field.type === 'boolean') return Boolean(field.default);
      if(field.default !== undefined && field.default !== null) return field.default;
      if(field.type === 'number' || field.type === 'slider') return field.min ?? 0;
      if(field.type === 'dropdown') return (field.options || [])[0] || '';
      return '';
    }
    function labelFor(field) {
      return field.name || field.input || field.id || '参数';
    }
    function status(text) {
      const node = $('status');
      if(node) node.textContent = text || '';
    }
    function inferKind(url, fallback) {
      const text = String(url || '').split('?')[0].toLowerCase();
      if(fallback) return fallback;
      if(/\.(mp4|webm|mov|avi|mkv)$/.test(text)) return 'video';
      if(/\.(mp3|wav|ogg|m4a|flac)$/.test(text)) return 'audio';
      if(/\.(txt|json|csv|log)$/.test(text)) return 'text';
      return 'image';
    }
    function normalizeOutputs(data) {
      const out = [];
      const seen = new Set();
      function add(url, kind, name) {
        if(!url) return;
        const key = String(url || '').trim();
        if(!key) return;
        if(seen.has(key)) return;
        seen.add(key);
        out.push({ url, kind: inferKind(url, kind), name: name || String(url).split('/').pop() });
      }
      (data.items || []).forEach(item => add(item.url, item.kind, item.name));
      (data.images || []).forEach(url => add(url, 'image'));
      (data.videos || []).forEach(url => add(url, 'video'));
      (data.audios || []).forEach(url => add(url, 'audio'));
      (data.texts || []).forEach(url => add(url, 'text'));
      (data.files || []).forEach(url => add(url, 'file'));
      (data.outputs || []).forEach(url => add(url, ''));
      return out;
    }
    function renderOutputs(data) {
      const outputs = normalizeOutputs(data);
      if(!outputs.length) {
        $('outputs').innerHTML = '<div class="empty">工作流已完成，但没有返回可显示的输出。</div>';
        return;
      }
      $('outputs').innerHTML = '<div class="grid">' + outputs.map(item => {
        const url = assetUrl(item.url);
        if(item.kind === 'video') return '<a class="item" href="' + esc(url) + '" target="_blank"><video src="' + esc(url) + '" controls></video></a>';
        if(item.kind === 'audio') return '<a class="item file" href="' + esc(url) + '" target="_blank">音频：' + esc(item.name) + '</a>';
        if(item.kind === 'image') return '<a class="item" href="' + esc(url) + '" target="_blank"><img src="' + esc(url) + '" alt="' + esc(item.name || 'output') + '"></a>';
        return '<a class="item file" href="' + esc(url) + '" target="_blank">' + esc(item.name || item.url) + '</a>';
      }).join('') + '</div>';
    }
    async function uploadFile(field, input) {
      const file = input.files && input.files[0];
      if(!file) return;
      status('上传中...');
      const form = new FormData();
      form.append('files', file);
      const res = await fetch(endpoint('/api/upload'), { method:'POST', body:form });
      const data = await res.json();
      if(!res.ok) throw new Error(data.detail || '上传失败');
      const item = data.files && data.files[0] || {};
      values[field.id] = item.comfy_name || item.filename || file.name;
      input.nextElementSibling.textContent = '已上传：' + values[field.id];
      status('');
    }
    function fieldHtml(field, index) {
      const value = values[field.id];
      const label = esc(labelFor(field));
      const common = 'data-index="' + index + '"';
      if(mediaTypes.has(field.type)) {
        const accept = field.type === 'image' ? 'image/*' : field.type === 'video' ? 'video/*' : 'audio/*';
        return '<div class="field"><span>' + label + '</span><input ' + common + ' type="file" accept="' + accept + '"><div class="status">请选择文件</div></div>';
      }
      if(field.type === 'textarea') return '<label class="field"><span>' + label + '</span><textarea ' + common + '>' + esc(value) + '</textarea></label>';
      if(field.type === 'slider') return '<label class="field"><span>' + label + '：<b id="range' + index + '">' + esc(value) + '</b></span><input ' + common + ' type="range" min="' + esc(field.min ?? 0) + '" max="' + esc(field.max ?? 100) + '" step="' + esc(field.step ?? 1) + '" value="' + esc(value) + '"></label>';
      if(field.type === 'number') return '<label class="field"><span>' + label + '</span><input ' + common + ' type="number" step="' + esc(field.step ?? 1) + '" value="' + esc(value) + '"></label>';
      if(field.type === 'dropdown') return '<label class="field"><span>' + label + '</span><select ' + common + '>' + (field.options || []).map(option => '<option value="' + esc(option) + '"' + (String(option) === String(value) ? ' selected' : '') + '>' + esc(option) + '</option>').join('') + '</select></label>';
      if(field.type === 'boolean') return '<label class="check"><input ' + common + ' type="checkbox"' + (value ? ' checked' : '') + '><span>' + label + '</span></label>';
      return '<label class="field"><span>' + label + '</span><input ' + common + ' value="' + esc(value) + '"></label>';
    }
    function collectValues() {
      const fields = config.fields || [];
      fields.forEach((field, index) => {
        const input = document.querySelector('[data-index="' + index + '"]');
        if(!input || mediaTypes.has(field.type)) return;
        if(field.type === 'boolean') values[field.id] = input.checked;
        else if(field.type === 'number' || field.type === 'slider') values[field.id] = input.value === '' ? '' : Number(input.value);
        else values[field.id] = input.value;
      });
      return values;
    }
    async function runApp() {
      const btn = $('runBtn');
      btn.disabled = true;
      status('运行中，请等待 ComfyUI 返回结果...');
      $('outputs').innerHTML = '<div class="empty">处理中...</div>';
      try {
        const res = await fetch(endpoint('/api/workflow-apps/' + encodeURIComponent(app.id) + '/run'), {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ fields: collectValues(), client_id:'hosted-workflow-app-' + Date.now() })
        });
        const data = await res.json();
        if(!res.ok) throw new Error(data.detail || '运行失败');
        if(data.error) throw new Error(data.error);
        renderOutputs(data);
        status('运行完成');
      } catch(error) {
        status(error.message || '运行失败');
        $('outputs').innerHTML = '<div class="empty">' + esc(error.message || '运行失败') + '</div>';
      } finally {
        btn.disabled = false;
      }
    }
    function init() {
      document.title = app.title || 'ComfyUI Workflow App';
      $('title').textContent = app.title || 'ComfyUI Workflow App';
      $('desc').textContent = app.description || app.workflow_name || '';
      const fields = config.fields || [];
      fields.forEach(field => values[field.id] = defaultValue(field));
      $('form').innerHTML = (fields.length ? fields.map(fieldHtml).join('') : '<div class="empty">这个工作流没有暴露参数，运行时将使用默认值。</div>') + '<button id="runBtn" class="run" type="button">运行应用</button><div id="status" class="status"></div>';
      fields.forEach((field, index) => {
        const input = document.querySelector('[data-index="' + index + '"]');
        if(!input) return;
        if(mediaTypes.has(field.type)) input.addEventListener('change', () => uploadFile(field, input).catch(error => status(error.message || '上传失败')));
        if(field.type === 'slider') input.addEventListener('input', () => { const node = $('range' + index); if(node) node.textContent = input.value; });
      });
      $('runBtn').addEventListener('click', runApp);
    }
    init();
  </script>
</body>
</html>"""
    return template.replace("__TITLE__", title).replace("__DATA__", payload)

def load_workflow_config_dict(name: str) -> Dict[str, Any]:
    cfg = {"title": name.replace(".json", ""), "fields": [], "mini_cards": {}}
    cfg_path = workflow_config_path(name)
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = json.load(f) or {}
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    cfg.setdefault("fields", [])
    cfg.setdefault("mini_cards", {})
    return with_runtime_lora_options(cfg)

def app_bundle_for_record(record: Dict[str, Any]) -> Dict[str, Any]:
    workflow_name = str(record.get("workflow_name") or "").strip()
    if not workflow_name or not WORKFLOW_NAME_RE.match(workflow_name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    workflow_path = workflow_path_from_name(workflow_name)
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow not found")
    config = load_workflow_config_dict(workflow_name)
    cover_image = str(record.get("cover_image") or config.get("cover_image") or "").strip()
    preview_images = workflow_app_preview_images(record.get("preview_images") or config.get("preview_images") or [])
    if not preview_images and cover_image:
        preview_images = [cover_image]
    return {
        "id": record.get("id"),
        "title": record.get("title") or config.get("title") or workflow_name.replace(".json", ""),
        "description": record.get("description") or "",
        "framework": record.get("framework") or "react",
        "category": workflow_app_category(record.get("category") or config.get("category")),
        "tags": workflow_app_tags(record.get("tags") or config.get("tags") or []),
        "coverImage": cover_image,
        "previewImages": preview_images,
        "author": str(record.get("author") or config.get("author") or "Hanako").strip(),
        "workflowName": workflow_name,
        "apiBaseUrl": record.get("api_base_url") or "http://127.0.0.1:13000",
        "config": config,
    }

def write_workflow_app_file(root: str, rel: str, content: str):
    target = os.path.abspath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([os.path.abspath(root), target]) != os.path.abspath(root):
        raise HTTPException(status_code=400, detail="Invalid generated file path")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

def workflow_app_readme(bundle: Dict[str, Any]) -> str:
    title = bundle["title"]
    framework = bundle["framework"]
    workflow_name = bundle["workflowName"]
    api_base = bundle["apiBaseUrl"]
    if framework in {"react", "vue"}:
        run = "npm install\nnpm run dev"
    elif framework == "gradio":
        run = "pip install -r requirements.txt\npython app.py"
    else:
        run = "pip install -r requirements.txt\nstreamlit run app.py"
    return f"""# {title}

Generated from ComfyUI workflow `{workflow_name}`.

This app calls the Hanako / Infinite Canvas backend, which forwards workflow runs to your configured ComfyUI API service.

## API

Default backend:

```text
{api_base}
```

Run endpoint:

```text
POST /api/workflows/{urllib.parse.quote(workflow_name, safe='')}/run
```

## Start

```bash
{run}
```

Set `VITE_HANAKO_API_BASE` for React/Vue or `HANAKO_API_BASE` for Gradio/Streamlit if the backend runs on another host.
"""

def workflow_app_common_config(bundle: Dict[str, Any]) -> str:
    return json.dumps(bundle, ensure_ascii=False, indent=2)

def react_app_files(bundle: Dict[str, Any]) -> Dict[str, str]:
    cfg = workflow_app_common_config(bundle)
    return {
        "package.json": """{
  "scripts": {
    "dev": "vite --host 127.0.0.1",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "@vitejs/plugin-react": "latest",
    "vite": "latest",
    "react": "latest",
    "react-dom": "latest"
  },
  "devDependencies": {}
}
""",
        "index.html": """<!doctype html>
<html>
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>ComfyUI Workflow App</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/App.jsx"></script>
  </body>
</html>
""",
        "src/workflowConfig.json": cfg + "\n",
        "src/App.jsx": """import React, { useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import appConfig from './workflowConfig.json';
import './styles.css';

const mediaTypes = new Set(['image', 'video', 'audio']);
const apiBase = (import.meta.env.VITE_HANAKO_API_BASE || appConfig.apiBaseUrl || window.location.origin).replace(/\\/$/, '');

function defaultValue(field) {
  if (field.type === 'boolean') return Boolean(field.default);
  if (field.default !== undefined && field.default !== null) return field.default;
  if (field.type === 'slider' || field.type === 'number') return field.min ?? 0;
  return '';
}

function endpoint(path) {
  return `${apiBase}${path}`;
}

async function uploadMedia(file) {
  const form = new FormData();
  form.append('files', file);
  const response = await fetch(endpoint('/api/upload'), { method: 'POST', body: form });
  if (!response.ok) throw new Error('Upload failed');
  const data = await response.json();
  return data.files?.[0]?.comfy_name || data.files?.[0]?.filename || file.name;
}

function Field({ field, value, onChange }) {
  const label = field.name || field.input || field.id;
  if (mediaTypes.has(field.type)) {
    return (
      <label className="field">
        <span>{label}</span>
        <input type="file" accept={`${field.type}/*`} onChange={async event => {
          const file = event.target.files?.[0];
          if (!file) return;
          onChange(await uploadMedia(file));
        }} />
      </label>
    );
  }
  if (field.type === 'textarea') {
    return <label className="field"><span>{label}</span><textarea value={value} onChange={e => onChange(e.target.value)} /></label>;
  }
  if (field.type === 'slider') {
    return <label className="field"><span>{label}: {value}</span><input type="range" min={field.min ?? 0} max={field.max ?? 100} step={field.step ?? 1} value={value} onChange={e => onChange(Number(e.target.value))} /></label>;
  }
  if (field.type === 'number') {
    return <label className="field"><span>{label}</span><input type="number" value={value} step={field.step ?? 1} onChange={e => onChange(e.target.value)} /></label>;
  }
  if (field.type === 'dropdown') {
    return <label className="field"><span>{label}</span><select value={value} onChange={e => onChange(e.target.value)}>{(field.options || []).map(option => <option key={option} value={option}>{option}</option>)}</select></label>;
  }
  if (field.type === 'boolean') {
    return <label className="check"><input type="checkbox" checked={Boolean(value)} onChange={e => onChange(e.target.checked)} /><span>{label}</span></label>;
  }
  return <label className="field"><span>{label}</span><input value={value} onChange={e => onChange(e.target.value)} /></label>;
}

function App() {
  const fields = appConfig.config.fields || [];
  const initialFields = useMemo(() => Object.fromEntries(fields.map(field => [field.id, defaultValue(field)])), []);
  const [values, setValues] = useState(initialFields);
  const [status, setStatus] = useState('');
  const [images, setImages] = useState([]);

  async function runWorkflow() {
    setStatus('Running...');
    setImages([]);
    const response = await fetch(endpoint(`/api/workflows/${encodeURIComponent(appConfig.workflowName)}/run`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields: values, config: appConfig.config, client_id: `workflow-app-${Date.now()}` })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Workflow failed');
    setImages(data.images || []);
    setStatus('Done');
  }

  return (
    <main className="app">
      <header>
        <p>ComfyUI Workflow App</p>
        <h1>{appConfig.title}</h1>
        <span>{appConfig.workflowName}</span>
      </header>
      <section className="panel">
        {fields.map(field => <Field key={field.id} field={field} value={values[field.id]} onChange={value => setValues(v => ({ ...v, [field.id]: value }))} />)}
        <button onClick={() => runWorkflow().catch(error => setStatus(error.message))}>Run workflow</button>
        <div className="status">{status}</div>
      </section>
      <section className="gallery">
        {images.map(url => <img key={url} src={url.startsWith('http') ? url : endpoint(url)} alt="result" />)}
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
""",
        "src/styles.css": """body { margin:0; background:#f6f7f9; color:#0f172a; font-family:Inter,system-ui,sans-serif; }
.app { max-width:1120px; margin:0 auto; padding:48px 24px; }
header { margin-bottom:24px; }
header p { color:#64748b; font-size:12px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
h1 { margin:0 0 8px; font-size:42px; letter-spacing:-.04em; }
header span { color:#64748b; font-size:13px; font-weight:700; }
.panel { display:grid; gap:14px; padding:20px; background:white; border:1px solid #e5e7eb; border-radius:18px; box-shadow:0 16px 50px rgba(15,23,42,.06); }
.field { display:grid; gap:7px; font-size:13px; font-weight:800; color:#475569; }
.field input, .field textarea, .field select { width:100%; border:1px solid #dbe1ea; border-radius:12px; padding:11px 12px; font:inherit; background:#f8fafc; }
.field textarea { min-height:110px; resize:vertical; }
.check { display:flex; align-items:center; gap:10px; font-weight:800; color:#475569; }
button { height:46px; border:0; border-radius:13px; background:#0f172a; color:white; font-weight:900; cursor:pointer; }
.status { color:#64748b; font-size:13px; font-weight:700; min-height:20px; }
.gallery { margin-top:24px; display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:16px; }
.gallery img { width:100%; border-radius:16px; background:white; border:1px solid #e5e7eb; }
"""
    }

def vue_app_files(bundle: Dict[str, Any]) -> Dict[str, str]:
    cfg = workflow_app_common_config(bundle)
    return {
        "package.json": """{
  "scripts": {
    "dev": "vite --host 127.0.0.1",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "@vitejs/plugin-vue": "latest",
    "vite": "latest",
    "vue": "latest"
  },
  "devDependencies": {}
}
""",
        "index.html": """<!doctype html>
<html>
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>ComfyUI Workflow App</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>
""",
        "src/workflowConfig.json": cfg + "\n",
        "src/main.js": """import { createApp } from 'vue';
import App from './App.vue';
import './style.css';

createApp(App).mount('#app');
""",
        "src/App.vue": """<script setup>
import { computed, ref } from 'vue';
import appConfig from './workflowConfig.json';

const mediaTypes = new Set(['image', 'video', 'audio']);
const apiBase = (import.meta.env.VITE_HANAKO_API_BASE || appConfig.apiBaseUrl || window.location.origin).replace(/\\/$/, '');
const fields = appConfig.config.fields || [];
const values = ref(Object.fromEntries(fields.map(field => [field.id, defaultValue(field)])));
const images = ref([]);
const status = ref('');

function defaultValue(field) {
  if (field.type === 'boolean') return Boolean(field.default);
  if (field.default !== undefined && field.default !== null) return field.default;
  if (field.type === 'slider' || field.type === 'number') return field.min ?? 0;
  return '';
}
function endpoint(path) { return `${apiBase}${path}`; }
function imageSrc(url) { return String(url).startsWith('http') ? url : endpoint(url); }
async function uploadMedia(event, field) {
  const file = event.target.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append('files', file);
  const response = await fetch(endpoint('/api/upload'), { method: 'POST', body: form });
  if (!response.ok) throw new Error('Upload failed');
  const data = await response.json();
  values.value[field.id] = data.files?.[0]?.comfy_name || data.files?.[0]?.filename || file.name;
}
async function runWorkflow() {
  status.value = 'Running...';
  images.value = [];
  try {
    const response = await fetch(endpoint(`/api/workflows/${encodeURIComponent(appConfig.workflowName)}/run`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields: values.value, config: appConfig.config, client_id: `workflow-app-${Date.now()}` })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Workflow failed');
    images.value = data.images || [];
    status.value = 'Done';
  } catch (error) {
    status.value = error.message;
  }
}
</script>

<template>
  <main class="app">
    <header>
      <p>ComfyUI Workflow App</p>
      <h1>{{ appConfig.title }}</h1>
      <span>{{ appConfig.workflowName }}</span>
    </header>
    <section class="panel">
      <label v-for="field in fields" :key="field.id" class="field">
        <span>{{ field.name || field.input || field.id }}</span>
        <input v-if="mediaTypes.has(field.type)" type="file" :accept="`${field.type}/*`" @change="event => uploadMedia(event, field)" />
        <textarea v-else-if="field.type === 'textarea'" v-model="values[field.id]" />
        <input v-else-if="field.type === 'slider'" type="range" :min="field.min ?? 0" :max="field.max ?? 100" :step="field.step ?? 1" v-model="values[field.id]" />
        <input v-else-if="field.type === 'number'" type="number" :step="field.step ?? 1" v-model="values[field.id]" />
        <select v-else-if="field.type === 'dropdown'" v-model="values[field.id]">
          <option v-for="option in field.options || []" :key="option" :value="option">{{ option }}</option>
        </select>
        <input v-else-if="field.type === 'boolean'" type="checkbox" v-model="values[field.id]" />
        <input v-else v-model="values[field.id]" />
      </label>
      <button @click="runWorkflow">Run workflow</button>
      <div class="status">{{ status }}</div>
    </section>
    <section class="gallery">
      <img v-for="url in images" :key="url" :src="imageSrc(url)" alt="result" />
    </section>
  </main>
</template>
""",
        "src/style.css": react_app_files(bundle)["src/styles.css"],
    }

def gradio_app_files(bundle: Dict[str, Any]) -> Dict[str, str]:
    cfg = workflow_app_common_config(bundle)
    return {
        "workflow_config.json": cfg + "\n",
        "requirements.txt": "gradio\nrequests\n",
        "app.py": """import json
import os
from pathlib import Path

import gradio as gr
import requests

CONFIG = json.loads(Path("workflow_config.json").read_text(encoding="utf-8"))
API_BASE = os.getenv("HANAKO_API_BASE", CONFIG.get("apiBaseUrl", "http://127.0.0.1:13000")).rstrip("/")
FIELDS = CONFIG.get("config", {}).get("fields", [])

def endpoint(path):
    return f"{API_BASE}{path}"

def upload_media(value):
    if not value:
        return ""
    path = value if isinstance(value, str) else getattr(value, "name", "")
    if not path:
        return ""
    with open(path, "rb") as f:
        response = requests.post(endpoint("/api/upload"), files={"files": (os.path.basename(path), f)})
    response.raise_for_status()
    data = response.json()
    return data.get("files", [{}])[0].get("comfy_name") or data.get("files", [{}])[0].get("filename") or os.path.basename(path)

def normalize_value(field, value):
    if field.get("type") in {"image", "video", "audio"}:
        return upload_media(value)
    return value

def run_workflow(*values):
    fields = {field["id"]: normalize_value(field, value) for field, value in zip(FIELDS, values)}
    response = requests.post(
        endpoint(f"/api/workflows/{CONFIG['workflowName']}/run"),
        json={"fields": fields, "config": CONFIG["config"], "client_id": "workflow-app-gradio"},
        timeout=1800,
    )
    response.raise_for_status()
    data = response.json()
    images = [endpoint(url) if isinstance(url, str) and url.startswith("/") else url for url in data.get("images", [])]
    return images, "Done"

def component_for(field):
    label = field.get("name") or field.get("input") or field.get("id")
    value = field.get("default")
    kind = field.get("type")
    if kind in {"image", "video", "audio"}:
        return gr.File(label=label)
    if kind == "textarea":
        return gr.Textbox(label=label, value=value or "", lines=5)
    if kind == "slider":
        return gr.Slider(label=label, minimum=field.get("min") or 0, maximum=field.get("max") or 100, step=field.get("step") or 1, value=value or field.get("min") or 0)
    if kind == "number":
        return gr.Number(label=label, value=value or 0)
    if kind == "dropdown":
        options = field.get("options") or []
        return gr.Dropdown(label=label, choices=options, value=value if value in options else (options[0] if options else None))
    if kind == "boolean":
        return gr.Checkbox(label=label, value=bool(value))
    return gr.Textbox(label=label, value="" if value is None else str(value))

with gr.Blocks(title=CONFIG.get("title", "ComfyUI Workflow App")) as demo:
    gr.Markdown(f"# {CONFIG.get('title', 'ComfyUI Workflow App')}\\n`{CONFIG.get('workflowName')}`")
    inputs = [component_for(field) for field in FIELDS]
    run = gr.Button("Run workflow", variant="primary")
    gallery = gr.Gallery(label="Outputs")
    status = gr.Textbox(label="Status", interactive=False)
    run.click(run_workflow, inputs=inputs, outputs=[gallery, status])

if __name__ == "__main__":
    demo.launch()
"""
    }

def streamlit_app_files(bundle: Dict[str, Any]) -> Dict[str, str]:
    cfg = workflow_app_common_config(bundle)
    return {
        "workflow_config.json": cfg + "\n",
        "requirements.txt": "streamlit\nrequests\n",
        "app.py": """import json
import os
from pathlib import Path

import requests
import streamlit as st

CONFIG = json.loads(Path("workflow_config.json").read_text(encoding="utf-8"))
API_BASE = os.getenv("HANAKO_API_BASE", CONFIG.get("apiBaseUrl", "http://127.0.0.1:13000")).rstrip("/")
FIELDS = CONFIG.get("config", {}).get("fields", [])

def endpoint(path):
    return f"{API_BASE}{path}"

def upload_media(uploaded):
    if not uploaded:
        return ""
    response = requests.post(endpoint("/api/upload"), files={"files": (uploaded.name, uploaded.getvalue())})
    response.raise_for_status()
    data = response.json()
    return data.get("files", [{}])[0].get("comfy_name") or data.get("files", [{}])[0].get("filename") or uploaded.name

def field_input(field):
    label = field.get("name") or field.get("input") or field.get("id")
    value = field.get("default")
    kind = field.get("type")
    if kind in {"image", "video", "audio"}:
        uploaded = st.file_uploader(label, key=field["id"])
        return upload_media(uploaded) if uploaded else ""
    if kind == "textarea":
        return st.text_area(label, "" if value is None else str(value), key=field["id"])
    if kind == "slider":
        return st.slider(label, float(field.get("min") or 0), float(field.get("max") or 100), float(value if value is not None else field.get("min") or 0), float(field.get("step") or 1), key=field["id"])
    if kind == "number":
        return st.number_input(label, value=float(value or 0), step=float(field.get("step") or 1), key=field["id"])
    if kind == "dropdown":
        options = field.get("options") or []
        index = options.index(value) if value in options else 0
        return st.selectbox(label, options, index=index, key=field["id"]) if options else ""
    if kind == "boolean":
        return st.checkbox(label, value=bool(value), key=field["id"])
    return st.text_input(label, "" if value is None else str(value), key=field["id"])

st.set_page_config(page_title=CONFIG.get("title", "ComfyUI Workflow App"), layout="wide")
st.title(CONFIG.get("title", "ComfyUI Workflow App"))
st.caption(CONFIG.get("workflowName", ""))

values = {field["id"]: field_input(field) for field in FIELDS}

if st.button("Run workflow", type="primary"):
    with st.spinner("Running workflow..."):
        response = requests.post(
            endpoint(f"/api/workflows/{CONFIG['workflowName']}/run"),
            json={"fields": values, "config": CONFIG["config"], "client_id": "workflow-app-streamlit"},
            timeout=1800,
        )
        response.raise_for_status()
        data = response.json()
    for url in data.get("images", []):
        st.image(endpoint(url) if isinstance(url, str) and url.startswith("/") else url)
"""
    }

def workflow_app_files(bundle: Dict[str, Any]) -> Dict[str, str]:
    framework = bundle.get("framework") or "react"
    if framework == "vue":
        files = vue_app_files(bundle)
    elif framework == "gradio":
        files = gradio_app_files(bundle)
    elif framework == "streamlit":
        files = streamlit_app_files(bundle)
    else:
        files = react_app_files(bundle)
    files["README.md"] = workflow_app_readme(bundle)
    files["app.manifest.json"] = workflow_app_common_config(bundle) + "\n"
    return files

def generate_workflow_app_files(record: Dict[str, Any]) -> Dict[str, Any]:
    bundle = app_bundle_for_record(record)
    root = workflow_app_path(record.get("slug") or record.get("id") or bundle["title"])
    os.makedirs(root, exist_ok=True)
    for rel, content in workflow_app_files(bundle).items():
        write_workflow_app_file(root, rel, content)
    record["app_dir"] = root
    record["field_count"] = len((bundle.get("config") or {}).get("fields") or [])
    record["updated_at"] = now_ms()
    return record

def runninghub_workflow_store_path() -> str:
    return RUNNINGHUB_WORKFLOW_STORE_FILE

def load_runninghub_workflow_store():
    if not os.path.exists(RUNNINGHUB_WORKFLOW_STORE_FILE):
        return {}
    try:
        with open(RUNNINGHUB_WORKFLOW_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_runninghub_workflow_store(store):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RUNNINGHUB_WORKFLOW_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

def runninghub_workflow_config_has_payload(cfg):
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("fields") or cfg.get("workflowJson") or cfg.get("raw"))

def runninghub_workflow_entry_from_config(cfg, fallback=None):
    fallback = fallback if isinstance(fallback, dict) else {}
    key = runninghub_workflow_store_key((cfg or {}).get("workflowId") or fallback.get("workflowId") or fallback.get("id"))
    if not key:
        return None
    return normalize_runninghub_entry({
        "id": key,
        "workflowId": key,
        "title": (cfg or {}).get("title") or fallback.get("title") or fallback.get("name") or f"工作流 {key[-6:]}",
        "note": (cfg or {}).get("description") or fallback.get("note") or fallback.get("description") or "",
        "thumbnail": fallback.get("thumbnail") or "",
        "enabled": fallback.get("enabled", True),
        "fields": (cfg or {}).get("fields") or fallback.get("fields") or [],
        "workflowJson": (cfg or {}).get("workflowJson") if isinstance((cfg or {}).get("workflowJson"), dict) else fallback.get("workflowJson") or {},
        "optionalImageMode": (cfg or {}).get("optionalImageMode") or fallback.get("optionalImageMode") or "prune-workflow",
        "raw": (cfg or {}).get("raw") if isinstance((cfg or {}).get("raw"), dict) else fallback.get("raw") or {},
        "updatedAt": (cfg or {}).get("updatedAt") or fallback.get("updatedAt") or 0,
    }, "workflow")

def runninghub_provider_with_workflow_store(provider):
    if not isinstance(provider, dict) or provider.get("id") != "runninghub":
        return provider
    store = load_runninghub_workflow_store()
    if not store:
        return provider
    merged = dict(provider)
    workflows = [dict(item) for item in (merged.get("rh_workflows") or []) if isinstance(item, dict)]
    by_id = {
        runninghub_workflow_store_key(item.get("workflowId") or item.get("id")): item
        for item in workflows
        if runninghub_workflow_store_key(item.get("workflowId") or item.get("id"))
    }
    for workflow_id, cfg in store.items():
        if not isinstance(cfg, dict) or not runninghub_workflow_config_has_payload(cfg):
            continue
        existing = by_id.get(workflow_id)
        selected = runninghub_select_workflow_config(existing, cfg)
        entry = runninghub_workflow_entry_from_config(selected, existing)
        if not entry:
            continue
        if existing is None:
            workflows.append(entry)
        else:
            existing.update(entry)
    merged["rh_workflows"] = normalize_runninghub_entries(workflows, "workflow")
    return merged

def runninghub_provider_workflow_config(workflow_id: str, require_payload: bool = True):
    key = runninghub_workflow_store_key(workflow_id)
    if not key:
        return None
    providers = load_api_providers()
    provider = next((item for item in providers if item.get("id") == "runninghub"), None)
    if not provider:
        return None
    for entry in provider.get("rh_workflows") or []:
        entry_key = runninghub_workflow_store_key(entry.get("workflowId") or entry.get("id"))
        if entry_key != key:
            continue
        cfg = {
            "workflowId": key,
            "title": entry.get("title") or key,
            "description": entry.get("note") or entry.get("description") or "",
            "fields": [
                field for field in (runninghub_normalize_field(item) for item in (entry.get("fields") or []))
                if not runninghub_is_saved_link_field(field)
            ],
            "workflowJson": entry.get("workflowJson") if isinstance(entry.get("workflowJson"), dict) else {},
            "optionalImageMode": entry.get("optionalImageMode") or "prune-workflow",
            "raw": entry.get("raw") if isinstance(entry.get("raw"), dict) else {},
            "updatedAt": entry.get("updatedAt") or 0,
            "source": "api_providers",
        }
        has_payload = runninghub_workflow_config_has_payload(cfg)
        cfg["configured"] = has_payload
        return cfg if has_payload or not require_payload else None
    return None

def runninghub_select_workflow_config(local_cfg, provider_cfg):
    if isinstance(local_cfg, dict) and isinstance(provider_cfg, dict):
        try:
            local_updated = int(local_cfg.get("updatedAt") or 0)
        except Exception:
            local_updated = 0
        try:
            provider_updated = int(provider_cfg.get("updatedAt") or 0)
        except Exception:
            provider_updated = 0
        return provider_cfg if provider_updated > local_updated else local_cfg
    if isinstance(local_cfg, dict):
        return local_cfg
    if isinstance(provider_cfg, dict):
        return provider_cfg
    return None

def sync_runninghub_workflow_to_provider(cfg):
    if not isinstance(cfg, dict):
        return
    key = runninghub_workflow_store_key(cfg.get("workflowId"))
    if not key:
        return
    providers = load_api_providers()
    provider = next((item for item in providers if item.get("id") == "runninghub"), None)
    if not provider:
        provider = {
            "id": "runninghub",
            "name": "RunningHub",
            "base_url": RUNNINGHUB_DEFAULT_BASE_URL,
            "protocol": "runninghub",
            "image_generation_endpoint": "",
            "image_edit_endpoint": "",
            "enabled": True,
            "primary": False,
            "text_image_models": RUNNINGHUB_DEFAULT_IMAGE_MODELS,
            "image_to_image_models": [],
            "image_models": RUNNINGHUB_DEFAULT_IMAGE_MODELS,
            "chat_models": [],
            "video_models": [],
            "ms_loras": [],
            "ms_defaults_version": 0,
            "rh_apps": RUNNINGHUB_DEFAULT_APPS,
            "rh_workflows": [],
        }
        providers.append(provider)
    workflows = provider.setdefault("rh_workflows", [])
    entry = None
    for item in workflows:
        item_key = runninghub_workflow_store_key(item.get("workflowId") or item.get("id"))
        if item_key == key:
            entry = item
            break
    if entry is None:
        entry = {
            "id": key,
            "workflowId": key,
            "title": cfg.get("title") or f"工作流 {key[-6:]}",
            "note": cfg.get("description") or "",
            "thumbnail": "",
            "enabled": True,
        }
        workflows.append(entry)
    entry.update({
        "id": key,
        "workflowId": key,
        "title": cfg.get("title") or entry.get("title") or f"工作流 {key[-6:]}",
        "note": cfg.get("description") or "",
        "fields": [
            field for field in (runninghub_normalize_field(item) for item in (cfg.get("fields") or []))
            if not runninghub_is_saved_link_field(field)
        ],
        "workflowJson": cfg.get("workflowJson") if isinstance(cfg.get("workflowJson"), dict) else {},
        "optionalImageMode": cfg.get("optionalImageMode") or "prune-workflow",
        "raw": cfg.get("raw") if isinstance(cfg.get("raw"), dict) else {},
        "updatedAt": cfg.get("updatedAt") or now_ms(),
    })
    if "enabled" not in entry:
        entry["enabled"] = True
    if "thumbnail" not in entry:
        entry["thumbnail"] = ""
    save_api_providers([normalize_provider(item) for item in providers])

def remove_runninghub_workflow_from_provider(workflow_id: str):
    key = runninghub_workflow_store_key(workflow_id)
    if not key:
        return
    providers = load_api_providers()
    changed = False
    for provider in providers:
        if provider.get("id") != "runninghub":
            continue
        workflows = provider.get("rh_workflows") or []
        removed = next((
            item for item in workflows
            if runninghub_workflow_store_key(item.get("workflowId") or item.get("id")) == key
        ), None)
        kept = [
            item for item in workflows
            if runninghub_workflow_store_key(item.get("workflowId") or item.get("id")) != key
        ]
        static_provider = load_static_runninghub_provider()
        static_workflow = next((
            item for item in (static_provider or {}).get("rh_workflows", [])
            if runninghub_workflow_store_key(item.get("workflowId") or item.get("id")) == key
        ), None)
        if static_workflow:
            tombstone = normalize_runninghub_entry({**static_workflow, **(removed or {}), "enabled": False, "hidden": True}, "workflow")
            if tombstone:
                kept.append(tombstone)
        if static_workflow or len(kept) != len(workflows):
            provider["rh_workflows"] = kept
            changed = True
    if changed:
        save_api_providers([normalize_provider(item) for item in providers])

def runninghub_workflow_store_key(workflow_id: str) -> str:
    return str(workflow_id or "").strip()

def runninghub_normalize_field(raw, fallback=None):
    fallback = fallback or {}
    if hasattr(raw, "dict"):
        raw = raw.dict()
    if not isinstance(raw, dict):
        raw = {}
    options = raw.get("options", fallback.get("options", []))
    if isinstance(options, str):
        options = [item.strip() for item in re.split(r"[\r\n,]+", options) if item.strip()]
    elif isinstance(options, list):
        options = [str(item).strip() for item in options if str(item).strip()]
    else:
        options = []
    field_id = str(raw.get("id") or raw.get("fieldId") or raw.get("key") or raw.get("nodeId") or fallback.get("id") or "").strip()
    node_id = str(raw.get("nodeId") or fallback.get("nodeId") or raw.get("node_id") or "").strip()
    field_name = str(raw.get("fieldName") or raw.get("inputName") or raw.get("name") or fallback.get("fieldName") or "").strip()
    field_value = raw.get("fieldValue")
    if field_value is None:
        field_value = raw.get("defaultValue")
    if field_value is None:
        field_value = raw.get("value")
    if field_value is None:
        field_value = fallback.get("fieldValue", "")
    if isinstance(field_value, (dict, list)):
        field_value = json.dumps(field_value, ensure_ascii=False)
    elif field_value is None:
        field_value = ""
    else:
        field_value = str(field_value)
    return {
        "id": field_id or f"{node_id}::{field_name}",
        "nodeId": node_id,
        "fieldName": field_name,
        "fieldValue": field_value,
        "fieldType": str(raw.get("fieldType") or fallback.get("fieldType") or "TEXT"),
        "label": str(raw.get("label") or raw.get("title") or field_name or fallback.get("label") or ""),
        "enabled": bool(raw.get("enabled", fallback.get("enabled", True))),
        "sourceFromUpstream": bool(raw.get("sourceFromUpstream", fallback.get("sourceFromUpstream", True))),
        "group": str(raw.get("group") or fallback.get("group") or ""),
        "note": str(raw.get("note") or fallback.get("note") or ""),
        "options": options,
        "random_enabled": bool(raw.get("random_enabled", fallback.get("random_enabled", False))),
        "min": raw.get("min", fallback.get("min", "")),
        "max": raw.get("max", fallback.get("max", "")),
        "step": raw.get("step", fallback.get("step", "")),
        "imageOrder": int(raw.get("imageOrder") or raw.get("image_order") or fallback.get("imageOrder") or 0),
        "required": bool(raw.get("required", fallback.get("required", False))),
    }

def runninghub_is_saved_link_field(field):
    if not isinstance(field, dict):
        return False
    value = field.get("fieldValue")
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return False
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    return runninghub_is_workflow_link_value(parsed)

def runninghub_collect_workflow_fields(workflow_json):
    fields = []
    if not isinstance(workflow_json, dict):
        return fields
    for node_id, node_content in workflow_json.items():
        if not isinstance(node_content, dict):
            continue
        inputs = node_content.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for field_name, raw_value in inputs.items():
            if runninghub_is_workflow_link_value(raw_value):
                continue
            if isinstance(raw_value, (dict, list)):
                field_value = json.dumps(raw_value, ensure_ascii=False)
            elif raw_value is None:
                field_value = ""
            else:
                field_value = str(raw_value)
            field_type = runninghub_infer_workflow_field_type(field_name, field_value)
            fields.append({
                "id": f"{node_id}::{field_name}",
                "nodeId": str(node_id),
                "fieldName": str(field_name),
                "fieldValue": field_value,
                "fieldType": field_type,
                "label": str(field_name),
                "enabled": False,
                "sourceFromUpstream": True,
                "group": str(
                    (node_content.get("_meta") or {}).get("title")
                    or node_content.get("class_type")
                    or node_content.get("_class")
                    or node_content.get("type")
                    or ""
                ),
                "note": "",
                "imageOrder": 0,
                "required": field_type == "IMAGE",
            })
    return fields

class ComfyInstancesPayload(BaseModel):
    instances: List[str] = []

@app.get("/api/comfyui/instances")
def get_comfyui_instances():
    return {"instances": COMFYUI_INSTANCES}

@app.put("/api/comfyui/instances")
def save_comfyui_instances(payload: ComfyInstancesPayload):
    # 宽容校验：去前后空白、去 http(s):// 前缀、去尾部斜杠；要求形如 host:port
    cleaned = []
    for item in payload.instances:
        s = str(item or "").strip()
        if not s:
            continue
        s = re.sub(r"^https?://", "", s)
        s = s.rstrip("/")
        if ":" not in s:
            raise HTTPException(status_code=400, detail=f"地址缺少端口号：{item}（应为 host:port，例如 127.0.0.1:8188）")
        host, _, port = s.rpartition(":")
        if not host or not port.isdigit():
            raise HTTPException(status_code=400, detail=f"地址不合法：{item}（应为 host:port，例如 127.0.0.1:8188）")
        if s in cleaned:
            continue
        cleaned.append(s)
    if not cleaned:
        raise HTTPException(status_code=400, detail="至少保留一个 ComfyUI 后端地址")
    # 写入 env 文件
    try:
        update_env_values({"COMFYUI_INSTANCES": ",".join(cleaned)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入 env 失败：{e}")
    # 更新进程中的全局变量
    global COMFYUI_INSTANCES, COMFYUI_ADDRESS, BACKEND_LOCAL_LOAD
    COMFYUI_INSTANCES = cleaned
    COMFYUI_ADDRESS = cleaned[0]
    new_load = {addr: 0 for addr in cleaned}
    for addr, n in (BACKEND_LOCAL_LOAD or {}).items():
        if addr in new_load:
            new_load[addr] = n
    BACKEND_LOCAL_LOAD = new_load
    return {"instances": COMFYUI_INSTANCES}

@app.get("/api/workflows")
def list_workflows():
    if not os.path.isdir(WORKFLOW_DIR):
        return {"workflows": []}
    items = []
    for root, dirs, files in os.walk(WORKFLOW_DIR):
        if os.path.abspath(root) == os.path.abspath(WORKFLOW_DIR):
            dirs[:] = [d for d in dirs if d in {CUSTOM_WORKFLOW_FOLDER, LEGACY_CUSTOM_WORKFLOW_FOLDER}]
        for fn in sorted(files):
            if not fn.endswith(".json") or fn.endswith(".config.json"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), WORKFLOW_DIR).replace("\\", "/")
            if is_builtin_workflow(rel):
                continue
            cfg = {}
            cfg_path = workflow_config_path(rel)
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception:
                    cfg = {}
            items.append({
                "name": rel,
                "title": cfg.get("title") or fn.replace(".json", ""),
                "builtin": False,
                "field_count": len(cfg.get("fields") or []),
            })
    items.sort(key=lambda item: (0 if item["name"].startswith(f"{CUSTOM_WORKFLOW_FOLDER}/") else 1, item["title"]))
    return {"workflows": items}

@app.get("/api/workflows/{name:path}")
def get_workflow(name: str):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    workflow_path = workflow_path_from_name(name)
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow not found")
    with open(workflow_path, "r", encoding="utf-8") as f:
        workflow = json.load(f)
    cfg = load_workflow_config_dict(name)
    return {"name": name, "workflow": workflow, "config": cfg, "builtin": is_builtin_workflow(name)}

@app.post("/api/workflows")
def upload_workflow(payload: WorkflowUploadRequest):
    name = os.path.basename(payload.name.strip())
    if not name.endswith(".json"):
        name = name + ".json"
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="工作流名称不合法，请使用中文/英文/数字/_-.")
    if not isinstance(payload.workflow, dict) or not payload.workflow:
        raise HTTPException(status_code=400, detail="工作流 JSON 为空")
    # 简单校验：是 API 格式（节点 id 为 key，含 class_type）
    sample = next(iter(payload.workflow.values()), None)
    if not isinstance(sample, dict) or "class_type" not in sample:
        raise HTTPException(status_code=400, detail="不是有效的 ComfyUI API 工作流 JSON（需包含 class_type）")
    custom_dir = os.path.join(WORKFLOW_DIR, CUSTOM_WORKFLOW_FOLDER)
    os.makedirs(custom_dir, exist_ok=True)
    stored_name = f"{CUSTOM_WORKFLOW_FOLDER}/{name}"
    path = workflow_path_from_name(stored_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload.workflow, f, ensure_ascii=False, indent=2)
    return {"name": stored_name}

@app.put("/api/workflows/{name:path}/config")
def save_workflow_config(name: str, payload: WorkflowConfig):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    workflow_path = workflow_path_from_name(name)
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow not found")
    cfg_path = workflow_config_path(name)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(payload.dict(), f, ensure_ascii=False, indent=2)
    return {"config": payload.dict()}

@app.delete("/api/workflows/{name:path}")
def delete_workflow(name: str):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    if is_builtin_workflow(name):
        raise HTTPException(status_code=400, detail="内置工作流不可删除")
    workflow_path = workflow_path_from_name(name)
    cfg_path = workflow_config_path(name)
    if not os.path.exists(workflow_path):
        raise HTTPException(status_code=404, detail="Workflow not found")
    os.remove(workflow_path)
    if os.path.exists(cfg_path):
        os.remove(cfg_path)
    return {"ok": True}

@app.post("/api/workflows/{name:path}/run")
def run_workflow(name: str, payload: WorkflowRunRequest):
    if not WORKFLOW_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    if not os.path.exists(workflow_path_from_name(name)):
        raise HTTPException(status_code=404, detail="Workflow not found")
    # 根据 config 的字段把值映射成 params 节点覆盖
    params: Dict[str, Dict[str, Any]] = {}
    for field in payload.config.fields:
        if not field.node or not field.input:
            continue
        if field.id in payload.fields:
            value = payload.fields[field.id]
            # 类型转换
            if field.type in ("number", "slider"):
                try:
                    value = float(value) if (field.step and field.step < 1) else int(float(value))
                except Exception:
                    pass
            elif field.type == "boolean":
                value = bool(value)
            elif field.type == "dropdown":
                # 下拉值如果看起来是数字（如 "1024" / "2048" / "0.8"），自动转成 int/float
                if isinstance(value, str):
                    s = value.strip()
                    try:
                        if s and ('.' in s or 'e' in s.lower()):
                            value = float(s)
                        elif s and (s.lstrip('-').isdigit()):
                            value = int(s)
                    except (ValueError, TypeError):
                        pass
            params.setdefault(field.node, {})[field.input] = value
    req = GenerateRequest(
        prompt="",
        workflow_json=name,
        params=params,
        type="workflow-test",
        client_id=payload.client_id or str(uuid.uuid4()),
    )
    return generate(req)

@app.get("/api/workflow-apps")
def list_workflow_apps():
    with WORKFLOW_APP_LOCK:
        apps = [workflow_app_public_record(item) for item in load_workflow_app_store()]
    apps.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    return {"apps": apps, "root": WORKFLOW_APP_DIR}

@app.get("/workflow-apps/{app_id}")
@app.get("/workflow-apps/{app_id}/")
def open_workflow_app(app_id: str):
    with WORKFLOW_APP_LOCK:
        record = find_workflow_app_record(app_id)
    if not record:
        raise HTTPException(status_code=404, detail="App not found")
    public = workflow_app_public_record(record)
    if not public.get("available"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    workflow_name = str(public.get("workflow_name") or "").strip()
    config = load_workflow_config_dict(workflow_name)
    return Response(workflow_app_runner_html(public, config), media_type="text/html")

@app.get("/api/workflow-apps/{app_id}")
def get_workflow_app(app_id: str):
    with WORKFLOW_APP_LOCK:
        record = next((item for item in load_workflow_app_store() if item.get("id") == app_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="App not found")
    public = workflow_app_public_record(record)
    workflow_name = str(public.get("workflow_name") or "").strip()
    config = load_workflow_config_dict(workflow_name) if public.get("available") else {"fields": []}
    return {"app": public, "config": config}

def save_workflow_app_payload(payload: WorkflowAppPayload):
    framework = str(payload.framework or "react").strip().lower()
    if framework not in {"react", "vue", "gradio", "streamlit"}:
        raise HTTPException(status_code=400, detail="Unsupported framework")
    workflow_name = str(payload.workflow_name or "").strip()
    if not workflow_name or not WORKFLOW_NAME_RE.match(workflow_name):
        raise HTTPException(status_code=400, detail="Invalid workflow name")
    if not os.path.exists(workflow_path_from_name(workflow_name)):
        raise HTTPException(status_code=404, detail="Workflow not found")

    config = load_workflow_config_dict(workflow_name)
    app_id = str(payload.id or "").strip() or f"app_{uuid.uuid4().hex[:12]}"
    title = str(payload.title or config.get("title") or workflow_name.replace(".json", "")).strip()
    slug = safe_workflow_app_slug(payload.slug or title or workflow_name, app_id)
    now = now_ms()
    record = {
        "id": app_id,
        "title": title,
        "description": str(payload.description or "").strip(),
        "workflow_name": workflow_name,
        "framework": framework,
        "slug": slug,
        "api_base_url": str(payload.api_base_url or "").strip() or "http://127.0.0.1:13000",
        "category": workflow_app_category(payload.category),
        "tags": workflow_app_tags(payload.tags),
        "cover_image": str(payload.cover_image or "").strip(),
        "preview_images": workflow_app_preview_images(payload.preview_images),
        "author": str(payload.author or "Hanako").strip(),
        "created_at": now,
        "updated_at": now,
    }
    with WORKFLOW_APP_LOCK:
        apps = load_workflow_app_store()
        existing = next((item for item in apps if item.get("id") == app_id), None)
        if existing:
            record["created_at"] = existing.get("created_at") or now
            existing.update(record)
            record = existing
        else:
            apps.append(record)
        record = generate_workflow_app_files(record)
        save_workflow_app_store(apps)
    return {"app": record}

@app.post("/api/workflow-apps")
def save_workflow_app(payload: WorkflowAppPayload):
    return save_workflow_app_payload(payload)

@app.put("/api/workflow-apps/{app_id}")
def update_workflow_app(app_id: str, payload: WorkflowAppPayload):
    if hasattr(payload, "model_copy"):
        payload = payload.model_copy(update={"id": app_id})
    else:
        payload = payload.copy(update={"id": app_id})
    return save_workflow_app_payload(payload)

@app.post("/api/workflow-apps/{app_id}/generate")
def regenerate_workflow_app(app_id: str):
    with WORKFLOW_APP_LOCK:
        apps = load_workflow_app_store()
        record = next((item for item in apps if item.get("id") == app_id), None)
        if not record:
            raise HTTPException(status_code=404, detail="App not found")
        record = generate_workflow_app_files(record)
        save_workflow_app_store(apps)
    return {"app": record}

@app.post("/api/workflow-apps/{app_id}/run")
def run_workflow_app(app_id: str, payload: WorkflowAppRunRequest):
    with WORKFLOW_APP_LOCK:
        record = next((item for item in load_workflow_app_store() if item.get("id") == app_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="App not found")
    public = workflow_app_public_record(record)
    if not public.get("available"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    workflow_name = str(public.get("workflow_name") or "").strip()
    config = WorkflowConfig(**load_workflow_config_dict(workflow_name))
    return run_workflow(
        workflow_name,
        WorkflowRunRequest(
            fields=payload.fields or {},
            config=config,
            client_id=payload.client_id or f"workflow-app-{app_id}-{uuid.uuid4().hex[:8]}",
        ),
    )

@app.delete("/api/workflow-apps/{app_id}")
def remove_workflow_app(app_id: str):
    with WORKFLOW_APP_LOCK:
        apps = load_workflow_app_store()
        kept = [item for item in apps if item.get("id") != app_id]
        if len(kept) == len(apps):
            raise HTTPException(status_code=404, detail="App not found")
        save_workflow_app_store(kept)
    return {"ok": True}

@app.get("/api/workflow-apps/{app_id}/download")
def download_workflow_app(app_id: str):
    with WORKFLOW_APP_LOCK:
        apps = load_workflow_app_store()
        record = next((item for item in apps if item.get("id") == app_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="App not found")
    root = workflow_app_path(record.get("slug") or record.get("id") or app_id)
    if not os.path.isdir(root):
        record = generate_workflow_app_files(record)
        root = record.get("app_dir") or root
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, root).replace("\\", "/")
                zf.write(path, rel)
    buffer.seek(0)
    filename = f"{safe_workflow_app_slug(record.get('slug') or record.get('title') or app_id)}.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)

