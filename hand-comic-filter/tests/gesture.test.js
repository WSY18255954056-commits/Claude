import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  LM,
  GestureController,
  frameRect,
  pairHands,
  pinchRatio,
  rectIsValid,
  regionQuads,
} from '../js/gesture.js';

// 构造一只手。手掌尺度（手腕到食指根部）固定为 0.2，
// 拇指尖与食指尖水平相距 gap，因此 pinchRatio = gap / 0.2，
// 两指中点位于 (cx, cy - 0.1)。
function makeHand({ cx, cy, gap, tips = {} }) {
  const lm = Array.from({ length: 21 }, () => ({ x: cx, y: cy }));
  lm[LM.WRIST] = { x: cx, y: cy + 0.2 };
  lm[LM.INDEX_MCP] = { x: cx, y: cy };
  lm[LM.THUMB_TIP] = { x: cx - gap / 2, y: cy - 0.1 };
  lm[LM.INDEX_TIP] = { x: cx + gap / 2, y: cy - 0.1 };
  for (const [idx, p] of Object.entries(tips)) lm[idx] = p;
  return lm;
}

const LEFT_POS = { cx: 0.3, cy: 0.5 }; // 中点 (0.3, 0.4)
const RIGHT_POS = { cx: 0.7, cy: 0.6 }; // 中点 (0.7, 0.5)

// 中点由浮点运算得到，比较时允许极小误差
function assertRectClose(actual, expected) {
  for (const k of ['x0', 'y0', 'x1', 'y1']) {
    assert.ok(Math.abs(actual[k] - expected[k]) < 1e-9, `${k}: ${actual[k]} ≠ ${expected[k]}`);
  }
}

const pairWithGap = (gap, tipsLeft = {}, tipsRight = {}) => ({
  left: makeHand({ ...LEFT_POS, gap, tips: tipsLeft }),
  right: makeHand({ ...RIGHT_POS, gap, tips: tipsRight }),
});

const GATHERED = 0.1; // 比例 0.5：聚拢，但未捏合
const PINCHED = 0.02; // 比例 0.1：捏合
const OPEN = 0.4; // 比例 2.0：张开

function newController() {
  return new GestureController({ filterCount: 6, initialFilter: 0 });
}

// 让控制器从普通模式进入新模式（聚拢持续 250ms）
function enterWindow(c, t0 = 0) {
  c.update(t0, pairWithGap(GATHERED));
  const out = c.update(t0 + 260, pairWithGap(GATHERED));
  assert.equal(out.enteredWindow, true);
  assert.equal(c.mode, 'window');
  return t0 + 260;
}

test('pinchRatio 用手掌尺度归一化', () => {
  assert.ok(Math.abs(pinchRatio(makeHand({ ...LEFT_POS, gap: 0.02 })) - 0.1) < 1e-9);
  assert.ok(Math.abs(pinchRatio(makeHand({ ...LEFT_POS, gap: 0.4 })) - 2) < 1e-9);
});

test('pairHands 优先按 Left/Right 标签配对', () => {
  const L = makeHand({ ...LEFT_POS, gap: 0.1 });
  const R = makeHand({ ...RIGHT_POS, gap: 0.1 });
  const pair = pairHands([
    { label: 'Right', landmarks: R },
    { label: 'Left', landmarks: L },
  ]);
  assert.equal(pair.left, L);
  assert.equal(pair.right, R);
});

test('pairHands 标签缺失时按手腕 x 坐标兜底', () => {
  const L = makeHand({ ...LEFT_POS, gap: 0.1 });
  const R = makeHand({ ...RIGHT_POS, gap: 0.1 });
  const pair = pairHands([
    { label: '', landmarks: R },
    { label: '', landmarks: L },
  ]);
  assert.equal(pair.left, L);
  assert.equal(pair.right, R);
});

test('pairHands 不足两只手时返回 null', () => {
  assert.equal(pairHands([{ label: 'Left', landmarks: makeHand({ ...LEFT_POS, gap: 0.1 }) }]), null);
  assert.equal(pairHands([]), null);
});

test('regionQuads 生成三个区域，顶点取自两只手的对应指尖', () => {
  const pair = pairWithGap(0.1);
  const regions = regionQuads(pair);
  assert.deepEqual(
    regions.map((r) => r.id),
    ['thumb-index', 'index-middle', 'middle-pinky'],
  );
  const { left, right } = pair;
  assert.deepEqual(regions[0].quad, [
    [left[LM.THUMB_TIP].x, left[LM.THUMB_TIP].y],
    [left[LM.INDEX_TIP].x, left[LM.INDEX_TIP].y],
    [right[LM.INDEX_TIP].x, right[LM.INDEX_TIP].y],
    [right[LM.THUMB_TIP].x, right[LM.THUMB_TIP].y],
  ]);
  assert.deepEqual(regions[2].quad[0], [left[LM.MIDDLE_TIP].x, left[LM.MIDDLE_TIP].y]);
  assert.deepEqual(regions[2].quad[1], [left[LM.PINKY_TIP].x, left[LM.PINKY_TIP].y]);
});

test('frameRect 以两个中点为对角顶点，且水平对齐时无面积', () => {
  const r = frameRect({ x: 0.7, y: 0.5 }, { x: 0.3, y: 0.4 });
  assert.deepEqual(r, { x0: 0.3, y0: 0.4, x1: 0.7, y1: 0.5 });
  assert.equal(rectIsValid(frameRect({ x: 0.3, y: 0.4 }, { x: 0.7, y: 0.4 }), 0.03), false);
  assert.equal(rectIsValid(r, 0.03), true);
});

test('新模式需要聚拢持续 enterHoldMs，中途张开则重新计时', () => {
  const c = newController();
  assert.equal(c.update(0, pairWithGap(GATHERED)).enteredWindow, false);
  assert.equal(c.update(200, pairWithGap(GATHERED)).enteredWindow, false);
  assert.equal(c.mode, 'normal');
  c.update(210, pairWithGap(OPEN));
  c.update(220, pairWithGap(GATHERED));
  assert.equal(c.update(400, pairWithGap(GATHERED)).enteredWindow, false);
  assert.equal(c.update(480, pairWithGap(GATHERED)).enteredWindow, true);
});

test('双手张开时不会进入新模式', () => {
  const c = newController();
  for (let t = 0; t <= 2000; t += 50) c.update(t, pairWithGap(OPEN));
  assert.equal(c.mode, 'normal');
});

test('新模式窗口的两个顶点来自两只手的拇指/食指中点', () => {
  const c = newController();
  let t = enterWindow(c);
  c.update(t + 33, pairWithGap(GATHERED));
  assertRectClose(c.liveRect, { x0: 0.3, y0: 0.4, x1: 0.7, y1: 0.5 });
});

test('新模式只使用拇指和食指：中指、无名指、小指的变化不影响窗口与模式', () => {
  const c = newController();
  let t = enterWindow(c);
  const before = c.liveRect;
  const farTips = {
    [LM.MIDDLE_TIP]: { x: 0.9, y: 0.05 },
    16: { x: 0.05, y: 0.95 },
    [LM.PINKY_TIP]: { x: 0.5, y: 0.99 },
  };
  c.update(t + 33, pairWithGap(GATHERED, farTips, farTips));
  assert.deepEqual(c.liveRect, before);
  assert.equal(c.mode, 'window');
});

test('一次捏合只复制一次，松开后才能再次复制，且每次复制切换滤镜', () => {
  const c = newController();
  let t = enterWindow(c);

  const first = c.update(t + 33, pairWithGap(PINCHED));
  assert.equal(first.copy?.filter, 0);
  assertRectClose(first.copy.rect, { x0: 0.3, y0: 0.4, x1: 0.7, y1: 0.5 });
  assert.equal(c.liveFilter, 1);

  // 持续捏合，不重复复制
  assert.equal(c.update(t + 66, pairWithGap(PINCHED)).copy, null);
  assert.equal(c.update(t + 99, pairWithGap(PINCHED)).copy, null);

  // 松开（张开到超过 pinchOff）后，再次捏合才能复制
  c.update(t + 100, pairWithGap(GATHERED));
  const second = c.update(t + 133, pairWithGap(PINCHED));
  assert.equal(second.copy?.filter, 1);
  assert.equal(c.liveFilter, 2);
});

test('滤镜按完整列表无限循环', () => {
  const c = newController();
  let t = enterWindow(c);
  const seen = [];
  for (let i = 0; i < 7; i++) {
    t += 100;
    c.update(t, pairWithGap(GATHERED));
    seen.push(c.update(t + 33, pairWithGap(PINCHED)).copy.filter);
    t += 33;
  }
  assert.deepEqual(seen, [0, 1, 2, 3, 4, 5, 0]);
});

test('进入新模式时已处于捏合，不会立即复制', () => {
  const c = newController();
  c.update(0, pairWithGap(PINCHED)); // 普通模式下先捏合
  let t = 0;
  c.update(t += 100, pairWithGap(PINCHED));
  c.update(t += 200, pairWithGap(PINCHED)); // 进入新模式
  assert.equal(c.mode, 'window');
  assert.equal(c.update(t += 33, pairWithGap(PINCHED)).copy, null);
  // 松开后再捏合才复制
  c.update(t += 33, pairWithGap(GATHERED));
  assert.equal(c.update(t += 33, pairWithGap(PINCHED)).copy?.filter, 0);
});

test('窗口过小时不显示，也不能复制', () => {
  const c = newController();
  let t = enterWindow(c);
  // 两个中点水平对齐 → 窗口高度为 0。平滑需要若干帧才收敛到零高度（聚拢状态，未捏合）
  const flat = {
    left: makeHand({ cx: 0.3, cy: 0.5, gap: 0.1 }),
    right: makeHand({ cx: 0.7, cy: 0.5, gap: 0.1 }),
  };
  for (let i = 0; i < 12; i++) c.update((t += 33), flat);
  assert.equal(c.liveRect, null);

  // 此时捏合不应复制
  const flatPinched = {
    left: makeHand({ cx: 0.3, cy: 0.5, gap: 0.02 }),
    right: makeHand({ cx: 0.7, cy: 0.5, gap: 0.02 }),
  };
  const out = c.update((t += 33), flatPinched);
  assert.equal(out.copy, null);
  assert.equal(c.liveFilter, 0);
});

test('手丢失超过 lostHoldMs 后退出新模式，窗口立即隐藏', () => {
  const c = newController();
  let t = enterWindow(c);
  assert.equal(c.update(t + 10, null).exitedWindow, false);
  assert.equal(c.liveRect, null);
  assert.equal(c.mode, 'window');
  assert.equal(c.update(t + 10 + 400, null).exitedWindow, true);
  assert.equal(c.mode, 'normal');
});

test('双手明显张开持续 exitHoldMs 后退出新模式', () => {
  const c = newController();
  let t = enterWindow(c);
  c.update(t += 100, pairWithGap(OPEN));
  assert.equal(c.mode, 'window');
  assert.equal(c.update(t += 300, pairWithGap(OPEN)).exitedWindow, true);
  assert.equal(c.mode, 'normal');
});

test('窗口滤镜在退出后保留，再次进入新模式继续轮换', () => {
  const c = newController();
  let t = enterWindow(c);
  c.update(t += 33, pairWithGap(GATHERED));
  c.update(t += 33, pairWithGap(PINCHED)); // 复制一次，filter 0 → 1
  c.update(t += 100, pairWithGap(OPEN));
  c.update(t += 400, pairWithGap(OPEN)); // 退出
  assert.equal(c.mode, 'normal');
  assert.equal(c.liveFilter, 1);
});
