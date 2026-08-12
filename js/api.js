const Api = {
  csrfToken: null,

  async request(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json';
    }
    if (!['GET', 'HEAD'].includes(method)) {
      if (!this.csrfToken) {
        await this.session();
      }
      if (this.csrfToken) headers['X-CSRF-Token'] = this.csrfToken;
    }

    const response = await fetch(path, {
      ...options,
      method,
      headers,
      credentials: 'same-origin'
    });
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json')
      ? await response.json()
      : { error: await response.text() };

    if (!response.ok) {
      if (response.status === 401 && !location.pathname.endsWith('/login.html')) {
        const target = `${location.pathname.split('/').pop() || 'index.html'}${location.search}`;
        location.href = `login.html?redirect=${encodeURIComponent(target)}`;
      }
      throw new Error(payload.error || `Request failed (${response.status})`);
    }
    return payload;
  },

  async session() {
    const session = await this.request('/api/session');
    this.csrfToken = session.csrfToken || null;
    return session;
  }
};
