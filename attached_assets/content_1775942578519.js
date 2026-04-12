// OP Extension Content Script
// Injected into all pages to detect Stripe keys

(function() {
  'use strict';
  
  console.log('[OP Extension] Content script loaded');
  
  // Patterns for Stripe keys - MORE FLEXIBLE
  const PATTERNS = {
    pk_live: /pk_live_[a-zA-Z0-9]{24,}/gi,
    client_key: /client_key[_=:\s]*([a-zA-Z0-9_\-]{10,})/gi,
    // Payment Intent client_secret - matches pi_xxxxx_secret_xxxxx
    client_secret: /pi_[a-zA-Z0-9\-_]{10,}_secret_[a-zA-Z0-9\-_]{10,}/gi,
    // Setup Intent secrets
    setup_secret: /seti_[a-zA-Z0-9\-_]{10,}_secret_[a-zA-Z0-9\-_]{10,}/gi,
    stripe_pk: /stripe[_-]?pk[_=:\s]*([a-zA-Z0-9_\-]{24,})/gi,
    // Payment Method IDs (pm_xxxxx)
    payment_method: /pm_[a-zA-Z0-9]{24,}/gi
  };
  
  const detectedKeys = new Set();
  
  // Inject script into page context for deeper access
  function injectScript() {
    const script = document.createElement('script');
    script.src = chrome.runtime.getURL('injected.js');
    script.onload = () => script.remove();
    (document.head || document.documentElement).appendChild(script);
  }
  
  // Listen for messages from injected script
  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    if (event.data?.type === 'OP_STRIPE_KEYS_FOUND') {
      reportKeys(event.data.keys, 'injected');
    }
  });
  
  // Scan text for keys
  function scanText(text, source) {
    if (!text || typeof text !== 'string') return [];
    
    // Limit text length to prevent performance issues (check first 50KB)
    var maxLength = 50000;
    if (text.length > maxLength) {
      text = text.substring(0, maxLength);
    }
    
    const found = [];
    
    try {
      // Check for pk_live
      const pkMatches = text.matchAll(PATTERNS.pk_live);
      for (const match of pkMatches) {
        const key = match[0];
        if (!detectedKeys.has(key)) {
          detectedKeys.add(key);
          console.log('[OP Content] Found pk_live:', key.substring(0, 30) + '...');
          found.push({
            type: 'pk_live',
            value: key,
            source: source
          });
        }
      }
      
      // Check for client_key
      const clientMatches = text.matchAll(PATTERNS.client_key);
      for (const match of clientMatches) {
        const key = match[1] || match[0];
        if (!detectedKeys.has(key)) {
          detectedKeys.add(key);
          found.push({
            type: 'client_key',
            value: key,
            source: source
          });
        }
      }
      
      // Check for client_secret (Payment Intent format)
      const secretMatches = text.matchAll(PATTERNS.client_secret);
      for (const match of secretMatches) {
        const key = match[0];
        if (!detectedKeys.has(key)) {
          detectedKeys.add(key);
          console.log('[OP Content] FOUND CLIENT_SECRET:', key);
          found.push({
            type: 'client_secret',
            value: key,
            source: source
          });
        }
      }
      
      // Check for setup_secret
      const setupMatches = text.matchAll(PATTERNS.setup_secret);
      for (const match of setupMatches) {
        const key = match[0];
        if (!detectedKeys.has(key)) {
          detectedKeys.add(key);
          console.log('[OP Content] FOUND SETUP_SECRET:', key);
          found.push({
            type: 'setup_secret',
            value: key,
            source: source
          });
        }
      }
      
      // Check for payment_method IDs (pm_xxxxx)
      const pmMatches = text.matchAll(PATTERNS.payment_method);
      for (const match of pmMatches) {
        const key = match[0];
        if (!detectedKeys.has(key)) {
          detectedKeys.add(key);
          console.log('[OP Content] FOUND PAYMENT_METHOD:', key);
          found.push({
            type: 'payment_method',
            value: key,
            source: source
          });
        }
      }
    } catch (e) {
      // Ignore regex errors
    }
    
    if (found.length > 0) {
      console.log('[OP Content] Total found in scan:', found.length, 'keys');
    }
    
    return found;
  }
  
  // Report found keys to background
  function reportKeys(keys, source) {
    if (keys.length === 0) return;
    
    console.log('[OP Content] Reporting keys:', keys);
    
    chrome.runtime.sendMessage({
      type: 'STRIPE_KEYS_DETECTED',
      data: keys.map(k => ({
        ...k,
        pageUrl: window.location.href,
        domain: window.location.hostname,
        timestamp: new Date().toISOString()
      }))
    }).catch(err => console.log('[OP] Message failed:', err));
  }
  
  // Scan the entire document
  function scanDocument() {
    const html = document.documentElement.innerHTML;
    console.log('[OP Content] Scanning document, length:', html.length);
    const found = scanText(html, 'page_html');
    if (found.length > 0) {
      console.log('[OP Content] Found in document:', found);
    }
    reportKeys(found, 'document');
  }
  
  // Scan scripts
  function scanScripts() {
    const scripts = document.querySelectorAll('script');
    console.log('[OP Content] Scanning', scripts.length, 'scripts');
    scripts.forEach((script, index) => {
      const content = script.textContent || script.src;
      if (content && (content.includes('stripe') || content.includes('pi_'))) {
        console.log('[OP Content] Script', index, 'contains stripe/pi_');
      }
      const found = scanText(content, 'script');
      if (found.length > 0) {
        console.log('[OP Content] Found in script', index + ':', found);
      }
      reportKeys(found, 'script');
    });
  }
  
  // Scan network requests via fetch interception
  function interceptFetch() {
    const originalFetch = window.fetch;
    window.fetch = function(...args) {
      // Scan URL and body before sending (non-blocking)
      setTimeout(function() {
        try {
          var url = args[0];
          var options = args[1] || {};
          
          // Check URL for Stripe patterns
          if (typeof url === 'string') {
            var decodedUrl;
            try {
              decodedUrl = decodeURIComponent(url);
            } catch (e) {
              decodedUrl = url;
            }
            if (decodedUrl.includes('pi_') || decodedUrl.includes('stripe')) {
              console.log('[OP Content] Fetch URL:', decodedUrl.substring(0, 200));
            }
            var found = scanText(decodedUrl, 'fetch_url');
            if (found.length > 0) console.log('[OP Content] Found in fetch URL:', found);
            reportKeys(found, 'fetch');
          }
          
          // Check request body
          if (options.body) {
            try {
              var body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
              if (body.includes('pi_') || body.includes('secret')) {
                console.log('[OP Content] Fetch body contains pi_/secret');
              }
              var found = scanText(body, 'fetch_body');
              if (found.length > 0) console.log('[OP Content] Found in fetch body:', found);
              reportKeys(found, 'fetch');
            } catch (e) {}
          }
        } catch (e) {}
      }, 0);
      
      // Call original fetch
      return originalFetch.apply(this, args).then(function(response) {
        // Scan response asynchronously without blocking
        setTimeout(function() {
          try {
            var clone = response.clone();
            clone.text().then(function(text) {
              if (text.includes('pi_') || text.includes('secret')) {
                console.log('[OP Content] Response contains pi_/secret');
              }
              var found = scanText(text, 'fetch_response');
              if (found.length > 0) console.log('[OP Content] Found in response:', found);
              reportKeys(found, 'fetch');
            }).catch(function() {});
          } catch (e) {}
        }, 0);
        
        return response;
      }).catch(function(error) {
        // Re-throw errors
        throw error;
      });
    };
  }
  
  // Intercept XMLHttpRequest - minimal overhead
  function interceptXHR() {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    
    XMLHttpRequest.prototype.open = function(method, url, ...args) {
      this._opUrl = url;
      // Defer scanning to not block
      var self = this;
      setTimeout(function() {
        try {
          if (typeof url === 'string') {
            var decodedUrl;
            try {
              decodedUrl = decodeURIComponent(url);
            } catch (e) {
              decodedUrl = url;
            }
            if (decodedUrl.includes('pi_')) {
              console.log('[OP Content] XHR URL with pi_:', decodedUrl.substring(0, 200));
            }
            var found = scanText(decodedUrl, 'xhr_url');
            reportKeys(found, 'xhr');
          }
        } catch (e) {}
      }, 0);
      
      return originalOpen.apply(this, [method, url, ...args]);
    };
    
    XMLHttpRequest.prototype.send = function(body) {
      var self = this;
      // Defer body scanning
      setTimeout(function() {
        try {
          if (body) {
            var bodyStr = typeof body === 'string' ? body : String(body);
            if (bodyStr.includes('pi_') || bodyStr.includes('secret')) {
              console.log('[OP Content] XHR body with pi_/secret');
            }
            var found = scanText(bodyStr, 'xhr_body');
            reportKeys(found, 'xhr');
          }
        } catch (e) {}
      }, 0);
      
      // Add response listener
      this.addEventListener('load', function() {
        setTimeout(function() {
          try {
            if (self.responseText && (self.responseText.includes('pi_') || self.responseText.includes('secret'))) {
              console.log('[OP Content] XHR response with pi_/secret');
            }
            var found = scanText(self.responseText, 'xhr_response');
            reportKeys(found, 'xhr');
          } catch (e) {}
        }, 0);
      });
      
      return originalSend.apply(this, [body]);
    };
  }
  
  // Watch for dynamic content changes
  function observeMutations() {
    const observer = new MutationObserver((mutations) => {
      mutations.forEach(mutation => {
        mutation.addedNodes.forEach(node => {
          if (node.nodeType === Node.ELEMENT_NODE) {
            const html = node.outerHTML || node.textContent;
            const found = scanText(html, 'mutation');
            reportKeys(found, 'mutation');
          }
        });
      });
    });
    
    observer.observe(document.body || document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['src', 'href', 'data-key', 'data-stripe', 'data-secret', 'value']
    });
  }
  
  // Check meta tags and data attributes
  function scanMetaAndData() {
    // Meta tags
    const metaTags = document.querySelectorAll('meta');
    metaTags.forEach(meta => {
      const content = meta.getAttribute('content') || '';
      const found = scanText(content, 'meta');
      reportKeys(found, 'meta');
    });
    
    // Data attributes - broader search
    const allElements = document.querySelectorAll('*');
    allElements.forEach(el => {
      Array.from(el.attributes).forEach(attr => {
        const attrName = attr.name.toLowerCase();
        const attrValue = attr.value || '';
        if (attrName.includes('stripe') || 
            attrName.includes('key') || 
            attrName.includes('pk') ||
            attrName.includes('secret') ||
            attrName.includes('pi_') ||
            attrValue.includes('pi_') ||
            attrValue.includes('secret_')) {
          const found = scanText(attrValue, 'data_attribute');
          if (found.length > 0) {
            console.log('[OP Content] Found in attribute', attr.name + ':', found);
          }
          reportKeys(found, 'data_attribute');
        }
      });
    });
    
    // Check input values (for hidden inputs with secrets)
    const inputs = document.querySelectorAll('input[type="hidden"], input[name*="secret"], input[name*="client"]');
    inputs.forEach(input => {
      const value = input.value || '';
      if (value) {
        const found = scanText(value, 'input_value');
        if (found.length > 0) {
          console.log('[OP Content] Found in input:', found);
        }
        reportKeys(found, 'input_value');
      }
    });
  }
  
  // Initialize
  function init() {
    console.log('[OP Content] Initializing on', window.location.href);
    
    // Inject script after a short delay to not interfere with page load
    setTimeout(function() {
      injectScript();
    }, 100);
    
    // Set up interceptors
    interceptFetch();
    interceptXHR();
    
    // Delay initial scans to let page fully load
    setTimeout(function() {
      console.log('[OP Content] Starting scans...');
      scanDocument();
      scanScripts();
      scanMetaAndData();
      if (document.body) {
        observeMutations();
      }
    }, 2000);
  }
  
  // Run when DOM is fully loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  
})();
