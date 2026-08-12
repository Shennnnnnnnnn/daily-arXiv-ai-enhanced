const Auth = {
  async login(password, remember = true) {
    try {
      const result = await Api.request('/api/login', {
        method: 'POST',
        body: JSON.stringify({ password, remember })
      });
      Api.csrfToken = result.csrfToken || null;
      return true;
    } catch (error) {
      console.warn('Authentication failed:', error.message);
      return false;
    }
  },

  async isAuthenticated() {
    try {
      return (await Api.session()).authenticated === true;
    } catch (_error) {
      return false;
    }
  },

  async logout() {
    try {
      await Api.request('/api/logout', { method: 'POST' });
    } finally {
      Api.csrfToken = null;
      window.location.href = 'login.html';
    }
  },

  isPasswordEnabled() {
    return true;
  },

  async requireAuth() {
    if (!(await this.isAuthenticated())) {
      const currentPage = `${window.location.pathname.split('/').pop() || 'index.html'}${window.location.search}`;
      window.location.href = `login.html?redirect=${encodeURIComponent(currentPage)}`;
    }
  }
};
