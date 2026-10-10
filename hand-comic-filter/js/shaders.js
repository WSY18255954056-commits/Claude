// WebGL 1 着色器。所有滤镜共用一个程序：
// 每个区域（普通模式四边形 / live 窗口 / 冻结窗口）都是两个三角形，
// 顶点携带纹理坐标，片元着色器按 u_filter 选择滤镜。

export const VERTEX_SHADER = `
attribute vec2 a_position;
attribute vec2 a_uv;
varying vec2 v_uv;

void main() {
  v_uv = a_uv;
  gl_Position = vec4(a_position, 0.0, 1.0);
}
`;

export const FRAGMENT_SHADER = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

uniform sampler2D u_texture;
uniform vec2 u_texel;   // 1 / 源纹理的像素尺寸，用于邻域采样
uniform float u_filter; // -1 原始画面；0..5 对应 FILTERS 的下标
varying vec2 v_uv;

float luma(vec3 c) {
  return dot(c, vec3(0.299, 0.587, 0.114));
}

vec3 src() {
  return texture2D(u_texture, v_uv).rgb;
}

vec3 srcAt(vec2 offset) {
  return texture2D(u_texture, v_uv + offset * u_texel).rgb;
}

// Sobel 边缘强度。k 为采样半径（像素），k 越大线条越粗。纯黑白跳变约为 1.0
float edge(float k) {
  float tl = luma(srcAt(vec2(-k, -k)));
  float tc = luma(srcAt(vec2(0.0, -k)));
  float tr = luma(srcAt(vec2(k, -k)));
  float ml = luma(srcAt(vec2(-k, 0.0)));
  float mr = luma(srcAt(vec2(k, 0.0)));
  float bl = luma(srcAt(vec2(-k, k)));
  float bc = luma(srcAt(vec2(0.0, k)));
  float br = luma(srcAt(vec2(k, k)));
  float gx = (tr + 2.0 * mr + br) - (tl + 2.0 * ml + bl);
  float gy = (bl + 2.0 * bc + br) - (tl + 2.0 * tc + tr);
  return length(vec2(gx, gy)) * 0.25;
}

// 旋转网点。ink 为墨量 0~1，返回 1.0 表示该像素落在墨点内
float dots(vec2 p, float ink, float cell, float angle) {
  if (ink < 0.01) return 0.0;
  float c = cos(angle);
  float s = sin(angle);
  vec2 q = vec2(c * p.x - s * p.y, s * p.x + c * p.y) / cell;
  vec2 f = fract(q) - 0.5;
  float r = sqrt(clamp(ink, 0.0, 1.0)) * 0.72;
  return 1.0 - smoothstep(r - 0.05, r + 0.05, length(f));
}

// 彩色漫画：提高饱和 + 色阶分离 + 粗黑轮廓
vec3 colorComic() {
  vec3 c = src();
  c = mix(vec3(luma(c)), c, 1.35);
  c = floor(c * 5.0 + 0.5) / 5.0;
  float line = smoothstep(0.15, 0.3, edge(2.0));
  return mix(c, vec3(0.07, 0.05, 0.09), line);
}

// 日漫网点：黑白，暗部用网点表现，最暗处实心，带轮廓
vec3 mangaScreentone() {
  float l = luma(src());
  float ink = clamp((0.92 - l) / 0.7, 0.0, 1.0);
  float screen = dots(gl_FragCoord.xy, ink, 7.0, 0.785);
  float solid = step(0.88, ink);
  float line = smoothstep(0.15, 0.35, edge(2.0));
  float shade = max(max(screen, solid), line);
  return mix(vec3(0.97, 0.96, 0.93), vec3(0.05, 0.04, 0.05), shade);
}

// 美式波普：高饱和 + 4 级色阶 + Ben-Day 网点 + 黑轮廓
vec3 popArt() {
  vec3 c = src();
  c = mix(vec3(luma(c)), c, 1.8);
  c = floor(c * 4.0 + 0.5) / 4.0;
  float tone = luma(c);
  float benDay = dots(gl_FragCoord.xy, 1.0 - tone, 6.0, 0.26);
  c *= 1.0 - 0.3 * benDay;
  float line = smoothstep(0.15, 0.35, edge(2.0));
  return mix(c, vec3(0.04, 0.03, 0.05), line);
}

// 黑白线稿：白纸上的黑色细线
vec3 inkLine() {
  float line = smoothstep(0.12, 0.3, edge(1.0));
  return mix(vec3(0.98, 0.97, 0.93), vec3(0.08, 0.07, 0.08), line);
}

// 黑白留红（Sin City 风格）：其余为高对比黑白，红色保留
vec3 noirRed() {
  vec3 c = src();
  float bw = smoothstep(0.25, 0.75, luma(c));
  float red = clamp((c.r - max(c.g, c.b) - 0.12) * 5.0, 0.0, 1.0);
  return mix(vec3(bw), vec3(min(c.r * 1.2, 1.0), c.g * 0.3, c.b * 0.3), red);
}

// 赛璐璐：三段明暗 + 暗部偏冷色 + 细轮廓
vec3 celShade() {
  vec3 c = src();
  float l = luma(c);
  vec3 base = floor(c * 3.0 + 0.5) / 3.0;
  float band = l < 0.35 ? 0.55 : (l < 0.7 ? 0.8 : 1.0);
  vec3 tone = base * band;
  tone = mix(tone, tone * vec3(0.6, 0.55, 0.85), step(l, 0.35));
  float line = smoothstep(0.15, 0.3, edge(1.0));
  return mix(tone, vec3(0.06, 0.05, 0.1), line);
}

void main() {
  vec3 col;
  if (u_filter < -0.5) {
    col = src();
  } else if (u_filter < 0.5) {
    col = colorComic();
  } else if (u_filter < 1.5) {
    col = mangaScreentone();
  } else if (u_filter < 2.5) {
    col = popArt();
  } else if (u_filter < 3.5) {
    col = inkLine();
  } else if (u_filter < 4.5) {
    col = noirRed();
  } else {
    col = celShade();
  }
  gl_FragColor = vec4(col, 1.0);
}
`;
