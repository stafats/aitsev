# Aitsev IDE

The Aitsev editor is [Code - OSS](https://github.com/microsoft/vscode) (the MIT-licensed source of VS Code) with:

* Aitsev's product identity: name, icons, installer images, data folder `~/.aitsev`, URL protocol `aitsev://` (`product.overlay.json`, `branding/`)
* the [Open VSX](https://open-vsx.org) extension store, since Microsoft's Marketplace may only be used by Microsoft products
* the Aitsev extension (chat, agent, background suggestions, tasks) as a built-in extension
* `aitsev-defaults`, which hides the built-in Copilot UI so Aitsev is the AI, and turns telemetry off

| File | Purpose |
| --- | --- |
| `vscode-version.txt` | The VS Code release the IDE is built from. Bump it to update. |
| `product.overlay.json` | Fields merged into VS Code's `product.json`. Keep the GUIDs: Windows uses them to upgrade installs in place. |
| `prepare.mjs` | Applies the overlay, icons and installer name to a VS Code checkout. |
| `install-extensions.mjs` | Copies the Aitsev extension (from its `.vsix`) and the defaults into a built IDE. |
| `branding/make_icons.py` | Regenerates every icon and installer image from one drawing. |
| `extensions/aitsev-defaults` | Default settings for the IDE only. |

Build: `.github/workflows/ide-release.yml` (GitHub Actions), or by hand; see `RUN.md`, step 13.

Rules for a VS Code fork: don't use Microsoft's names or logos, don't point at the Visual Studio Marketplace, and keep VS Code's MIT license notice (it ships in the build as `LICENSE.txt`).
