const SIDEBAR_KEY = 'krull-sidebar-open';

export function setupSidebar() {
  const savedState = localStorage.getItem(SIDEBAR_KEY);
  if (savedState === 'false') {
    document.body.classList.add('sidebar-closed');
  }

  document.getElementById('sidebar-close')?.addEventListener('click', () => {
    document.body.classList.add('sidebar-closed');
    localStorage.setItem(SIDEBAR_KEY, 'false');
    // Trigger map resize after transition
    setTimeout(() => window.dispatchEvent(new Event('resize')), 350);
  });

  document.getElementById('sidebar-open')?.addEventListener('click', () => {
    document.body.classList.remove('sidebar-closed');
    localStorage.setItem(SIDEBAR_KEY, 'true');
    setTimeout(() => window.dispatchEvent(new Event('resize')), 350);
  });

  // Collapsible sections
  document.querySelectorAll('.section-header[data-toggle]').forEach((header) => {
    header.addEventListener('click', () => {
      header.classList.toggle('collapsed');
      const body = header.nextElementSibling as HTMLElement;
      if (body) body.classList.toggle('collapsed');
    });
  });
}
