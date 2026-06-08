import React, { useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import appConfig from './workflowConfig.json';
import './styles.css';

const mediaTypes = new Set(['image', 'video', 'audio']);
const apiBase = (import.meta.env.VITE_HANAKO_API_BASE || appConfig.apiBaseUrl || window.location.origin).replace(/\/$/, '');

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
