const { app, BrowserWindow, Menu, globalShortcut, ipcMain, shell, session } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_URL = 'http://127.0.0.1:8765/?desktop=1';
const APP_URL = process.env.HERMES_LIVEKIT_DESKTOP_URL || DEFAULT_URL;
const DESKTOP_BUILD = '20260502-mic-toggle';
const DEFAULT_TALKBACK_SHORTCUT = 'F13';
const TALKBACK_SHORTCUT_CHOICES = new Set([
  'F13',
  'F14',
  'F15',
  'F8',
  'F9',
  'nummult',
  '`',
  'CommandOrControl+Shift+Space',
]);

let mainWindow = null;
let talkbackShortcut = process.env.HERMES_TALKBACK_SHORTCUT || DEFAULT_TALKBACK_SHORTCUT;
let shortcutRegistered = false;

function configPath() {
  return path.join(app.getPath('userData'), 'config.json');
}

function loadConfig() {
  try {
    const raw = fs.readFileSync(configPath(), 'utf8');
    const parsed = JSON.parse(raw);
    const shortcut = String(parsed?.talkbackShortcut || '').trim();
    if (shortcut && TALKBACK_SHORTCUT_CHOICES.has(shortcut)) {
      talkbackShortcut = shortcut;
    }
  } catch (_) {}
}

function saveConfig() {
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.writeFileSync(configPath(), JSON.stringify({ talkbackShortcut }, null, 2));
}

function desktopConfig() {
  return {
    shortcut: talkbackShortcut,
    shortcutRegistered,
    shortcutChoices: [
      { id: 'F13', name: 'F13' },
      { id: 'F14', name: 'F14' },
      { id: 'F15', name: 'F15' },
      { id: 'F8', name: 'F8' },
      { id: 'F9', name: 'F9' },
      { id: 'nummult', name: 'Numpad *' },
      { id: '`', name: '`' },
      { id: 'CommandOrControl+Shift+Space', name: 'Ctrl/Cmd Shift Space' },
    ],
    platform: process.platform,
  };
}

function sendDesktopConfig() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send('hermes-desktop-config', desktopConfig());
}

function sendTalkback(action) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send('hermes-desktop-talkback', {
    action,
    shortcut: talkbackShortcut,
  });
}

function registerShortcuts() {
  globalShortcut.unregisterAll();
  shortcutRegistered = false;
  shortcutRegistered = globalShortcut.register(talkbackShortcut, () => {
    sendTalkback('toggle');
  });
  if (!shortcutRegistered) {
    console.warn(`Could not register global shortcut: ${talkbackShortcut}`);
  }
  sendDesktopConfig();
}

function installMenu() {
  const template = [
    ...(process.platform === 'darwin'
      ? [{
          label: app.name,
          submenu: [
            { role: 'about' },
            { type: 'separator' },
            { role: 'services' },
            { type: 'separator' },
            { role: 'hide' },
            { role: 'hideOthers' },
            { role: 'unhide' },
            { type: 'separator' },
            { role: 'quit' },
          ],
        }]
      : []),
    {
      label: 'Mic',
      submenu: [
        {
          label: 'Toggle Mic',
          accelerator: talkbackShortcut,
          click: () => sendTalkback('toggle'),
        },
        {
          label: 'Mic On',
          click: () => sendTalkback('start'),
        },
        {
          label: 'Mic Off',
          click: () => sendTalkback('stop'),
        },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 430,
    height: 820,
    minWidth: 360,
    minHeight: 620,
    title: 'Hermes Voice',
    backgroundColor: '#020303',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      autoplayPolicy: 'no-user-gesture-required',
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.webContents.on('did-finish-load', () => {
    sendDesktopConfig();
  });

  await mainWindow.webContents.session.clearCache();
  await mainWindow.loadURL(cacheBustedAppUrl());
}

function cacheBustedAppUrl() {
  const url = new URL(APP_URL);
  url.searchParams.set('desktop', '1');
  url.searchParams.set('desktopBuild', DESKTOP_BUILD);
  return url.toString();
}

ipcMain.handle('hermes-desktop:get-config', () => desktopConfig());
ipcMain.handle('hermes-desktop:set-shortcut', (_event, requestedShortcut) => {
  const nextShortcut = String(requestedShortcut || '').trim();
  if (!TALKBACK_SHORTCUT_CHOICES.has(nextShortcut)) {
    return { ...desktopConfig(), error: 'Unsupported shortcut' };
  }
  talkbackShortcut = nextShortcut;
  saveConfig();
  registerShortcuts();
  installMenu();
  return desktopConfig();
});

app.whenReady().then(async () => {
  app.name = 'Hermes Voice';
  loadConfig();

  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(['media', 'microphone', 'speaker-selection'].includes(permission));
  });
  session.defaultSession.setPermissionCheckHandler((_webContents, permission) => {
    return ['media', 'microphone', 'speaker-selection'].includes(permission);
  });

  installMenu();
  registerShortcuts();
  await createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
