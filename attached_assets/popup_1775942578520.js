// OP Extension Popup Script

document.addEventListener('DOMContentLoaded', async () => {
  const STORAGE_KEY = 'op_detected_keys';
  
  // Elements
  const totalCountEl = document.getElementById('totalCount');
  const pkCountEl = document.getElementById('pkCount');
  const clientCountEl = document.getElementById('clientCount');
  const secretCountEl = document.getElementById('secretCount');
  const setupCountEl = document.getElementById('setupCount');
  const pmCountEl = document.getElementById('pmCount');
  const keysListEl = document.getElementById('keysList');
  const enabledToggle = document.getElementById('enabledToggle');
  const statusBar = document.getElementById('statusBar');
  const statusText = document.getElementById('statusText');
  const clearBtn = document.getElementById('clearBtn');
  const exportBtn = document.getElementById('exportBtn');
  
  // Load data
  async function loadData() {
    const result = await chrome.storage.local.get([
      STORAGE_KEY, 
      'enabled', 
      'totalDetected'
    ]);
    
    const keys = result[STORAGE_KEY] || [];
    const enabled = result.enabled !== false;
    const total = result.totalDetected || 0;
    
    // Update toggle
    enabledToggle.checked = enabled;
    updateStatus(enabled);
    
    // Update counts
    const pkKeys = keys.filter(k => k.type === 'pk_live');
    const clientKeys = keys.filter(k => k.type === 'client_key');
    const secretKeys = keys.filter(k => k.type === 'client_secret');
    const setupKeys = keys.filter(k => k.type === 'setup_secret');
    const pmKeys = keys.filter(k => k.type === 'payment_method');
    
    totalCountEl.textContent = total;
    pkCountEl.textContent = pkKeys.length;
    if (clientCountEl) clientCountEl.textContent = clientKeys.length;
    secretCountEl.textContent = secretKeys.length;
    if (setupCountEl) setupCountEl.textContent = setupKeys.length;
    if (pmCountEl) pmCountEl.textContent = pmKeys.length;
    
    // Update badge
    chrome.action.setBadgeText({ text: total > 0 ? total.toString() : '' });
    
    // Render keys list
    renderKeys(keys);
  }
  
  // Render keys list
  function renderKeys(keys) {
    if (keys.length === 0) {
      keysListEl.innerHTML = `
        <div class="empty-state">
          <p>No keys detected yet</p>
          <span>Browse websites with Stripe payments</span>
        </div>
      `;
      return;
    }
    
    // Show last 10 keys
    const recentKeys = keys.slice(-10).reverse();
    
    keysListEl.innerHTML = recentKeys.map(key => `
      <div class="key-item" data-value="${escapeHtml(key.value)}">
        <span class="key-type ${key.type}">${key.type.replace('_', ' ')}</span>
        <div class="key-value">${truncateKey(key.value)}</div>
        <div class="key-source">${escapeHtml(key.source)} • ${formatTime(key.timestamp)}</div>
      </div>
    `).join('');
    
    // Add click handlers
    document.querySelectorAll('.key-item').forEach(item => {
      item.addEventListener('click', () => {
        const value = item.dataset.value;
        navigator.clipboard.writeText(value).then(() => {
          showToast('Key copied to clipboard!');
        });
      });
    });
  }
  
  // Update status display
  function updateStatus(enabled) {
    const dot = statusBar.querySelector('.status-dot');
    if (enabled) {
      dot.classList.add('active');
      statusText.textContent = 'Monitoring Active';
      statusBar.style.opacity = '1';
    } else {
      dot.classList.remove('active');
      statusText.textContent = 'Monitoring Paused';
      statusBar.style.opacity = '0.6';
    }
  }
  
  // Toggle enabled state
  enabledToggle.addEventListener('change', async () => {
    const enabled = enabledToggle.checked;
    await chrome.storage.local.set({ enabled });
    updateStatus(enabled);
    showToast(enabled ? 'Monitoring activated' : 'Monitoring paused');
  });
  
  // Clear all keys
  clearBtn.addEventListener('click', async () => {
    if (confirm('Are you sure you want to clear all detected keys?')) {
      await chrome.storage.local.set({
        [STORAGE_KEY]: [],
        totalDetected: 0,
        lastDetected: null
      });
      chrome.action.setBadgeText({ text: '' });
      await loadData();
      showToast('All keys cleared');
    }
  });
  
  // Export keys as JSON
  exportBtn.addEventListener('click', async () => {
    const result = await chrome.storage.local.get([STORAGE_KEY, 'telegramBot']);
    const keys = result[STORAGE_KEY] || [];
    
    const exportData = {
      extension: 'OP Extension',
      telegramBot: result.telegramBot || '@op_limitedbot',
      exportDate: new Date().toISOString(),
      totalKeys: keys.length,
      keys: keys
    };
    
    const blob = new Blob([JSON.stringify(exportData, null, 2)], {
      type: 'application/json'
    });
    
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `op-keys-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
    
    showToast('Keys exported!');
  });
  
  // Listen for real-time updates
  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === 'KEYS_UPDATED') {
      loadData();
    }
  });
  
  // Helper functions
  function truncateKey(key) {
    if (!key) return '';
    if (key.length <= 35) return escapeHtml(key);
    return escapeHtml(key.substring(0, 20)) + '...' + escapeHtml(key.substring(key.length - 12));
  }
  
  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
  
  function formatTime(timestamp) {
    if (!timestamp) return 'unknown';
    const date = new Date(timestamp);
    const now = new Date();
    const diff = now - date;
    
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
    return `${Math.floor(diff / 86400000)}d ago`;
  }
  
  function showToast(message) {
    // Remove existing toast
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();
    
    // Create new toast
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.body.appendChild(toast);
    
    // Show
    setTimeout(() => toast.classList.add('show'), 10);
    
    // Hide after 2 seconds
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 300);
    }, 2000);
  }
  
  // Initial load
  loadData();
});
