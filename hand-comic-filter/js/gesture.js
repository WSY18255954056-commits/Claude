// 手势逻辑：只做几何计算与状态判断，不依赖 DOM / WebGL，便于 node 单测。

// MediaPipe Hand Landmarker 的关键点编号
export const LM = {
  WRIST: 0,
  THUMB_TIP: 4,
  INDEX_MCP: 5,
  INDEX_TIP: 8,
  MIDDLE_TIP: 12,
  PINKY_TIP: 20,
};

// 阈值集中放在这里，调手感只改这一处
export const DEFAULT_CONFIG = {
  pinchOn: 0.3, // 双手的 拇指-食指比例 都低于它，视为捏合
  pinchOff: 0.45, // 任一手高于它，视为松开（滞回，避免抖动误判）
  gatherOn: 0.8, // 双手都低于它（拇指食指靠拢），持续 enterHoldMs 后进入新模式
  gatherOff: 1.05, // 任一手高于它（张开），持续 exitHoldMs 后退出新模式
  enterHoldMs: 250,
  exitHoldMs: 300,
  lostHoldMs: 400, // 手丢失超过该时长，退出新模式
  minWindowSize: 0.03, // 窗口任一边的最小长度（归一化坐标），小于它则不显示、不能复制
  followAlpha: 0.6, // live 窗口跟随系数，1 为无平滑
};

// 普通模式的三个区域：相邻指尖之间的四边形
export const REGION_DEFS = [
  { id: 'thumb-index', label: 'Thumb → Index', from: LM.THUMB_TIP, to: LM.INDEX_TIP },
  { id: 'index-middle', label: 'Index → Middle', from: LM.INDEX_TIP, to: LM.MIDDLE_TIP },
  { id: 'middle-pinky', label: 'Middle → Pinky', from: LM.MIDDLE_TIP, to: LM.PINKY_TIP },
];

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
const midpoint = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
const lerpPoint = (a, b, k) => ({ x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k });
const toPair = (p) => [p.x, p.y];

// 手掌尺度：手腕到食指根部的距离，用来消除手离摄像头远近的影响
export function handScale(lm) {
  return Math.max(dist(lm[LM.WRIST], lm[LM.INDEX_MCP]), 1e-6);
}

// 拇指指尖与食指指尖的距离 / 手掌尺度。越小越"捏合"
export function pinchRatio(lm) {
  return dist(lm[LM.THUMB_TIP], lm[LM.INDEX_TIP]) / handScale(lm);
}

// hands: [{ landmarks, label }]，label 为 'Left' | 'Right'
// 返回 { left, right }（各为 21 点数组）；不足两只手返回 null
export function pairHands(hands) {
  if (!hands || hands.length < 2) return null;
  const left = hands.find((h) => h.label === 'Left');
  const right = hands.find((h) => h.label === 'Right' && h !== left);
  if (left && right) return { left: left.landmarks, right: right.landmarks };
  // 标签缺失或重复时，按手腕 x 坐标兜底排序
  const [a, b] = hands
    .slice(0, 2)
    .sort((p, q) => p.landmarks[LM.WRIST].x - q.landmarks[LM.WRIST].x);
  return { left: a.landmarks, right: b.landmarks };
}

// 普通模式：每一对相邻指尖在两只手之间围成一个四边形
// 四边形顶点顺序：左手 a → 左手 b → 右手 b → 右手 a
export function regionQuads({ left, right }) {
  return REGION_DEFS.map((r) => ({
    id: r.id,
    label: r.label,
    quad: [
      toPair(left[r.from]),
      toPair(left[r.to]),
      toPair(right[r.to]),
      toPair(right[r.from]),
    ],
  }));
}

// 新模式：两个中点作为矩形的一对对角顶点
export function frameRect(a, b) {
  return {
    x0: Math.min(a.x, b.x),
    y0: Math.min(a.y, b.y),
    x1: Math.max(a.x, b.x),
    y1: Math.max(a.y, b.y),
  };
}

export function rectCorners(r) {
  return [
    [r.x0, r.y0],
    [r.x1, r.y0],
    [r.x1, r.y1],
    [r.x0, r.y1],
  ];
}

export function rectIsValid(r, minSize) {
  return !!r && r.x1 - r.x0 >= minSize && r.y1 - r.y0 >= minSize;
}

// 手势状态机：模式切换、live 窗口跟随、捏合复制、滤镜轮换
export class GestureController {
  constructor({ filterCount, initialFilter = 0, config = {} }) {
    this.cfg = { ...DEFAULT_CONFIG, ...config };
    this.filterCount = filterCount;
    this.liveFilter = initialFilter;
    this.mode = 'normal'; // 'normal' 普通模式 | 'window' 拇指食指窗口模式
    this.liveRect = null; // 新模式下 live 窗口（归一化坐标）；不可见时为 null
    this.pinched = false; // 双手当前是否处于捏合状态（带滞回）
    this.armed = true; // 复制后需要先松开，才会再次复制
    this.gatherSince = null;
    this.exitSince = null;
    this.lostSince = null;
    this.smoothed = null; // 平滑后的两个中点
  }

  // now: 毫秒时间戳；pair: pairHands() 的结果或 null
  // 返回 { enteredWindow, exitedWindow, copy }，copy 为 { rect, filter } 或 null
  update(now, pair) {
    const out = { enteredWindow: false, exitedWindow: false, copy: null };
    const c = this.cfg;

    if (!pair) {
      this.gatherSince = null;
      this.exitSince = null;
      this.smoothed = null;
      this.liveRect = null;
      this.lostSince ??= now;
      if (this.mode === 'window' && now - this.lostSince >= c.lostHoldMs) {
        this.exitWindow();
        out.exitedWindow = true;
      }
      return out;
    }
    this.lostSince = null;

    const rl = pinchRatio(pair.left);
    const rr = pinchRatio(pair.right);
    const worst = Math.max(rl, rr);

    if (this.mode === 'normal') {
      this.gatherSince = worst < c.gatherOn ? (this.gatherSince ?? now) : null;
      if (this.gatherSince !== null && now - this.gatherSince >= c.enterHoldMs) {
        this.enterWindow();
        out.enteredWindow = true;
      }
    } else {
      this.exitSince = worst > c.gatherOff ? (this.exitSince ?? now) : null;
      if (this.exitSince !== null && now - this.exitSince >= c.exitHoldMs) {
        this.exitWindow();
        out.exitedWindow = true;
      }
    }

    if (this.mode === 'window') this.follow(pair);
    out.copy = this.updatePinch(rl, rr);
    return out;
  }

  enterWindow() {
    this.mode = 'window';
    this.gatherSince = null;
    this.exitSince = null;
    this.smoothed = null;
    this.liveRect = null;
  }

  exitWindow() {
    this.mode = 'normal';
    this.exitSince = null;
    this.smoothed = null;
    this.liveRect = null;
  }

  // 只用拇指和食指计算两个中点，其余手指不参与
  follow(pair) {
    const target = {
      l: midpoint(pair.left[LM.THUMB_TIP], pair.left[LM.INDEX_TIP]),
      r: midpoint(pair.right[LM.THUMB_TIP], pair.right[LM.INDEX_TIP]),
    };
    if (!this.smoothed) {
      this.smoothed = target;
    } else {
      const k = this.cfg.followAlpha;
      this.smoothed = {
        l: lerpPoint(this.smoothed.l, target.l, k),
        r: lerpPoint(this.smoothed.r, target.r, k),
      };
    }
    const rect = frameRect(this.smoothed.l, this.smoothed.r);
    this.liveRect = rectIsValid(rect, this.cfg.minWindowSize) ? rect : null;
  }

  // 捏合为边沿触发：只在"未捏合 → 捏合"的瞬间尝试复制一次
  // 复制后 armed=false，直到检测到松开才重新 armed
  updatePinch(rl, rr) {
    const c = this.cfg;
    if (!this.pinched) {
      if (rl < c.pinchOn && rr < c.pinchOn) {
        this.pinched = true;
        if (this.mode === 'window' && this.armed && this.liveRect) {
          const copy = { rect: this.liveRect, filter: this.liveFilter };
          this.liveFilter = (this.liveFilter + 1) % this.filterCount;
          this.armed = false;
          return copy;
        }
      }
    } else if (rl > c.pinchOff || rr > c.pinchOff) {
      this.pinched = false;
      this.armed = true;
    }
    return null;
  }
}
