document.addEventListener('DOMContentLoaded', () => {  
  // 当刷新按钮被点击时，获取当前页面的 cookies  
  document.getElementById('refresh').addEventListener('click', fetchCookies);  
  // 加载时显示当前页面的 cookies  
  fetchCookies();  
});  

// 从 cookies 存储中获取当前页面的 cookies  
function fetchCookies() {  
  // 获取当前活动的标签页  
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {  
    const activeTab = tabs[0]; // 获取活动标签页  
    const currentUrl = activeTab.url; // 当前页面的 URL  
    console.log("当前活动的 URL:", currentUrl);  

    // 双查: 普通 + partitioned
    const origin = currentUrl.match(/^https?:\/\/[^\/]+/)[0];
    chrome.cookies.getAll({ url: currentUrl }, (cookies) => {
      chrome.cookies.getAll({ url: currentUrl, partitionKey: { topLevelSite: origin } }, (pCookies) => {
        const map = {};
        cookies.forEach(c => map[c.name] = c.value);
        pCookies.forEach(c => map[c.name] = c.value);
        const allCookies = Object.entries(map);

        const cookiesDisplay = document.getElementById('cookiesDisplay');
        cookiesDisplay.innerHTML = '';

        if (allCookies.length === 0) {
          cookiesDisplay.innerHTML = '<li>No available cookies</li>';
        } else {
          let cookiesString = '';
          allCookies.forEach(([name, value]) => {
            const li = document.createElement('li');
            li.textContent = `${name}: ${value}`;
            cookiesDisplay.appendChild(li);
            cookiesString += `${name}=${value}; `;
          });
          console.log('cookies:', allCookies.length);
          copyCookiesToClipboard(cookiesString.trim());
        }
      });
    });  
  });  
}  

// 将 cookies 复制到剪贴板  
function copyCookiesToClipboard(cookiesString) {  
  // 使用 Clipboard API 复制到剪贴板  
  navigator.clipboard.writeText(cookiesString)  
    .then(() => {  
      console.log("Cookies copied to clipboard:", cookiesString); 
    })  
    .catch(err => {  
      console.error("Unable to copy to clipboard:", err);  
    });  
}