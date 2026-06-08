import json
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
    gr.Markdown(f"# {CONFIG.get('title', 'ComfyUI Workflow App')}\n`{CONFIG.get('workflowName')}`")
    inputs = [component_for(field) for field in FIELDS]
    run = gr.Button("Run workflow", variant="primary")
    gallery = gr.Gallery(label="Outputs")
    status = gr.Textbox(label="Status", interactive=False)
    run.click(run_workflow, inputs=inputs, outputs=[gallery, status])

if __name__ == "__main__":
    demo.launch()
