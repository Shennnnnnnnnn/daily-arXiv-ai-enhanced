document.addEventListener('DOMContentLoaded', async () => {
  await initSettings();
  initEventListeners();
  loadJobStatus();
});

// 初始化设置，从localStorage加载已保存的设置
async function initSettings() {
  try {
    const settings = await Api.request('/api/settings');
    localStorage.setItem('preferredKeywords', JSON.stringify(settings.keywords || []));
    localStorage.setItem('preferredAuthors', JSON.stringify(settings.authors || []));
    loadKeywordPreferences();
    loadAuthorPreferences();
    setValue('aiApiBase', settings.base_url);
    setValue('aiModel', settings.model);
    setValue('paperAiAssistant', settings.paper_ai_assistant || 'kimi');
    setValue('paperAiCustomUrl', settings.paper_ai_custom_url || '');
    setValue('schedule', settings.schedule);
    setValue('categories', (settings.categories || []).join(', '));
    setValue('zoteroId', settings.zotero_id);
    setValue('zoteroTargetCollection', settings.zotero_target_collection || '我的文库/arxiv');
    setValue('zoteroEmbeddingModel', settings.zotero_embedding_model || 'text-embedding-3-small');
    setValue('zoteroIncludePaths', (settings.zotero_include_paths || []).join(', '));
    setValue('zoteroIgnorePaths', (settings.zotero_ignore_paths || []).join(', '));
    setValue('zoteroMaxPapers', settings.zotero_max_papers || 20);
    setValue('smtpHost', settings.smtp_host);
    setValue('smtpPort', settings.smtp_port || 587);
    setValue('smtpUser', settings.smtp_user);
    setValue('emailFrom', settings.email_from);
    setValue('emailTo', settings.email_to);
    document.getElementById('emailEnabled').checked = settings.email_enabled === true;
    document.getElementById('zoteroRecommendationEnabled').checked = settings.zotero_recommendation_enabled === true;
    applySecretState('aiApiKey', settings.ai_api_key_configured);
    applySecretState('zoteroKey', settings.zotero_key_configured);
    applySecretState('smtpPassword', settings.smtp_password_configured);
  } catch (error) {
    showNotification(`加载设置失败：${error.message}`, 'info');
  }
}

function setValue(id, value) {
  const element = document.getElementById(id);
  if (element && value !== undefined && value !== null) element.value = value;
}

function applySecretState(id, configured) {
  const element = document.getElementById(id);
  if (element && configured) element.placeholder = '已配置，留空则保持不变';
}

// 从localStorage加载关键词偏好
function loadKeywordPreferences() {
  const selectedKeywordsContainer = document.getElementById('selectedKeywords');
  selectedKeywordsContainer.innerHTML = '';
  
  // 获取保存的关键词，如果没有则使用默认关键词
  let savedKeywords = localStorage.getItem('preferredKeywords');
  let keywords = []; // 默认无关键词
  
  if (savedKeywords) {
    try {
      keywords = JSON.parse(savedKeywords);
    } catch (e) {
      console.error('解析保存的关键词失败:', e);
    }
  }
  
  // 显示保存的关键词
  if (keywords.length > 0) {
    keywords.forEach(keyword => {
      addKeywordTag(keyword);
    });
  } else {
    // 显示空标签消息
    showEmptyTagMessage();
  }
}

// 从localStorage加载作者偏好
function loadAuthorPreferences() {
  const selectedAuthorsContainer = document.getElementById('selectedAuthors');
  selectedAuthorsContainer.innerHTML = '';
  
  // 获取保存的作者，如果没有则为空数组
  let savedAuthors = localStorage.getItem('preferredAuthors');
  let authors = []; // 默认无作者
  
  if (savedAuthors) {
    try {
      authors = JSON.parse(savedAuthors);
    } catch (e) {
      console.error('解析保存的作者失败:', e);
    }
  }
  
  // 显示保存的作者
  if (authors.length > 0) {
    authors.forEach(author => {
      addAuthorTag(author);
    });
  } else {
    // 显示空标签消息
    showEmptyAuthorMessage();
  }
}

// 显示空标签消息
function showEmptyTagMessage() {
  const selectedKeywordsContainer = document.getElementById('selectedKeywords');
  const emptyMessage = document.createElement('div');
  emptyMessage.id = 'emptyTagMessage';
  emptyMessage.className = 'empty-tag-message';
  emptyMessage.textContent = '尚未添加关键词';
  selectedKeywordsContainer.appendChild(emptyMessage);
}

// 显示空作者标签消息
function showEmptyAuthorMessage() {
  const selectedAuthorsContainer = document.getElementById('selectedAuthors');
  const emptyMessage = document.createElement('div');
  emptyMessage.id = 'emptyAuthorMessage';
  emptyMessage.className = 'empty-tag-message';
  emptyMessage.textContent = '尚未添加作者';
  selectedAuthorsContainer.appendChild(emptyMessage);
}

// 隐藏空标签消息
function hideEmptyTagMessage() {
  const emptyMessage = document.getElementById('emptyTagMessage');
  if (emptyMessage) {
    emptyMessage.remove();
  }
}

// 隐藏空作者标签消息
function hideEmptyAuthorMessage() {
  const emptyMessage = document.getElementById('emptyAuthorMessage');
  if (emptyMessage) {
    emptyMessage.remove();
  }
}

// 添加关键词标签
function addKeywordTag(keyword) {
  const selectedKeywordsContainer = document.getElementById('selectedKeywords');
  
  // 移除空标签消息
  hideEmptyTagMessage();
  
  // 检查关键词是否已存在
  const existingTags = selectedKeywordsContainer.querySelectorAll('.category-button');
  for (let i = 0; i < existingTags.length; i++) {
    if (existingTags[i].textContent.trim().startsWith(keyword)) {
      // 已存在该关键词，添加闪烁动画提示用户
      existingTags[i].classList.add('tag-highlight');
      setTimeout(() => {
        existingTags[i].classList.remove('tag-highlight');
      }, 1000);
      return; // 关键词已存在，不添加
    }
  }
  
  // 创建新的关键词标签
  const tagElement = document.createElement('span');
  tagElement.className = 'category-button tag-appear';
  tagElement.innerHTML = `${keyword} <button class="remove-tag">×</button>`;
  
  // 添加删除按钮事件
  const removeButton = tagElement.querySelector('.remove-tag');
  removeButton.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    
    // 添加删除动画
    tagElement.classList.add('tag-disappear');
    
    // 动画结束后移除元素
    setTimeout(() => {
      tagElement.remove();
      
      // 如果没有标签了，显示空标签消息
      if (selectedKeywordsContainer.querySelectorAll('.category-button').length === 0) {
        showEmptyTagMessage();
      }
    }, 300);
  });
  
  selectedKeywordsContainer.appendChild(tagElement);
  
  // 添加出现动画后移除动画类
  setTimeout(() => {
    tagElement.classList.remove('tag-appear');
  }, 300);
}

// 添加作者标签
function addAuthorTag(author) {
  const selectedAuthorsContainer = document.getElementById('selectedAuthors');
  
  // 移除空标签消息
  hideEmptyAuthorMessage();
  
  // 检查作者是否已存在
  const existingTags = selectedAuthorsContainer.querySelectorAll('.category-button');
  for (let i = 0; i < existingTags.length; i++) {
    if (existingTags[i].textContent.trim().startsWith(author)) {
      // 已存在该作者，添加闪烁动画提示用户
      existingTags[i].classList.add('tag-highlight');
      setTimeout(() => {
        existingTags[i].classList.remove('tag-highlight');
      }, 1000);
      return; // 作者已存在，不添加
    }
  }
  
  // 创建新的作者标签
  const tagElement = document.createElement('span');
  tagElement.className = 'category-button tag-appear';
  tagElement.innerHTML = `${author} <button class="remove-tag">×</button>`;
  
  // 添加删除按钮事件
  const removeButton = tagElement.querySelector('.remove-tag');
  removeButton.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    
    // 添加删除动画
    tagElement.classList.add('tag-disappear');
    
    // 动画结束后移除元素
    setTimeout(() => {
      tagElement.remove();
      
      // 如果没有标签了，显示空标签消息
      if (selectedAuthorsContainer.querySelectorAll('.category-button').length === 0) {
        showEmptyAuthorMessage();
      }
    }, 300);
  });
  
  selectedAuthorsContainer.appendChild(tagElement);
  
  // 添加出现动画后移除动画类
  setTimeout(() => {
    tagElement.classList.remove('tag-appear');
  }, 300);
}

// 初始化事件监听器
function initEventListeners() {
  // 关键词添加按钮
  const addKeywordButton = document.getElementById('addKeyword');
  addKeywordButton.addEventListener('click', () => {
    const keywordInput = document.getElementById('keywordInput');
    const keyword = keywordInput.value.trim();

    if (keyword) {
      // 检测是否包含英文逗号，如果有则分割
      if (keyword.includes(',')) {
        const keywords = keyword.split(',').map(k => k.trim()).filter(k => k);
        keywords.forEach(k => addKeywordTag(k));
      } else {
        addKeywordTag(keyword);
      }
      keywordInput.value = '';
    }
  });

  // 关键词输入框回车事件
  const keywordInput = document.getElementById('keywordInput');
  keywordInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const keyword = keywordInput.value.trim();

      if (keyword) {
        // 检测是否包含英文逗号，如果有则分割
        if (keyword.includes(',')) {
          const keywords = keyword.split(',').map(k => k.trim()).filter(k => k);
          keywords.forEach(k => addKeywordTag(k));
        } else {
          addKeywordTag(keyword);
        }
        keywordInput.value = '';
      }
    }
  });

  // 作者添加按钮
  const addAuthorButton = document.getElementById('addAuthor');
  addAuthorButton.addEventListener('click', () => {
    const authorInput = document.getElementById('authorInput');
    const author = authorInput.value.trim();

    if (author) {
      // 检测是否包含英文逗号，如果有则分割
      if (author.includes(',')) {
        const authors = author.split(',').map(a => a.trim()).filter(a => a);
        authors.forEach(a => addAuthorTag(a));
      } else {
        addAuthorTag(author);
      }
      authorInput.value = '';
    }
  });

  // 作者输入框回车事件
  const authorInput = document.getElementById('authorInput');
  authorInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const author = authorInput.value.trim();

      if (author) {
        // 检测是否包含英文逗号，如果有则分割
        if (author.includes(',')) {
          const authors = author.split(',').map(a => a.trim()).filter(a => a);
          authors.forEach(a => addAuthorTag(a));
        } else {
          addAuthorTag(author);
        }
        authorInput.value = '';
      }
    }
  });

  // 关键词复制按钮
  const copyKeywordsButton = document.getElementById('copyKeywords');
  copyKeywordsButton.addEventListener('click', copyKeywords);

  // 作者复制按钮
  const copyAuthorsButton = document.getElementById('copyAuthors');
  copyAuthorsButton.addEventListener('click', copyAuthors);

  // 保存设置按钮
  const saveSettingsButton = document.getElementById('saveSettings');
  saveSettingsButton.addEventListener('click', saveSettings);

  document.getElementById('testEmail').addEventListener('click', testEmail);
  document.getElementById('testAi').addEventListener('click', testAi);
  document.getElementById('testZotero').addEventListener('click', testZotero);
  document.getElementById('runJob').addEventListener('click', runJob);
  document.getElementById('changePassword').addEventListener('click', changePassword);

  // 重置设置按钮
  const resetSettingsButton = document.getElementById('resetSettings');
  resetSettingsButton.addEventListener('click', resetSettings);
}

// 复制关键词到剪切板
function copyKeywords() {
  const keywordTags = document.getElementById('selectedKeywords').querySelectorAll('.category-button');
  const keywords = [];
  keywordTags.forEach(tag => {
    const keywordName = tag.textContent.trim().replace('×', '').trim();
    keywords.push(keywordName);
  });

  if (keywords.length === 0) {
    showNotification('暂无可复制的关键词', 'info');
    return;
  }

  const keywordsString = keywords.join(',');
  copyToClipboard(keywordsString, 'Keywords copied to clipboard!');
}

// 复制作者到剪切板
function copyAuthors() {
  const authorTags = document.getElementById('selectedAuthors').querySelectorAll('.category-button');
  const authors = [];
  authorTags.forEach(tag => {
    const authorName = tag.textContent.trim().replace('×', '').trim();
    authors.push(authorName);
  });

  if (authors.length === 0) {
    showNotification('暂无可复制的作者', 'info');
    return;
  }

  const authorsString = authors.join(',');
  copyToClipboard(authorsString, 'Authors copied to clipboard!');
}

// 复制到剪切板的通用函数
function copyToClipboard(text, successMessage) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      showNotification(successMessage, 'success');
    }).catch(err => {
      console.error('复制失败:', err);
      fallbackCopyText(text, successMessage);
    });
  } else {
    fallbackCopyText(text, successMessage);
  }
}

// 后备复制方法（用于不支持 clipboard API 的浏览器）
function fallbackCopyText(text, successMessage) {
  const textArea = document.createElement('textarea');
  textArea.value = text;
  textArea.style.position = 'fixed';
  textArea.style.left = '-9999px';
  document.body.appendChild(textArea);
  textArea.select();

  try {
    document.execCommand('copy');
    showNotification(successMessage, 'success');
  } catch (err) {
    console.error('复制失败:', err);
    showNotification('复制失败，请手动复制', 'info');
  }

  document.body.removeChild(textArea);
}

// 保存设置
async function saveSettings() {
  // 获取所有选中的关键词
  const keywordTags = document.getElementById('selectedKeywords').querySelectorAll('.category-button');
  const keywords = [];
  keywordTags.forEach(tag => {
    const keywordName = tag.textContent.trim().replace('×', '').trim();
    keywords.push(keywordName);
  });
  
  // 获取所有选中的作者
  const authorTags = document.getElementById('selectedAuthors').querySelectorAll('.category-button');
  const authors = [];
  authorTags.forEach(tag => {
    const authorName = tag.textContent.trim().replace('×', '').trim();
    authors.push(authorName);
  });
  
  const payload = {
    keywords,
    authors,
    ai_api_key: document.getElementById('aiApiKey').value,
    base_url: document.getElementById('aiApiBase').value.trim(),
    model: document.getElementById('aiModel').value.trim(),
    paper_ai_assistant: document.getElementById('paperAiAssistant').value,
    paper_ai_custom_url: document.getElementById('paperAiCustomUrl').value.trim(),
    schedule: document.getElementById('schedule').value,
    categories: document.getElementById('categories').value.split(',').map(item => item.trim()).filter(Boolean),
    zotero_id: document.getElementById('zoteroId').value.trim(),
    zotero_key: document.getElementById('zoteroKey').value,
    zotero_target_collection: document.getElementById('zoteroTargetCollection').value.trim(),
    zotero_recommendation_enabled: document.getElementById('zoteroRecommendationEnabled').checked,
    zotero_embedding_model: document.getElementById('zoteroEmbeddingModel').value.trim(),
    zotero_include_paths: splitList('zoteroIncludePaths'),
    zotero_ignore_paths: splitList('zoteroIgnorePaths'),
    zotero_max_papers: Number(document.getElementById('zoteroMaxPapers').value || 20),
    email_enabled: document.getElementById('emailEnabled').checked,
    smtp_host: document.getElementById('smtpHost').value.trim(),
    smtp_port: Number(document.getElementById('smtpPort').value || 587),
    smtp_user: document.getElementById('smtpUser').value.trim(),
    smtp_password: document.getElementById('smtpPassword').value,
    email_from: document.getElementById('emailFrom').value.trim(),
    email_to: document.getElementById('emailTo').value.trim()
  };
  try {
    await Api.request('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
    localStorage.setItem('preferredKeywords', JSON.stringify(keywords));
    localStorage.setItem('preferredAuthors', JSON.stringify(authors));
    ['aiApiKey', 'zoteroKey', 'smtpPassword'].forEach(id => { document.getElementById(id).value = ''; });
    showNotification('设置已保存', 'success');
  } catch (error) {
    showNotification(`保存失败：${error.message}`, 'info');
  }
}

function splitList(id) {
  return document.getElementById(id).value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean);
}

async function runConfigTest(buttonId, statusId, path, payload, successMessage) {
  const button = document.getElementById(buttonId);
  const status = document.getElementById(statusId);
  button.disabled = true;
  status.textContent = '测试中...';
  status.className = 'test-status pending';
  try {
    const result = await Api.request(path, { method: 'POST', body: JSON.stringify(payload) });
    const detail = result.model || result.library_name || result.item_count;
    status.textContent = detail ? `${successMessage}：${detail}` : successMessage;
    status.className = 'test-status success';
    showNotification(successMessage, 'success');
  } catch (error) {
    status.textContent = error.message;
    status.className = 'test-status error';
    showNotification(`测试失败：${error.message}`, 'info');
  } finally {
    button.disabled = false;
  }
}

function testAi() {
  return runConfigTest('testAi', 'aiTestStatus', '/api/ai/test', {
    ai_api_key: document.getElementById('aiApiKey').value,
    base_url: document.getElementById('aiApiBase').value.trim(),
    model: document.getElementById('aiModel').value.trim()
  }, 'AI 配置正常');
}

function testZotero() {
  return runConfigTest('testZotero', 'zoteroTestStatus', '/api/zotero/test', {
    zotero_id: document.getElementById('zoteroId').value.trim(),
    zotero_key: document.getElementById('zoteroKey').value
  }, 'Zotero 配置正常');
}

async function testEmail() {
  try {
    await saveSettings();
    await Api.request('/api/email/test', { method: 'POST', body: '{}' });
    showNotification('测试邮件已发送', 'success');
  } catch (error) {
    showNotification(`邮件发送失败：${error.message}`, 'info');
  }
}

async function loadJobStatus() {
  try {
    const status = await Api.request('/api/jobs/status');
    const lastResult = status.last_exit_code === null || status.last_exit_code === undefined
      ? '尚未运行'
      : (status.last_exit_code === 0 ? '最近运行成功' : `最近运行失败（${status.last_exit_code}）`);
    document.getElementById('jobStatus').textContent = status.running ? '正在运行' : lastResult;
  } catch (error) {
    document.getElementById('jobStatus').textContent = error.message;
  }
}

async function runJob() {
  const button = document.getElementById('runJob');
  button.disabled = true;
  try {
    await Api.request('/api/jobs/run', { method: 'POST', body: '{}' });
    showNotification('任务已启动', 'success');
    setTimeout(loadJobStatus, 1200);
  } catch (error) {
    showNotification(`启动失败：${error.message}`, 'info');
  } finally {
    button.disabled = false;
  }
}

async function changePassword() {
  const currentPassword = document.getElementById('currentPassword');
  const newPassword = document.getElementById('newPassword');
  if (newPassword.value.length < 12) {
    showNotification('新密码至少需要 12 个字符', 'info');
    return;
  }
  try {
    await Api.request('/api/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword.value, new_password: newPassword.value })
    });
    currentPassword.value = '';
    newPassword.value = '';
    showNotification('管理员密码已更新', 'success');
  } catch (error) {
    showNotification(`密码更新失败：${error.message}`, 'info');
  }
}

// 重置设置
function resetSettings() {
  // 重置关键词
  const selectedKeywordsContainer = document.getElementById('selectedKeywords');
  selectedKeywordsContainer.innerHTML = '';
  
  // 重置作者
  const selectedAuthorsContainer = document.getElementById('selectedAuthors');
  selectedAuthorsContainer.innerHTML = '';
  
  // 显示空标签消息
  showEmptyTagMessage();
  showEmptyAuthorMessage();
  
  // 显示重置成功提示
  showNotification('Settings reset to default!', 'info');
}

// 显示通知
function showNotification(message, type = 'success') {
  // 检查是否已存在通知元素
  let notification = document.querySelector('.settings-notification');
  
  if (!notification) {
    // 创建通知元素
    notification = document.createElement('div');
    notification.className = 'settings-notification';
    document.body.appendChild(notification);
  }
  
  // 根据类型设置图标
  let icon = '';
  let bgColor = 'var(--primary-color)';
  
  if (type === 'success') {
    icon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z" fill="currentColor"/></svg>';
  } else if (type === 'info') {
    icon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 15c-.55 0-1-.45-1-1v-4c0-.55.45-1 1-1s1 .45 1 1v4c0 .55-.45 1-1 1zm1-8h-2V7h2v2z" fill="currentColor"/></svg>';
    bgColor = '#3b82f6';
  }
  
  // 设置通知内容和样式
  notification.innerHTML = `${icon}<span>${message}</span>`;
  notification.style.display = 'flex';
  notification.style.alignItems = 'center';
  notification.style.gap = '8px';
  notification.style.position = 'fixed';
  notification.style.bottom = '20px';
  notification.style.right = '20px';
  notification.style.backgroundColor = bgColor;
  notification.style.color = 'white';
  notification.style.padding = '12px 20px';
  notification.style.borderRadius = 'var(--radius-sm)';
  notification.style.boxShadow = 'var(--shadow-md)';
  notification.style.zIndex = '1000';
  notification.style.opacity = '0';
  notification.style.transform = 'translateY(20px)';
  notification.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
  
  // 显示通知
  setTimeout(() => {
    notification.style.opacity = '1';
    notification.style.transform = 'translateY(0)';
  }, 10);
  
  // 3秒后隐藏通知
  setTimeout(() => {
    notification.style.opacity = '0';
    notification.style.transform = 'translateY(20px)';
    
    // 动画结束后移除元素
    setTimeout(() => {
      if (notification.parentNode) {
        notification.parentNode.removeChild(notification);
      }
    }, 300);
  }, 3000);
}
