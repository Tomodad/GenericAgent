// content.js - MutationObserver 监听触发元素
const TRIGGER_ID = '__ljqcg__';

const obs = new MutationObserver(muts => {
  for (const m of muts) {
    for (const node of m.addedNodes) {
      if (node.nodeType === 1 && node.id === TRIGGER_ID) {
        chrome.runtime.sendMessage({type: 'getCookies'}, res => {
          if (res && res.cookies) node.textContent = res.cookies;
          else node.textContent = '__cg_error__';
        });
        return;
      }
    }
  }
});

obs.observe(document.documentElement, {childList: true, subtree: true});
