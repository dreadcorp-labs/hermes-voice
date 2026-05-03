const { contextBridge, ipcRenderer } = require('electron');

const desktopState = {
  platform: process.platform,
  shortcut: '',
  shortcutRegistered: false,
  shortcutChoices: [],
};

contextBridge.exposeInMainWorld('hermesDesktop', {
  platform: desktopState.platform,
  getShortcut: () => desktopState.shortcut,
  getConfig: () => ipcRenderer.invoke('hermes-desktop:get-config'),
  setShortcut: (shortcut) => ipcRenderer.invoke('hermes-desktop:set-shortcut', shortcut),
});

ipcRenderer.on('hermes-desktop-config', (_event, payload) => {
  desktopState.shortcut = String(payload?.shortcut || '');
  desktopState.shortcutRegistered = Boolean(payload?.shortcutRegistered);
  desktopState.shortcutChoices = Array.isArray(payload?.shortcutChoices) ? payload.shortcutChoices : [];
  window.dispatchEvent(new CustomEvent('hermes:desktop-config', {
    detail: {
      shortcut: desktopState.shortcut,
      shortcutRegistered: desktopState.shortcutRegistered,
      shortcutChoices: desktopState.shortcutChoices,
      platform: payload?.platform || desktopState.platform,
    },
  }));
});

ipcRenderer.on('hermes-desktop-talkback', (_event, payload) => {
  window.dispatchEvent(new CustomEvent('hermes:desktop-talkback', {
    detail: {
      action: payload?.action || 'toggle',
      shortcut: payload?.shortcut || desktopState.shortcut,
    },
  }));
});
