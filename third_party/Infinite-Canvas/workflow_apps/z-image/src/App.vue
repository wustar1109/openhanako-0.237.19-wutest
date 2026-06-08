<script setup>
import { computed, ref } from 'vue';
import appConfig from './workflowConfig.json';

const mediaTypes = new Set(['image', 'video', 'audio']);
const apiBase = (import.meta.env.VITE_HANAKO_API_BASE || appConfig.apiBaseUrl || window.location.origin).replace(/\/$/, '');
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
