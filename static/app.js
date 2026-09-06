const $ = (selector) => document.querySelector(selector);

const els = {
  sourceSelect: $("#sourceSelect"), refreshFiles: $("#refreshFiles"), empty: $("#emptyState"), workspace: $("#workspace"),
  uploadButton: $("#uploadButton"), uploadInput: $("#uploadInput"), uploadPanel: $("#uploadPanel"), uploadStatus: $("#uploadStatus"),
  sourceName: $("#sourceName"), sourceDuration: $("#sourceDuration"), sourceFormat: $("#sourceFormat"), sourceGpmd: $("#sourceGpmd"),
  proxyStatus: $("#proxyStatus"), proxyOverlay: $("#proxyOverlay"), video: $("#video"), currentTime: $("#currentTime"),
  taskLabel: $("#taskLabel"), inTime: $("#inTime"), outTime: $("#outTime"), markedDuration: $("#markedDuration"),
  setIn: $("#setIn"), setOut: $("#setOut"), addClip: $("#addClip"), addClipText: $("#addClipText"), cancelEdit: $("#cancelEdit"),
  formError: $("#formError"), timeline: $("#timeline"), timelineClips: $("#timelineClips"), timelineEnd: $("#timelineEnd"), playhead: $("#playhead"),
  clipCount: $("#clipCount"), clipList: $("#clipList"), retainPreZero: $("#retainPreZero"), exportAll: $("#exportAll"), exportProgress: $("#exportProgress"),
};

let sources = [];
let source = null;
let project = null;
let selectedClipId = null;
let editingClipId = null;
let saveTimer = null;
let exportStatuses = {};

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) return "00:00.000";
  const totalMillis = Math.round(Math.max(0, seconds) * 1000);
  const hours = Math.floor(totalMillis / 3_600_000);
  const minutes = Math.floor((totalMillis % 3_600_000) / 60_000);
  const secs = Math.floor((totalMillis % 60_000) / 1000);
  const millis = totalMillis % 1000;
  if (hours > 0) return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function parseTime(raw) {
  const value = raw.trim();
  if (!value) return NaN;
  const parts = value.split(":").map(Number);
  if (parts.some((item) => !Number.isFinite(item)) || parts.length > 3) return NaN;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] * 3600 + parts[1] * 60 + parts[2];
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) { /* response was not JSON */ }
    throw new Error(message);
  }
  return response.json();
}

async function loadFiles(preferredId = null) {
  els.sourceSelect.innerHTML = '<option value="">Choose a recording…</option>';
  try {
    const payload = await api("/api/files");
    sources = payload.files;
    for (const item of sources) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.filename} · ${formatTime(item.duration)}`;
      els.sourceSelect.append(option);
    }
    const wanted = preferredId && sources.some((item) => item.id === preferredId) ? preferredId : sources[0]?.id;
    if (wanted) {
      els.sourceSelect.value = wanted;
      await selectSource(wanted);
    } else {
      els.empty.hidden = false;
      els.workspace.hidden = true;
    }
    if (payload.errors.length) els.formError.textContent = payload.errors.map((item) => `${item.filename}: ${item.error}`).join(" · ");
  } catch (error) {
    els.empty.hidden = false;
    els.workspace.hidden = true;
    els.empty.querySelector("p").textContent = `Could not enumerate MEDIA_DIR: ${error.message}`;
  }
}

async function selectSource(fileId) {
  source = sources.find((item) => item.id === fileId) || null;
  if (!source) {
    els.workspace.hidden = true;
    els.empty.hidden = false;
    els.video.removeAttribute("src");
    els.video.load();
    return;
  }
  selectedClipId = null;
  editingClipId = null;
  exportStatuses = {};
  els.empty.hidden = true;
  els.workspace.hidden = false;
  els.sourceName.textContent = source.filename;
  els.sourceDuration.textContent = formatTime(source.duration);
  els.sourceFormat.textContent = `${source.width}×${source.height} · ${source.fps?.toFixed(2) || "?"} fps · ${source.video_codec || "?"}`;
  els.sourceGpmd.textContent = source.gpmd_present ? "✓ present" : "✕ unavailable";
  els.sourceGpmd.style.color = source.gpmd_present ? "var(--success)" : "var(--danger)";
  els.timelineEnd.textContent = formatTime(source.duration);
  project = await api(`/api/projects/${source.id}`);
  els.retainPreZero.checked = project.imu_settings.retain_one_pre_zero_sample;
  resetForm(false);
  render();
  await ensureProxy();
}

async function ensureProxy() {
  const fileId = source.id;
  els.proxyStatus.textContent = source.proxy_status === "ready" ? "cached" : "queued";
  setProxyOverlay("Preparing review proxy…", "Only one proxy is generated at a time.", source.proxy_status !== "ready");
  try {
    let job = await api(`/api/sources/${fileId}/proxy`, {method: "POST", body: "{}"});
    while (!["ready", "failed"].includes(job.state)) {
      if (!source || source.id !== fileId) return;
      const percent = Math.round((job.progress || 0) * 100);
      els.proxyStatus.textContent = job.state === "queued" ? "queued" : `${percent}%`;
      setProxyOverlay(job.message, job.state === "queued" ? "Waiting for the active proxy to finish." : `${percent}% complete`, true);
      await new Promise((resolve) => setTimeout(resolve, 750));
      job = await api(`/api/sources/${fileId}/proxy/status`);
    }
    if (job.state === "failed") throw new Error(job.message);
    if (!source || source.id !== fileId) return;
    source.proxy_status = "ready";
    els.proxyStatus.textContent = "ready";
    setProxyOverlay("", "", false);
    els.video.src = `/api/sources/${fileId}/proxy?v=${encodeURIComponent(fileId)}`;
    els.video.load();
  } catch (error) {
    if (!source || source.id !== fileId) return;
    els.proxyStatus.textContent = "failed";
    setProxyOverlay("Proxy generation failed", error.message, true);
  }
}

function setProxyOverlay(title, detail, visible) {
  els.proxyOverlay.replaceChildren();
  const titleNode = document.createElement("div");
  titleNode.textContent = title;
  const detailNode = document.createElement("small");
  detailNode.textContent = detail;
  els.proxyOverlay.append(titleNode, detailNode);
  els.proxyOverlay.hidden = !visible;
}

function uploadOne(file, position, total) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `/api/uploads?filename=${encodeURIComponent(file.name)}`);
    request.setRequestHeader("Content-Type", file.type || "video/mp4");
    request.upload.addEventListener("progress", (event) => {
      const percent = event.lengthComputable ? Math.round((event.loaded / event.total) * 100) : 0;
      els.uploadStatus.className = "upload-status active";
      els.uploadStatus.textContent = `Uploading ${position}/${total}: ${file.name} · ${percent}%`;
    });
    request.addEventListener("load", () => {
      let payload = {};
      try { payload = JSON.parse(request.responseText); } catch (_) { /* handled below */ }
      if (request.status >= 200 && request.status < 300) resolve(payload);
      else reject(new Error(payload.detail || `Upload failed with HTTP ${request.status}`));
    });
    request.addEventListener("error", () => reject(new Error(`Network error while uploading ${file.name}`)));
    request.send(file);
  });
}

async function uploadFiles(fileList) {
  const files = [...fileList].filter((file) => file.name.toLowerCase().endsWith(".mp4"));
  if (!files.length) {
    els.uploadStatus.className = "upload-status error";
    els.uploadStatus.textContent = "Choose one or more MP4 files.";
    return;
  }
  els.uploadButton.disabled = true;
  const failures = [];
  let lastUploadedId = null;
  for (let index = 0; index < files.length; index += 1) {
    try {
      const result = await uploadOne(files[index], index + 1, files.length);
      lastUploadedId = result.file.id;
    } catch (error) {
      failures.push(`${files[index].name}: ${error.message}`);
    }
  }
  els.uploadButton.disabled = false;
  els.uploadInput.value = "";
  if (failures.length) {
    els.uploadStatus.className = "upload-status error";
    els.uploadStatus.textContent = `${files.length - failures.length}/${files.length} uploaded · ${failures.join(" · ")}`;
  } else {
    els.uploadStatus.className = "upload-status";
    els.uploadStatus.textContent = `${files.length} file${files.length === 1 ? "" : "s"} uploaded`;
  }
  await loadFiles(lastUploadedId || source?.id);
}

function projectPayload() {
  return {
    source_file: project.source_file,
    source_id: project.source_id,
    imu_settings: project.imu_settings,
    clips: project.clips,
    next_clip_index: project.next_clip_index,
  };
}

async function saveProject() {
  clearTimeout(saveTimer);
  if (!project || !source) return;
  project = await api(`/api/projects/${source.id}`, {method: "PUT", body: JSON.stringify(projectPayload())});
}

function updateMarkedDuration() {
  const start = parseTime(els.inTime.value);
  const end = parseTime(els.outTime.value);
  els.markedDuration.textContent = Number.isFinite(start) && Number.isFinite(end) && end >= start ? formatTime(end - start) : "—";
}

function resetForm(clearLabel = false) {
  editingClipId = null;
  els.inTime.value = "00:00.000";
  els.outTime.value = "00:00.000";
  if (clearLabel) els.taskLabel.value = "";
  els.addClipText.textContent = "Add Clip";
  els.cancelEdit.hidden = true;
  els.formError.textContent = "";
  updateMarkedDuration();
}

function validateForm() {
  const start = parseTime(els.inTime.value);
  const end = parseTime(els.outTime.value);
  const label = els.taskLabel.value.trim();
  if (!label) throw new Error("Enter a task label.");
  if (!Number.isFinite(start) || !Number.isFinite(end)) throw new Error("Use seconds, MM:SS.mmm, or HH:MM:SS.mmm timestamps.");
  if (start < 0 || end <= start) throw new Error("OUT must be after IN.");
  if (end > source.duration + 0.001) throw new Error("OUT is beyond the source duration.");
  return {start, end, label};
}

async function addOrUpdateClip() {
  try {
    const {start, end, label} = validateForm();
    if (editingClipId) {
      const clip = project.clips.find((item) => item.id === editingClipId);
      Object.assign(clip, {task_label: label, requested_start_s: start, requested_end_s: end});
      selectedClipId = clip.id;
    } else {
      const index = project.next_clip_index;
      const clip = {
        id: `${source.source_id}_${String(index).padStart(3, "0")}`,
        clip_index: index,
        task_label: label,
        requested_start_s: start,
        requested_end_s: end,
      };
      project.clips.push(clip);
      project.next_clip_index += 1;
      selectedClipId = clip.id;
    }
    await saveProject();
    resetForm(false);
    render();
  } catch (error) {
    els.formError.textContent = error.message;
  }
}

function editClip(clip) {
  selectedClipId = clip.id;
  editingClipId = clip.id;
  els.taskLabel.value = clip.task_label;
  els.inTime.value = formatTime(clip.requested_start_s);
  els.outTime.value = formatTime(clip.requested_end_s);
  els.addClipText.textContent = "Save Changes";
  els.cancelEdit.hidden = false;
  updateMarkedDuration();
  render();
  els.taskLabel.focus();
}

function selectClip(clip, jump = false) {
  selectedClipId = clip.id;
  if (jump) {
    els.video.currentTime = clip.requested_start_s;
    els.video.scrollIntoView({behavior: "smooth", block: "center"});
  }
  render();
}

async function deleteClip(clip) {
  if (!confirm(`Delete #${String(clip.clip_index).padStart(3, "0")} ${clip.task_label}? Its clip number will remain unused.`)) return;
  project.clips = project.clips.filter((item) => item.id !== clip.id);
  if (selectedClipId === clip.id) selectedClipId = null;
  if (editingClipId === clip.id) resetForm(false);
  await saveProject();
  render();
}

function actionButton(text, action, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.className = className;
  button.addEventListener("click", (event) => { event.stopPropagation(); action(); });
  return button;
}

function render() {
  if (!project || !source) return;
  const ordered = [...project.clips].sort((a, b) => a.clip_index - b.clip_index);
  els.clipCount.textContent = `${ordered.length} clip${ordered.length === 1 ? "" : "s"}`;
  els.timelineClips.replaceChildren();
  els.clipList.replaceChildren();

  const laneEnds = [];
  const lanes = {};
  for (const clip of [...ordered].sort((a, b) => a.requested_start_s - b.requested_start_s)) {
    let lane = laneEnds.findIndex((end) => end <= clip.requested_start_s);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = clip.requested_end_s;
    lanes[clip.id] = lane;
  }
  els.timeline.style.height = `${Math.max(150, 65 + laneEnds.length * 25)}px`;

  if (!ordered.length) {
    const empty = document.createElement("div");
    empty.className = "clip-empty";
    empty.textContent = "No clips marked yet. Set IN and OUT, then add your first task clip.";
    els.clipList.append(empty);
  }

  ordered.forEach((clip, order) => {
    const range = document.createElement("button");
    range.className = `timeline-clip${selectedClipId === clip.id ? " selected" : ""}`;
    range.style.left = `${(clip.requested_start_s / source.duration) * 100}%`;
    range.style.width = `${((clip.requested_end_s - clip.requested_start_s) / source.duration) * 100}%`;
    range.style.top = `${lanes[clip.id] * 25}px`;
    range.textContent = `#${String(clip.clip_index).padStart(3, "0")} ${clip.task_label}`;
    range.title = `${clip.task_label}: ${formatTime(clip.requested_start_s)} → ${formatTime(clip.requested_end_s)}`;
    range.addEventListener("click", () => selectClip(clip, false));
    els.timelineClips.append(range);

    const row = document.createElement("div");
    row.className = `clip-row${selectedClipId === clip.id ? " selected" : ""}`;
    row.addEventListener("click", () => selectClip(clip, false));
    const number = document.createElement("div");
    number.className = "clip-number";
    number.textContent = `#${String(clip.clip_index).padStart(3, "0")}`;
    const detail = document.createElement("div");
    detail.className = "clip-detail";
    const title = document.createElement("strong");
    title.textContent = clip.task_label;
    const status = exportStatuses[clip.id];
    if (status) {
      const badge = document.createElement("span");
      badge.className = `status ${status.status}`;
      badge.textContent = status.status;
      badge.title = status.message;
      title.append(badge);
    }
    const timing = document.createElement("span");
    timing.textContent = `${formatTime(clip.requested_start_s)} → ${formatTime(clip.requested_end_s)} · ${formatTime(clip.requested_end_s - clip.requested_start_s)}`;
    if (clip.requested_end_s - clip.requested_start_s < 120) {
      const warning = document.createElement("span");
      warning.className = "short-warning";
      warning.textContent = "under 2 min";
      timing.append(warning);
    }
    detail.append(title, timing);
    const actions = document.createElement("div");
    actions.className = "clip-actions";
    actions.append(
      actionButton("Jump", () => selectClip(clip, true)),
      actionButton("Edit", () => editClip(clip)),
      actionButton("Delete", () => deleteClip(clip).catch((error) => { els.formError.textContent = error.message; }), "delete"),
    );
    row.append(number, detail, actions);
    els.clipList.append(row);
  });
  els.exportAll.disabled = !ordered.length;
}

async function exportAll() {
  if (!project.clips.length) return;
  const short = project.clips.filter((clip) => clip.requested_end_s - clip.requested_start_s < 120);
  if (short.length && !confirm(`${short.length} clip(s) are under the recommended 2 minutes. Export anyway?`)) return;
  els.exportAll.disabled = true;
  els.exportProgress.textContent = "Saving project…";
  try {
    await saveProject();
    const started = await api(`/api/projects/${source.id}/export`, {method: "POST", body: JSON.stringify({clip_ids: null})});
    await pollExport(started.status_url);
  } catch (error) {
    els.exportProgress.textContent = `Export failed: ${error.message}`;
  } finally {
    els.exportAll.disabled = false;
  }
}

async function pollExport(statusUrl) {
  while (true) {
    const job = await api(statusUrl);
    exportStatuses = Object.fromEntries(job.clips.map((item) => [item.clip_id, item]));
    els.exportProgress.textContent = job.state === "running" ? `${job.message} · ${job.completed}/${job.total} complete` : job.message;
    render();
    if (["complete", "failed"].includes(job.state)) return;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

els.sourceSelect.addEventListener("change", () => selectSource(els.sourceSelect.value).catch((error) => { els.empty.hidden = false; els.empty.querySelector("p").textContent = error.message; }));
els.refreshFiles.addEventListener("click", () => loadFiles(source?.id));
els.uploadButton.addEventListener("click", () => els.uploadInput.click());
els.uploadInput.addEventListener("change", () => uploadFiles(els.uploadInput.files));
els.uploadPanel.addEventListener("click", (event) => { if (event.target !== els.uploadButton) els.uploadInput.click(); });
els.uploadPanel.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); els.uploadInput.click(); } });
for (const eventName of ["dragenter", "dragover"]) {
  els.uploadPanel.addEventListener(eventName, (event) => { event.preventDefault(); els.uploadPanel.classList.add("dragging"); });
}
for (const eventName of ["dragleave", "drop"]) {
  els.uploadPanel.addEventListener(eventName, (event) => { event.preventDefault(); els.uploadPanel.classList.remove("dragging"); });
}
els.uploadPanel.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));
els.setIn.addEventListener("click", () => { els.inTime.value = formatTime(els.video.currentTime); updateMarkedDuration(); });
els.setOut.addEventListener("click", () => { els.outTime.value = formatTime(els.video.currentTime); updateMarkedDuration(); });
els.addClip.addEventListener("click", addOrUpdateClip);
els.cancelEdit.addEventListener("click", () => { resetForm(false); render(); });
els.inTime.addEventListener("input", updateMarkedDuration);
els.outTime.addEventListener("input", updateMarkedDuration);
els.retainPreZero.addEventListener("change", () => {
  project.imu_settings.retain_one_pre_zero_sample = els.retainPreZero.checked;
  saveProject().catch((error) => { els.formError.textContent = error.message; });
});
els.exportAll.addEventListener("click", exportAll);
els.video.addEventListener("timeupdate", () => {
  els.currentTime.textContent = formatTime(els.video.currentTime);
  if (source) els.playhead.style.left = `${Math.min(100, (els.video.currentTime / source.duration) * 100)}%`;
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target.matches("input, textarea, select") || target.isContentEditable || !source) return;
  if (event.code === "Space") {
    event.preventDefault();
    els.video.paused ? els.video.play() : els.video.pause();
  } else if (event.key.toLowerCase() === "i") {
    event.preventDefault(); els.setIn.click();
  } else if (event.key.toLowerCase() === "o") {
    event.preventDefault(); els.setOut.click();
  } else if (event.key === "Enter") {
    event.preventDefault(); addOrUpdateClip();
  } else if (event.key === "ArrowLeft") {
    event.preventDefault(); els.video.currentTime = Math.max(0, els.video.currentTime - 1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault(); els.video.currentTime = Math.min(source.duration, els.video.currentTime + 1);
  }
});

loadFiles();
