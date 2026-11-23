import {createTipRotation} from './tip-rotation.js';

// 新增或修改提示只需编辑这个数组，指示条会自动同步生成。
export const learningTips = [
  {title: '从一个想法开始', text: '把复杂的事情，拆成\n每一个清晰的小步骤。'},
  {title: '先跑通，再理解', text: '亲手调用一次接口，\n再沿着请求阅读代码。'},
  {title: '一次只学一个点', text: '给自己二十分钟，\n专注解决一个小问题。'},
  {title: '让错误成为线索', text: '看看状态码和日志，\n试着解释失败的原因。'},
  {title: '用输出巩固所学', text: '写下今天的新发现，\n用自己的话讲清楚。'},
];

export function initLearningTips(card) {
  if (!card) return () => {};
  const slides = card.querySelector('.tip-slides');
  const indicators = card.querySelector('.note-line');
  const announcement = card.querySelector('.tip-announcement');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const compact = matchMedia('(max-width: 960px)');
  const listeners = new AbortController();
  const rotation = createTipRotation({count: learningTips.length, onChange: render});

  slides.replaceChildren();
  indicators.replaceChildren();
  learningTips.forEach((tip, index) => {
    const slide = document.createElement('div');
    slide.className = 'tip-slide';
    slide.id = `learning-tip-${index}`;
    slide.setAttribute('role', 'group');
    slide.setAttribute('aria-roledescription', '幻灯片');
    slide.setAttribute('aria-label', `${index + 1} / ${learningTips.length}`);
    const title = document.createElement('strong'); title.textContent = tip.title;
    const text = document.createElement('p'); text.textContent = tip.text;
    slide.append(title, text); slides.append(slide);
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-label', `第 ${index + 1} 条：${tip.title}`);
    button.setAttribute('aria-controls', slide.id);
    button.title = tip.title;
    button.addEventListener('click', () => rotation.select(index, true), {signal: listeners.signal});
    indicators.append(button);
  });

  function render(index, manual) {
    [...slides.children].forEach((slide, i) => {
      const active = i === index;
      slide.classList.toggle('active', active);
      slide.setAttribute('aria-hidden', String(!active));
      slide.inert = !active;
      const button = indicators.children[i];
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    // 自动播放不打断读屏；手动切换才播报新内容。
    if (manual) announcement.textContent = `${learningTips[index].title}。${learningTips[index].text}`;
  }
  function syncPreferences() {
    rotation.pause('reduced-motion', reduced.matches);
    rotation.pause('compact-layout', compact.matches);
  }
  function listen(target, name, callback) {
    target.addEventListener(name, callback, {signal: listeners.signal});
  }
  listen(card, 'mouseenter', () => rotation.pause('hover', true));
  listen(card, 'mouseleave', () => rotation.pause('hover', false));
  listen(card, 'focusin', () => rotation.pause('focus', true));
  listen(card, 'focusout', event => {
    if (!card.contains(event.relatedTarget)) rotation.pause('focus', false);
  });
  listen(document, 'visibilitychange', () => rotation.pause('hidden-page', document.hidden));
  listen(window, 'pagehide', () => rotation.pause('pagehide', true));
  listen(window, 'pageshow', () => rotation.pause('pagehide', false));
  listen(reduced, 'change', syncPreferences);
  listen(compact, 'change', syncPreferences);
  rotation.pause('hidden-page', document.hidden);
  rotation.pause('hover', card.matches(':hover'));
  rotation.pause('focus', card.contains(document.activeElement));
  syncPreferences();
  rotation.start();
  return () => { listeners.abort(); rotation.dispose(); };
}
