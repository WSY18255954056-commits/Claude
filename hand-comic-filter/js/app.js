// 应用入口：摄像头 → 手部识别 → 手势状态机 → WebGL 渲染
import { FILTERS, DEFAULT_REGION_FILTERS } from './filters.js';
import { GestureController, pairHands, regionQuads, rectCorners, LM } from './gesture.js';
import { Renderer } from './renderer.js';
import { startCamera, createHandLandmarker, toHands } from './tracker.js';
import { drawDebug } from './debug.js';

const $ = (sel) => document.querySelector(sel);
const video = $('#camera');
const glCanvas = $('#gl');
const debugCanvas = $('#debug');
const debugCtx = debugCanvas.getContext('2d');
const statusEl = $('#status');
const badgeEl = $('#mode-badge');
const noticeEl = $('#notice');
const debugToggle = $('#debug-toggle');
const clearButton = $('#clear-frozen');
const regionSelects = [0, 1, 2].map((i) => $(`#region-filter-${i}`));

// 整幅画面的四个角（归一化视频坐标）
const FULL_FRAME = [[0, 0], [1, 0], [1, 1], [0, 1]];

const gesture = new GestureController({ filterCount: FILTERS.length, initialFilter: 0 });
const state = {
  regionFilters: [...DEFAULT_REGION_FILTERS],
  regions: [], // 当前帧的普通模式四边形
  points: [], // debug 用的指尖点
  frozen: [], // 冻结窗口：{ rect, filter, corners, uvs, texture }
};
let renderer = null;
let landmarker = null;
let lastVideoTime = -1;

// 冻结窗口的纹理坐标：把窗口自身映射到 [0,1]
const localUvs = (rect) =>
  rectCorners(rect).map(([x, y]) => [
    (x - rect.x0) / (rect.x1 - rect.x0),
    (y - rect.y0) / (rect.y1 - rect.y0),
  ]);

// 复制当前画面中窗口所在的区域，生成静态纹理。之后位置和滤镜都不再变化
function freezeWindow(rect, filter) {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  const sx = Math.floor(rect.x0 * vw);
  const sy = Math.floor(rect.y0 * vh);
  const sw = Math.min(vw - sx, Math.max(1, Math.round((rect.x1 - rect.x0) * vw)));
  const sh = Math.min(vh - sy, Math.max(1, Math.round((rect.y1 - rect.y0) * vh)));

  const crop = document.createElement('canvas');
  crop.width = sw;
  crop.height = sh;
  crop.getContext('2d').drawImage(video, sx, sy, sw, sh, 0, 0, sw, sh);

  state.frozen.push({
    rect,
    filter,
    corners: rectCorners(rect),
    uvs: localUvs(rect),
    texture: renderer.createTexture(crop),
  });
}

// 调试用：普通模式显示四个指尖，新模式只显示拇指和食指
function trackedPoints(pair, mode) {
  const ids =
    mode === 'window'
      ? [LM.THUMB_TIP, LM.INDEX_TIP]
      : [LM.THUMB_TIP, LM.INDEX_TIP, LM.MIDDLE_TIP, LM.PINKY_TIP];
  return [pair.left, pair.right].flatMap((lm) => ids.map((i) => [lm[i].x, lm[i].y]));
}

function onFrame(now) {
  requestAnimationFrame(onFrame);
  if (!renderer || video.readyState < 2) return;

  // 只在拿到新的视频帧时跑一次识别，避免重复推理
  if (video.currentTime !== lastVideoTime) {
    lastVideoTime = video.currentTime;
    const hands = toHands(landmarker.detectForVideo(video, now));
    const pair = pairHands(hands);
    const events = gesture.update(now, pair);
    if (events.copy) freezeWindow(events.copy.rect, events.copy.filter);

    state.regions = pair && gesture.mode === 'normal' ? regionQuads(pair) : [];
    state.points = pair ? trackedPoints(pair, gesture.mode) : [];
    updateHud(hands.length);
  }
  render();
}

function render() {
  const videoTex = renderer.uploadVideo(video);
  renderer.clear();

  // 1. 原始摄像头画面作为底图
  renderer.drawQuad(videoTex, FULL_FRAME, FULL_FRAME, -1);

  // 2. 普通模式：三个指尖区域
  if (gesture.mode === 'normal') {
    state.regions.forEach((r, i) => {
      renderer.drawQuad(videoTex, r.quad, r.quad, state.regionFilters[i]);
    });
  }

  // 3. 冻结窗口：固定位置与滤镜
  for (const f of state.frozen) {
    renderer.drawQuad(f.texture, f.corners, f.uvs, f.filter);
  }

  // 4. live 窗口：盖在最上层
  if (gesture.liveRect) {
    const corners = rectCorners(gesture.liveRect);
    renderer.drawQuad(videoTex, corners, corners, gesture.liveFilter);
  }

  drawOverlay();
}

function drawOverlay() {
  if (debugCanvas.width !== glCanvas.width || debugCanvas.height !== glCanvas.height) {
    debugCanvas.width = glCanvas.width;
    debugCanvas.height = glCanvas.height;
  }
  if (!debugToggle.checked) {
    debugCtx.clearRect(0, 0, debugCanvas.width, debugCanvas.height);
    return;
  }
  drawDebug(debugCtx, {
    width: debugCanvas.width,
    height: debugCanvas.height,
    regions: state.regions.map((r, i) => ({
      quad: r.quad,
      label: `${r.label}：${FILTERS[state.regionFilters[i]].name}`,
    })),
    frozenRects: state.frozen.map((f) => f.rect),
    liveRect: gesture.liveRect,
    liveLabel: `live：${FILTERS[gesture.liveFilter].name}`,
    points: state.points,
  });
}

function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function updateHud(handCount) {
  const inWindow = gesture.mode === 'window';
  badgeEl.classList.toggle('window', inWindow);
  setText(badgeEl, inWindow ? `新模式 · ${FILTERS[gesture.liveFilter].name}` : '普通模式');

  let status = `检测到 ${handCount} 只手 · 冻结窗口 ${state.frozen.length} 个`;
  if (handCount < 2) status += ' · 请让双手入镜';
  if (inWindow && gesture.pinched && !gesture.armed) status += ' · 捏合中，松开后可再次复制';
  setText(statusEl, status);
}

function setupSelectors() {
  const options = FILTERS.map((f, i) => `<option value="${i}">${f.name}</option>`).join('');
  regionSelects.forEach((sel, i) => {
    sel.innerHTML = options;
    sel.value = String(state.regionFilters[i]);
    sel.addEventListener('change', () => {
      state.regionFilters[i] = Number(sel.value);
    });
  });
}

clearButton.addEventListener('click', () => {
  for (const f of state.frozen) renderer.deleteTexture(f.texture);
  state.frozen.length = 0;
});

function showNotice(message) {
  noticeEl.textContent = message;
  noticeEl.hidden = false;
}

function describeError(err) {
  switch (err?.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return '没有获得摄像头权限。请在浏览器地址栏允许摄像头后刷新页面。';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return '没有找到可用的摄像头。';
    case 'NotReadableError':
      return '摄像头正被其他程序占用，请关闭后刷新页面。';
    default:
      return err?.message || String(err);
  }
}

async function boot() {
  setupSelectors();

  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    setText(statusEl, '无法访问摄像头');
    showNotice('摄像头只能在 localhost、127.0.0.1 或 HTTPS 页面中使用。请用本地服务器打开，例如 http://localhost:8000');
    return;
  }

  try {
    setText(statusEl, '请允许摄像头访问…');
    await startCamera(video);
    setText(statusEl, '正在加载手部识别模型…');
    renderer = new Renderer(glCanvas);
    landmarker = await createHandLandmarker();
    setText(statusEl, '准备就绪');
    requestAnimationFrame(onFrame);
  } catch (err) {
    console.error(err);
    setText(statusEl, '启动失败');
    showNotice(describeError(err));
  }
}

boot();
