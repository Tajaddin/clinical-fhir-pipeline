// Where the dashboard finds the API. '/api' is proxied by nginx (Compose) and by `ng serve`; a deployment
// that serves the dashboard from static hosting overwrites this file with the API's URL.
window.__CFP_API_BASE__ = '/api';
