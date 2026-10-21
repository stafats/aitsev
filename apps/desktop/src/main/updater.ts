/**
 * Automatic updates from GitHub Releases (only in installed builds). electron-builder writes
 * the feed configuration (app-update.yml) into the installer; updates are verified against the
 * code signature on Windows and macOS before they are applied.
 */
import { autoUpdater } from "electron-updater";

const SIX_HOURS = 6 * 60 * 60 * 1000;

export interface UpdaterHost {
  isPackaged: boolean;
}

export function shouldCheckForUpdates(host: UpdaterHost, env: NodeJS.ProcessEnv, smoke: boolean): boolean {
  return host.isPackaged && !smoke && env["AITSEV_DISABLE_UPDATES"] !== "1";
}

export function startAutoUpdates(host: UpdaterHost, smoke: boolean): void {
  if (!shouldCheckForUpdates(host, process.env, smoke)) return;
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on("error", (err: Error) => console.warn("Update check failed:", err.message));
  const check = () => void autoUpdater.checkForUpdatesAndNotify().catch((err: unknown) => {
    console.warn("Update check failed:", err instanceof Error ? err.message : String(err));
  });
  check();
  setInterval(check, SIX_HOURS).unref();
}
