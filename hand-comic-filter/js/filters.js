// 漫画滤镜列表。下标即滤镜编号，必须与 shaders.js 里 main() 的分支顺序一致。
export const FILTERS = [
  { id: 'color-comic', name: '彩色漫画' },
  { id: 'manga-screentone', name: '日漫网点' },
  { id: 'pop-art', name: '美式波普' },
  { id: 'ink-line', name: '黑白线稿' },
  { id: 'noir-red', name: '黑白留红' },
  { id: 'cel-shade', name: '赛璐璐' },
];

// 普通模式三个区域的默认滤镜：Thumb→Index / Index→Middle / Middle→Pinky
export const DEFAULT_REGION_FILTERS = [0, 1, 2];
