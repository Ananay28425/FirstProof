/* Only refresh an active experiment page. No inline scripts, CDN, or secrets. */
const active = document.querySelector('[data-autorefresh]');
if (active) setTimeout(() => window.location.reload(), Number(active.dataset.autorefresh) * 1000);
