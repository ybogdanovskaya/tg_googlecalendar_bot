declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData: string;
        ready: () => void;
        expand: () => void;
        close: () => void;
        themeParams?: Record<string, string | undefined>;
      };
    };
  }
}

export function telegramInitData(): string | null {
  const webApp = window.Telegram?.WebApp;
  if (!webApp?.initData) {
    return null;
  }
  webApp.ready();
  webApp.expand();
  return webApp.initData;
}

export function closeTelegramApp(): void {
  window.Telegram?.WebApp?.close();
}
