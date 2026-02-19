// background.js - 保留原有事件 + 新增消息监听
chrome.runtime.onInstalled.addListener(() => {
  console.log('Cookie Grabber installed');
});

// content script 请求cookie时的处理
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'getCookies' && sender.tab) {
    const url = sender.tab.url;
    // 普通cookie + partitioned cookie 双查合并
    chrome.cookies.getAll({url}, cookies => {
      console.log('[CookieGrabber] normal cookies:', cookies.map(c => c.name));
      chrome.cookies.getAll({url, partitionKey: {topLevelSite: url.match(/^https?:\/\/[^/]+/)[0]}}, pCookies => {
        console.log('[CookieGrabber] partitioned cookies:', pCookies.map(c => c.name));
        const map = {};
        cookies.forEach(c => map[c.name] = c.value);
        pCookies.forEach(c => map[c.name] = c.value);
        const str = Object.entries(map).map(([k,v]) => k + '=' + v).join('; ');
        sendResponse({cookies: str});
      });
    });
    return true; // 异步响应
  }
});