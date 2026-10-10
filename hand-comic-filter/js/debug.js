// Debug 线框：在 2D 画布上画出识别到的四边形、窗口与指尖点
import { rectCorners } from './gesture.js';

const REGION_COLORS = ['#ff4d6d', '#ffd166', '#06d6a0'];
const FROZEN_COLOR = '#b388ff';
const LIVE_COLOR = '#4cc9f0';

// regions: [{ quad, label }]；frozenRects: [rect]；liveRect: rect|null；points: [[x, y]]
export function drawDebug(ctx, { width, height, regions, frozenRects, liveRect, liveLabel, points }) {
  ctx.clearRect(0, 0, width, height);
  const fontPx = Math.max(12, Math.round(width * 0.02));
  ctx.font = `600 ${fontPx}px system-ui, sans-serif`;
  ctx.lineWidth = Math.max(2, Math.round(width / 400));

  // 与 renderer 一致，画面做水平镜像
  const toScreen = ([x, y]) => [(1 - x) * width, y * height];

  const polygon = (pts, color, dash = []) => {
    ctx.beginPath();
    pts.forEach((p, i) => {
      const [X, Y] = toScreen(p);
      if (i === 0) ctx.moveTo(X, Y);
      else ctx.lineTo(X, Y);
    });
    ctx.closePath();
    ctx.setLineDash(dash);
    ctx.strokeStyle = color;
    ctx.stroke();
    ctx.setLineDash([]);
  };

  // 标签夹在画布内，避免贴边时被裁掉
  const label = (text, pt, color) => {
    const [X, Y] = toScreen(pt);
    const w = ctx.measureText(text).width;
    ctx.fillStyle = color;
    ctx.fillText(text, Math.min(Math.max(X, 4), width - w - 4), Math.max(Y - 6, fontPx));
  };

  regions.forEach((r, i) => {
    const color = REGION_COLORS[i % REGION_COLORS.length];
    polygon(r.quad, color);
    // 标签放在四边形最靠上的顶点，三个区域的标签不会挤在中心
    const top = r.quad.reduce((a, b) => (b[1] < a[1] ? b : a));
    label(r.label, top, color);
  });

  frozenRects.forEach((rect) => {
    polygon(rectCorners(rect), FROZEN_COLOR, [fontPx / 2, fontPx / 2]);
  });

  if (liveRect) {
    const corners = rectCorners(liveRect);
    polygon(corners, LIVE_COLOR);
    if (liveLabel) label(liveLabel, corners[0], LIVE_COLOR);
  }

  ctx.fillStyle = '#ffffff';
  points.forEach((p) => {
    const [X, Y] = toScreen(p);
    ctx.beginPath();
    ctx.arc(X, Y, fontPx / 3, 0, Math.PI * 2);
    ctx.fill();
  });
}
