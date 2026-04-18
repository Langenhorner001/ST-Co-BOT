// OP Extension Injected Script
// Runs in page context to access window.Stripe and other page-level objects

(function() {
  'use strict';
  
  console.log('[OP Extension] Injected script running in page context');
  
  // Pattern for client secrets - MORE FLEXIBLE
  const SECRET_PATTERN = /pi_[a-zA-Z0-9\-_]{10,}_secret_[a-zA-Z0-9\-_]{10,}/gi;
  const SETUP_SECRET_PATTERN = /seti_[a-zA-Z0-9\-_]{10,}_secret_[a-zA-Z0-9\-_]{10,}/gi;
  const PK_PATTERN = /pk_live_[a-zA-Z0-9]{24,}/gi;
  // Payment Method IDs (pm_xxxxx)
  const PM_PATTERN = /pm_[a-zA-Z0-9]{24,}/gi;
  
  const detected = new Set();
  
  // Helper to report keys
  function report(type, value, source) {
    if (detected.has(value)) return;
    detected.add(value);
    
    console.log('[OP Injected] Reporting', type + ':', value.substring(0, 50) + '...');
    
    window.postMessage({
      type: 'OP_STRIPE_KEYS_FOUND',
      keys: [{
        type: type,
        value: value,
        source: source,
        timestamp: new Date().toISOString()
      }]
    }, '*');
  }
  
  // Scan text for secrets
  function scanText(text, source) {
    if (!text || typeof text !== 'string') return;
    
    // Limit text length to prevent performance issues (check first 50KB)
    var maxLength = 50000;
    if (text.length > maxLength) {
      text = text.substring(0, maxLength);
    }
    
    try {
      // Check for client_secret
      var secretMatches = text.matchAll(SECRET_PATTERN);
      for (var match of secretMatches) {
        report('client_secret', match[0], source);
      }
      
      // Check for setup secrets
      var setupMatches = text.matchAll(SETUP_SECRET_PATTERN);
      for (var match of setupMatches) {
        report('setup_secret', match[0], source);
      }
      
      // Check for pk_live
      var pkMatches = text.matchAll(PK_PATTERN);
      for (var match of pkMatches) {
        report('pk_live', match[0], source);
      }
      
      // Check for payment_method IDs (pm_xxxxx)
      var pmMatches = text.matchAll(PM_PATTERN);
      for (var match of pmMatches) {
        report('payment_method', match[0], source);
      }
    } catch (e) {
      // Ignore regex errors
    }
  }
  
  // Check window.Stripe - passive check only
  function checkStripeObject() {
    if (window.Stripe) {
      console.log('[OP] Stripe object found on window');
      
      // Try to get the key from Stripe instance
      try {
        if (window.Stripe.stripeAccountId) {
          report('stripe_account', window.Stripe.stripeAccountId, 'window.Stripe');
        }
      } catch (e) {}
    }
  }
  
  // Check for Stripe in various places
  function deepScan() {
    console.log('[OP Injected] Starting deep scan...');
    
    // Check global variables
    const globals = Object.keys(window);
    globals.forEach(key => {
      try {
        const val = window[key];
        if (typeof val === 'string') {
          scanText(val, `window.${key}`);
        } else if (typeof val === 'object' && val !== null) {
          // Check object properties recursively (shallow)
          const jsonStr = JSON.stringify(val);
          if (jsonStr && (jsonStr.includes('pi_') || jsonStr.includes('secret'))) {
            scanText(jsonStr, `window.${key}`);
          }
        }
      } catch (e) {}
    });
    
    // Check localStorage
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        const val = localStorage.getItem(key);
        
        if (val) {
          if (val.startsWith('pk_live_')) {
            report('pk_live', val, `localStorage.${key}`);
          }
          if (val.includes('client_key')) {
            const match = val.match(/client_key[_=:\s]*([a-zA-Z0-9_\-]{10,})/);
            if (match) {
              report('client_key', match[1], `localStorage.${key}`);
            }
          }
          scanText(val, `localStorage.${key}`);
        }
      }
    } catch (e) {}
    
    // Check sessionStorage
    try {
      for (let i = 0; i < sessionStorage.length; i++) {
        const key = sessionStorage.key(i);
        const val = sessionStorage.getItem(key);
        
        if (val) {
          if (val.startsWith('pk_live_')) {
            report('pk_live', val, `sessionStorage.${key}`);
          }
          scanText(val, `sessionStorage.${key}`);
        }
      }
    } catch (e) {}
    
    // Scan document body text
    scanText(document.body?.innerText || '', 'document.body.innerText');
  }
  
  // Monitor all fetch requests - use non-async to preserve behavior
  const originalFetch = window.fetch;
  window.fetch = function(...args) {
    // Scan URL and body before sending
    try {
      const url = args[0];
      const options = args[1] || {};
      
      // Check URL
      if (typeof url === 'string') {
        try {
          scanText(decodeURIComponent(url), 'fetch_url');
        } catch (e) {
          scanText(url, 'fetch_url');
        }
      }
      
      // Check request body
      if (options.body) {
        try {
          const body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
          scanText(body, 'fetch_body');
        } catch (e) {
          // Ignore body parsing errors
        }
      }
    } catch (e) {
      // Ignore interception errors
    }
    
    // Call original fetch and handle response
    return originalFetch.apply(this, args).then(function(response) {
      // Clone and check response asynchronously without blocking
      try {
        var clone = response.clone();
        clone.text().then(function(text) {
          if (text.includes('pi_') || text.includes('secret')) {
            console.log('[OP Injected] Response contains pi_/secret');
          }
          scanText(text, 'fetch_response');
        }).catch(function() {});
      } catch (e) {}
      
      return response;
    }).catch(function(error) {
      // Re-throw errors to not break page functionality
      throw error;
    });
  };
  // Preserve original fetch properties
  Object.defineProperty(window.fetch, 'name', { value: originalFetch.name });
  if (originalFetch.prototype) window.fetch.prototype = originalFetch.prototype;
  
  // Monitor XMLHttpRequest - minimal overhead
  const originalXHROpen = XMLHttpRequest.prototype.open;
  const originalXHRSend = XMLHttpRequest.prototype.send;
  
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    this._opUrl = url;
    // Defer scanning to not block
    var self = this;
    setTimeout(function() {
      try {
        if (typeof url === 'string') {
          try {
            scanText(decodeURIComponent(url), 'xhr_url');
          } catch (e) {
            scanText(url, 'xhr_url');
          }
        }
      } catch (e) {}
    }, 0);
    return originalXHROpen.apply(this, [method, url, ...args]);
  };
  
  XMLHttpRequest.prototype.send = function(body) {
    var self = this;
    // Defer body scanning
    setTimeout(function() {
      try {
        if (body) {
          var bodyStr = typeof body === 'string' ? body : String(body);
          scanText(bodyStr, 'xhr_body');
        }
      } catch (e) {}
    }, 0);
    
    // Add response listener
    this.addEventListener('load', function() {
      setTimeout(function() {
        try {
          scanText(self.responseText, 'xhr_response');
        } catch (e) {}
      }, 0);
    });
    
    return originalXHRSend.apply(this, [body]);
  };
  
  // Run checks after page is more loaded
  setTimeout(function() {
    checkStripeObject();
    deepScan();
  }, 500);
  
  // Periodic check for dynamically loaded Stripe (less frequent)
  setInterval(function() {
    checkStripeObject();
  }, 5000);
  
  console.log('[OP Injected] Script initialized');
  
})();
