const MAX_WINDOW_WIDTH = 1280;
const MAX_WINDOW_HEIGHT = 800;
const DEFAULT_MIN_WIDTH = 960;
const DEFAULT_MIN_HEIGHT = 640;
const WORK_AREA_INSET = 24;

function getWindowGeometry(workArea) {
  const availableWidth = Math.max(1, workArea.width - WORK_AREA_INSET * 2);
  const availableHeight = Math.max(1, workArea.height - WORK_AREA_INSET * 2);
  const width = Math.min(MAX_WINDOW_WIDTH, availableWidth);
  const height = Math.min(MAX_WINDOW_HEIGHT, availableHeight);

  return {
    width,
    height,
    minWidth: Math.min(DEFAULT_MIN_WIDTH, width),
    minHeight: Math.min(DEFAULT_MIN_HEIGHT, height),
    x: workArea.x + Math.max(0, Math.round((workArea.width - width) / 2)),
    y: workArea.y + Math.max(0, Math.round((workArea.height - height) / 2)),
  };
}

module.exports = { getWindowGeometry };
