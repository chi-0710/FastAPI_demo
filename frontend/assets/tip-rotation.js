// 调度与页面分开：多个暂停原因互不覆盖；恢复时给予完整阅读时间。
export function createTipRotation({count, onChange, delay = 5000,
  schedule = setTimeout, cancel = clearTimeout}) {
  let index = 0;
  let timer = null;
  let disposed = false;
  const pauses = new Set();

  function restart() {
    if (timer !== null) cancel(timer);
    timer = null;
    if (!disposed && !pauses.size && count > 1) {
      timer = schedule(() => select((index + 1) % count), delay);
    }
  }
  function select(next, manual = false) {
    if (disposed || !Number.isInteger(next) || next < 0 || next >= count) return;
    index = next;
    onChange(index, manual);
    restart();
  }
  function pause(reason, enabled) {
    if (enabled) pauses.add(reason);
    else pauses.delete(reason);
    restart();
  }
  return {
    select, pause,
    start: () => select(0),
    dispose() { disposed = true; restart(); },
  };
}
