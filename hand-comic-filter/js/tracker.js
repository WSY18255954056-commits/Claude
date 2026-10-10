// 摄像头与 MediaPipe Hand Landmarker 的封装
import { FilesetResolver, HandLandmarker } from 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs';

const WASM_ROOT = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm';
const MODEL_URL =
  'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task';

export async function startCamera(video) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: {
      facingMode: 'user',
      width: { ideal: 1280 },
      height: { ideal: 720 },
    },
  });
  video.srcObject = stream;
  await video.play();
}

// 优先 GPU，失败则回退 CPU
export async function createHandLandmarker() {
  const fileset = await FilesetResolver.forVisionTasks(WASM_ROOT);
  const common = {
    runningMode: 'VIDEO',
    numHands: 2,
    minHandDetectionConfidence: 0.5,
    minHandPresenceConfidence: 0.5,
    minTrackingConfidence: 0.5,
  };
  for (const delegate of ['GPU', 'CPU']) {
    try {
      return await HandLandmarker.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: MODEL_URL, delegate },
        ...common,
      });
    } catch (err) {
      console.warn(`HandLandmarker（${delegate}）初始化失败`, err);
    }
  }
  throw new Error('手部识别模型加载失败，请检查网络连接');
}

// 把 MediaPipe 的结果整理成 [{ landmarks, label }]
export function toHands(result) {
  return (result.landmarks || []).map((landmarks, i) => ({
    landmarks,
    label: result.handedness?.[i]?.[0]?.categoryName ?? '',
  }));
}
