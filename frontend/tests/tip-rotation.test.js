import test from 'node:test';
import assert from 'node:assert/strict';
import {createTipRotation} from '../assets/tip-rotation.js';

function fixture() {
  let pending;
  let last;
  const rotation = createTipRotation({count:5,
    onChange: (index, manual) => {last = {index, manual};},
    schedule: (callback, delay) => {pending = {callback, delay}; return 1;},
    cancel: () => {pending = null;},
  });
  rotation.start();
  return {rotation, tick: () => pending?.callback(), current: () => last, timer: () => pending};
}

test('每五秒前进一项并循环，手动选择后重置计时', () => {
  const f = fixture();
  assert.equal(f.timer().delay, 5000);
  for (let i=0;i<5;i++) f.tick();
  assert.equal(f.current().index, 0);
  f.rotation.select(3, true);
  assert.deepEqual(f.current(), {index:3, manual:true});
  f.tick(); assert.equal(f.current().index, 4);
});

test('悬停和键盘聚焦同时暂停，必须全部解除才恢复', () => {
  const f = fixture();
  f.rotation.pause('hover', true);
  f.rotation.pause('focus', true);
  f.rotation.pause('hover', false);
  assert.equal(f.timer(), null);
  f.rotation.select(2, true);
  assert.equal(f.current().index, 2);
  assert.equal(f.timer(), null);
  f.rotation.pause('focus', false);
  assert.equal(f.timer().delay, 5000);
  f.tick(); assert.equal(f.current().index, 3);
});

test('减少动态效果不会被移开鼠标覆盖，销毁清理计时器', () => {
  const f = fixture();
  f.rotation.pause('reduced-motion', true);
  f.rotation.pause('hover', false);
  assert.equal(f.timer(), null);
  f.rotation.pause('reduced-motion', false);
  assert.ok(f.timer());
  f.rotation.dispose();
  assert.equal(f.timer(), null);
  f.rotation.select(4, true);
  assert.equal(f.current().index, 0);
});
