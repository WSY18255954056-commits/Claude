// WebGL 渲染器：上传摄像头帧，绘制滤镜四边形
import { VERTEX_SHADER, FRAGMENT_SHADER } from './shaders.js';

const FLOATS_PER_VERTEX = 4; // x, y, u, v
const TRIANGLE_ORDER = [0, 1, 2, 0, 2, 3];

export class Renderer {
  constructor(canvas) {
    const gl = canvas.getContext('webgl', { alpha: false, antialias: false });
    if (!gl) throw new Error('当前浏览器不可用 WebGL');
    this.gl = gl;
    this.canvas = canvas;

    this.program = linkProgram(gl, VERTEX_SHADER, FRAGMENT_SHADER);
    this.aPosition = gl.getAttribLocation(this.program, 'a_position');
    this.aUv = gl.getAttribLocation(this.program, 'a_uv');
    this.uTexel = gl.getUniformLocation(this.program, 'u_texel');
    this.uFilter = gl.getUniformLocation(this.program, 'u_filter');

    gl.useProgram(this.program);
    gl.uniform1i(gl.getUniformLocation(this.program, 'u_texture'), 0);
    gl.enableVertexAttribArray(this.aPosition);
    gl.enableVertexAttribArray(this.aUv);

    this.buffer = gl.createBuffer();
    // 摄像头纹理在每帧上传时更新尺寸
    this.video = { texture: createTexture(gl), width: 0, height: 0 };
  }

  // 把当前摄像头帧上传为纹理；画布尺寸跟随视频分辨率
  uploadVideo(video) {
    const { gl } = this;
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.video.texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
    this.video.width = w;
    this.video.height = h;
    return this.video;
  }

  // 从一个 2D 画布创建静态纹理（冻结窗口的裁剪图）
  createTexture(source) {
    const { gl } = this;
    const texture = createTexture(gl);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    return { texture, width: source.width, height: source.height };
  }

  deleteTexture(tex) {
    this.gl.deleteTexture(tex.texture);
  }

  clear() {
    const { gl } = this;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
  }

  // corners: 四个顶点的归一化视频坐标（未镜像，左上角为原点）
  // uvs:     与 corners 一一对应的纹理坐标
  // filter:  -1 原始画面，否则为滤镜编号
  drawQuad(tex, corners, uvs, filter) {
    const { gl } = this;
    const verts = new Float32Array(TRIANGLE_ORDER.length * FLOATS_PER_VERTEX);
    TRIANGLE_ORDER.forEach((i, n) => {
      const [x, y] = corners[i];
      const [u, v] = uvs[i];
      // 屏幕镜像显示（自拍视角）：x 取反
      verts.set([(1 - x) * 2 - 1, 1 - y * 2, u, v], n * FLOATS_PER_VERTEX);
    });

    gl.useProgram(this.program);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, verts, gl.DYNAMIC_DRAW);
    const stride = FLOATS_PER_VERTEX * 4;
    gl.vertexAttribPointer(this.aPosition, 2, gl.FLOAT, false, stride, 0);
    gl.vertexAttribPointer(this.aUv, 2, gl.FLOAT, false, stride, 2 * 4);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, tex.texture);
    gl.uniform2f(this.uTexel, 1 / tex.width, 1 / tex.height);
    gl.uniform1f(this.uFilter, filter);
    gl.drawArrays(gl.TRIANGLES, 0, TRIANGLE_ORDER.length);
  }
}

function createTexture(gl) {
  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return texture;
}

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(`着色器编译失败：${gl.getShaderInfoLog(shader)}`);
  }
  return shader;
}

function linkProgram(gl, vertexSource, fragmentSource) {
  const program = gl.createProgram();
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, vertexSource));
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, fragmentSource));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(`着色器链接失败：${gl.getProgramInfoLog(program)}`);
  }
  return program;
}
