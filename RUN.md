# How to run Aitsev (Windows)

**What your users get** is a coding editor like Kiro, called **Aitsev**:

1. They click **Download** on your website. The installer shows its progress and then opens Aitsev.
2. They open their project folder and click **Sign in with browser**.
3. They chat with their code in the panel on the left:
   - pick the model from a dropdown;
   - let the agent edit files on **Autopilot**, or review every change in **Supervised** mode;
   - see suggestions from the background analysis as squiggles in the editor.

They never install Python or Node, paste keys, or set up models. Your server holds the Ollama, Claude and GPT keys and routes every request.

The product has three parts:
- **Aitsev**, the editor. It's built on VS Code, and the chat and agent are built in.
- **Aitsev Studio**, a desktop app for integrations, data migration and CRM work (Dynamics 365, Salesforce, HubSpot, Zoho CRM).
- **The extension**, for people who want to stay in VS Code, Cursor, Kiro or Windsurf. It's the same chat.

**This guide is for you, the owner.** It takes you from an empty PC to the whole product running on your machine, then shows how to publish it. Plan for about 30–45 minutes the first time.

---

## 1. Install the tools (once)

Install these, accepting the defaults. After installing, **close and reopen PowerShell** so the new commands are found.

| Tool | Why | Where |
| --- | --- | --- |
| Git | version control, pushing to GitHub | https://git-scm.com/download/win |
| Python 3.12 or newer | the server | https://www.python.org/downloads/ (tick **Add python.exe to PATH**) |
| uv | installs the server's Python packages | PowerShell: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Node.js 22 LTS | Studio, web app, extension | https://nodejs.org |
| pnpm | JavaScript package manager | PowerShell: `corepack enable` then `corepack prepare pnpm@10.28.0 --activate` |
| Docker Desktop (recommended) | runs AI-generated code safely in an isolated sandbox | https://www.docker.com/products/docker-desktop/ |
| VS Code (optional) | for the extension | https://code.visualstudio.com |

Check that everything is installed:

```powershell
git --version
python --version      # 3.12 or newer
uv --version
node --version        # v22.x
pnpm --version        # 10.x
docker --version      # only if you installed Docker Desktop
```

## 2. Get an Ollama Cloud API key

1. Sign in at https://ollama.com.
2. Open **Settings → Keys** (https://ollama.com/settings/keys) and create a key.
3. Keep it private. You'll paste it into the server's settings file in step 4.

## 3. Unzip the project

Unzip `aitsev-full.zip` to a short path, for example `C:\work\aitsev` (long paths can cause trouble on Windows):

```powershell
Expand-Archive aitsev-full.zip -DestinationPath C:\work
cd C:\work\aitsev
```

Optional: put the project on GitHub.

```powershell
git init
git add -A
git commit -m "Aitsev phase 5"
git branch -M main
git remote add origin https://github.com/<your-account>/aitsev.git
git push -u origin main
```

## 4. Configure the server

Create the settings file from the example:

```powershell
Copy-Item .env.example apps\backend\.env
notepad apps\backend\.env
```

Change these lines and save:

```ini
OLLAMA_API_KEY=<your Ollama Cloud key>

# Your own admin key for automation (any long random text, 16+ characters).
# Generate one:  python -c "import secrets; print(secrets.token_urlsafe(32))"
AITSEV_API_KEYS=suppu:<paste the generated key>

# Encrypts saved connection passwords. Generate it the same way and keep it safe:
# if you lose it, saved connection credentials can't be decrypted.
AITSEV_SECRET_KEY=<another generated key>

AITSEV_ENVIRONMENT=development
```

**Claude and GPT (later):** when you're ready to offer them, add `ANTHROPIC_API_KEY=` and `OPENAI_API_KEY=` here. Until then they appear to users as "Coming soon"; only paid plans (Pro and above) can pick them.

**Sandbox choice:**
- **With Docker Desktop running** (recommended): leave `AITSEV_SANDBOX_MODE=docker`. The first task downloads the `python:3.12-slim` image, which takes a minute.
- **Without Docker**, for a quick trial only: set `AITSEV_SANDBOX_MODE=local`. AI-generated code then runs directly on your PC without isolation, so use it only with your own test projects.

Never commit `apps\backend\.env` to git. It's already in `.gitignore`.

## 5. Install the dependencies

From the project root (`C:\work\aitsev`):

```powershell
pnpm install
cd apps\backend
uv sync
cd ..\..
```

## 6. Build the web app

```powershell
pnpm --filter aitsev-desktop build:web
```

This creates `apps\desktop\dist-web`.

## 7. Create the database and start the server

```powershell
cd apps\backend
uv run alembic upgrade head
$env:AITSEV_WEB_DIST_DIR = "..\desktop\dist-web"
uv run aitsev-api
```

Leave this window open: it's the running server. You should see `Aitsev 0.5.0 started (development)`. The development database is the file `apps\backend\aitsev.db`.

To start the server again later, open PowerShell and run:

```powershell
cd C:\work\aitsev\apps\backend
$env:AITSEV_WEB_DIST_DIR = "..\desktop\dist-web"
uv run aitsev-api
```

## 8. Use the web app

1. Open **http://localhost:8000**. You'll see the landing page.
2. Click **Create a free account**. The **first account becomes the admin**.
3. You land on **Connections**. The left menu has Mappings, Dynamics 365, Code tasks and **Account**.
4. On **Account** you can see your usage this month, create **API keys**, change your password and, as admin, change other accounts' plans.

In development, email confirmation is switched off, and "forgot password" emails are printed in the server window instead of being sent.

## 9. Check the AI models

1. Open **http://localhost:8000/docs**.
2. Click **Authorize** and enter your key from `AITSEV_API_KEYS` (the part after `suppu:`).
3. Run **GET /v1/models**. Every role should show `available: true`.

If a model shows as unavailable, your Ollama account uses a different model name. Change the matching line in `apps\backend\.env` (for example `AITSEV_GENERATOR_MODELS=...`) and restart the server.

## 10. Aitsev Studio (integrations, migration and CRM)

Open a **second** PowerShell window (the server keeps running in the first):

```powershell
cd C:\work\aitsev
pnpm package:vscode            # builds the editor extension Studio can install
cd apps\desktop
pnpm start
```

1. **Welcome to Aitsev** → click **Sign in with your browser**.
2. In the browser, check the code matches and click **Allow**.
3. Optionally tick your editors and click **Add to selected editors**. This puts the Aitsev chat into VS Code, Cursor, Kiro or Windsurf.
4. Click **Start using Aitsev**.

**Connect a CRM.** Go to **Connections → Add connection** and click a preset. Each preset fills in the address and sign-in type and shows where to get the keys:

| Preset | You need |
| --- | --- |
| **Dynamics 365** | An app registration in Microsoft Entra ID that's added as an application user. Use its client ID and secret. |
| **Salesforce** | A connected app with the client credentials flow and a run-as user. Use its consumer key and secret. |
| **HubSpot** | A private app's access token, with CRM schema and object scopes. |
| **Zoho CRM** | A self client from the Zoho API console. Paste its client ID, secret and a refresh token. `.in` is preset; change it to `.com` or `.eu` for other regions. |

Then open **CRM**:
- **Analyse** finds empty fields, missing required values and duplicates.
- **Design fields:** describe what you need in plain English. Two models design the fields, you review them, then approve and apply.

Mark sandboxes as **sandbox**. Anything else asks for confirmation before changes are applied.

**Build the Studio installer:** run `pnpm run dist`. The result is `apps\desktop\release\Aitsev-Studio-Setup.exe`.

## 11. Chat and agent (the extension; built into the Aitsev editor)

Install the extension into VS Code, Cursor or Kiro. Studio does this for you (step 10). To install it by hand:

```powershell
cd C:\work\aitsev
pnpm package:vscode
code --install-extension apps\vscode\aitsev.vsix      # or: cursor / kiro / windsurf --install-extension ...
```

1. **For local testing only:** open Settings, search **Aitsev: Server Url** and set it to `http://localhost:8000`.
2. Open a project folder. Click the **Aitsev** icon in the left bar, or press **Ctrl+L**. Click **Sign in with browser**.
3. Use the **Chat** panel:
   - **Model dropdown:** choose *Auto* to let Aitsev pick, or a specific model. Premium models show *(Pro)* on the Free plan.
   - **Agent / Ask:** *Agent* changes code, *Ask* only answers.
   - **Supervised / Autopilot:**
     - *Supervised* shows each changed file with **Accept**, **Reject** and a diff (click the file name).
     - *Autopilot* writes the changes straight away, with one-click **Undo**.
     - If you edited a file while the agent was working, Autopilot doesn't overwrite it and asks you instead.
   - **Tests:** when your project has tests, the agent's edits are run in the sandbox first. You'll see *Tests passed* (or *passed after a fix*) on the reply.
   - **Suggestions:** after you open a folder, and after each save, Aitsev reviews the code in the background:
     - problems show as squiggles and in the Problems view;
     - each one has an **Ask Aitsev to fix** quick fix (the light bulb) and a **Fix** button in the chat;
     - turn it off with **Aitsev: Background Analysis** in Settings.
   - **Ask about selection:** select code and press **Ctrl+Shift+L**.
4. The **Tasks** panel is still there for larger jobs. Several models write the change, cross-review it, and you approve the winner.

## 12. Plans and models

* Every new account starts on **Free**, which includes the Ollama models (GPT-OSS, Kimi, DeepSeek, GLM).
* **Pro**, **Team** and **Enterprise** add premium models (Claude, GPT). These count more towards the monthly token allowance because they cost more: Claude Sonnet and GPT-5 count ×5, Claude Opus counts ×10.
* To try a paid plan: in the web app go to **Account → Accounts**, then set the plan. You are the admin because yours was the first account.
* Payment collection isn't connected yet. For now, set an account's plan after the customer pays.
* To change the list of models, set `AITSEV_MODEL_CATALOG` in `apps\backend\.env` (see `apps/backend/src/aitsev/catalog.py`). The model names for Claude and GPT are defaults; check them against your provider accounts before enabling them.

## 13. The Aitsev editor (the VS Code fork)

The editor is VS Code's open-source code (version in `ide/vscode-version.txt`) with Aitsev's name, icons and installer, the Open VSX extension store, and the Aitsev extension built in. It's rebuilt from source.

**Easiest: let GitHub build it.**
1. Push the repo to GitHub.
2. Go to **Actions → ide-release → Run workflow** and enter a tag such as `v0.6.0`.
3. About 1–2 hours later the release has:
   - `Aitsev-IDE-Setup.exe` (a Windows installer with a progress window that opens Aitsev at the end)
   - `Aitsev-IDE-mac-arm64.dmg`
   - `Aitsev-IDE-linux-x64.deb`

If the repository is **private**, GitHub's standard Windows runners are too small for this build. Set the repository variable `IDE_RUNNER_WINDOWS` to a larger runner with 16 GB of memory (see the top of `.github/workflows/ide-release.yml`).

**On your own PC (Windows, 16 GB RAM, 1–2 hours):**
1. Install **Visual Studio 2022 Build Tools** with "Desktop development with C++", **Python 3.11**, and the Node.js version in the VS Code source's `.nvmrc`.
2. Then run:

```powershell
cd C:\work
git clone --depth 1 --branch (Get-Content aitsev\ide\vscode-version.txt) https://github.com/microsoft/vscode.git
cd aitsev
pnpm package:vscode
node ide\prepare.mjs --vscode ..\vscode
cd ..\vscode
npm ci
npm run gulp vscode-win32-x64-min
cd ..
node aitsev\ide\install-extensions.mjs --app VSCode-win32-x64 --vsix aitsev\apps\vscode\aitsev.vsix
cd vscode
npm run gulp vscode-win32-x64-inno-updater
npm run gulp vscode-win32-x64-user-setup
```

The installer is written to `vscode\.build\win32-x64\user-setup\Aitsev-Setup.exe`. To change the icons, edit `ide/branding/make_icons.py` and run it with Python.

## 14. Try each feature

- **Chat:** open a small project with tests, press Ctrl+L and type "add input validation to the signup function and tests for it". Review the diff and accept, or switch to Autopilot.
- **Code task:** in the Tasks panel, describe a bigger change. Several models write it, it's tested in the sandbox, the models cross-review, and you approve.
- **Integration and migration:** under **Connections**, add two systems (base URL, auth, and an OpenAPI or OData spec, or sample JSON). Click **Discover**, then on **Mappings** create a mapping between two entities. Review the suggested field matches, approve, run a **dry run**, and then a real run.
- **CRM:** in Studio, add a Dynamics 365, Salesforce, HubSpot or Zoho connection from its preset (environment **sandbox**). Then go to **CRM → Analyse**, or describe new fields in plain English, review them, approve and apply.

## 15. Run the tests (optional)

```powershell
# backend
cd C:\work\aitsev\apps\backend
uv run pytest -q

# desktop, web, VS Code extension and shared core
cd C:\work\aitsev
pnpm test
```

## 16. Full stack with Docker (PostgreSQL + server + web app)

When you want something closer to production on one machine:

```powershell
cd C:\work\aitsev
Copy-Item .env.example .env       # same values as step 4; this file is for docker compose
notepad .env
docker compose up --build
```

Then open http://localhost:8000. The database is PostgreSQL, and migrations run automatically.

## 17. Going public (the real product)

**1. Host the server.** Use a Linux VPS with Docker and your domain, for example `app.aitsev.com`.

Set these in `.env`:

```ini
AITSEV_ENVIRONMENT=production
AITSEV_PUBLIC_URL=https://app.aitsev.com
AITSEV_SECRET_KEY=<long random>
OLLAMA_API_KEY=<key>
AITSEV_SMTP_HOST=smtp.your-provider.com
AITSEV_SMTP_USERNAME=...
AITSEV_SMTP_PASSWORD=...
AITSEV_EMAIL_FROM=Aitsev <no-reply@aitsev.com>
AITSEV_DOWNLOAD_BASE_URL=https://github.com/<you>/aitsev/releases/latest/download
```

Then:
1. Run `docker compose up -d --build`.
2. Put HTTPS in front with Caddy or Nginx and a Let's Encrypt certificate.
3. Sign up first, so your account becomes the admin.

`AITSEV_PUBLIC_URL` must be exactly the address users reach the server at, because browser sign-in checks it.

**2. Set up GitHub once.** Go to the repository's Settings → Secrets and variables → Actions.

| Name | Type | What it does |
| --- | --- | --- |
| `AITSEV_PUBLIC_URL` | Variable | The server the installed apps and the extension connect to (`https://app.aitsev.com`). |
| `WIN_CSC_LINK`, `WIN_CSC_KEY_PASSWORD` | Secrets | Signs the Windows installer, so there's no SmartScreen warning. Buy a code-signing certificate (for example from Sectigo or DigiCert). |
| `MAC_CSC_LINK`, `MAC_CSC_KEY_PASSWORD`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` | Secrets | Signs and notarizes the Mac app. Needs an Apple Developer account. |
| `VSCE_PAT` | Secret | Publishes to the VS Code Marketplace. Create the publisher `aitsev` at https://marketplace.visualstudio.com/manage. |
| `OVSX_PAT` | Secret | Publishes to Open VSX, the store Cursor, Kiro, Windsurf and VSCodium use. Get a token at https://open-vsx.org. |

**3. Release.**
1. Bump `version` in `apps/desktop/package.json` and `apps/vscode/package.json`.
2. Run:
   ```powershell
   git tag v0.5.1
   git push origin v0.5.1
   ```

GitHub then builds:
- **the Aitsev editor:** `Aitsev-IDE-Setup.exe`, the `.dmg` and the `.deb` (the `ide-release` workflow);
- **Aitsev Studio:** `Aitsev-Studio-Setup.exe`, the `.dmg` files and the `.AppImage`;
- **the extension:** published to both stores;
- **the server image.**

The **Download** buttons on your site always point to the newest release:
- `/download/windows`, `/download/mac` and `/download/linux` give the editor;
- `/download/studio-windows` and the other `studio-` links give Studio;
- `/download/vscode` gives the extension file.

Studio updates itself. For a new editor version, users download the new installer for now.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `uv` / `pnpm` not recognised | Close and reopen PowerShell. For pnpm, run `corepack enable` again. |
| `running scripts is disabled` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. |
| Server says `AITSEV_SECRET_KEY must be set` | You set `AITSEV_ENVIRONMENT` to something other than `development` without a secret key. |
| Port 8000 in use | Run `$env:AITSEV_PORT = "8001"` before `uv run aitsev-api`, and use that port everywhere. |
| Tasks fail with Docker errors | Start Docker Desktop and wait until it says "running", or use `AITSEV_SANDBOX_MODE=local` for a trial. |
| Models unavailable / 401 from Ollama | Check `OLLAMA_API_KEY` in `apps\backend\.env`, restart the server, then check **GET /v1/models**. |
| Web page is blank or `/app` returns 404 | Run step 6 again and set `AITSEV_WEB_DIST_DIR` before starting the server. |
| "Upgrade your plan" (HTTP 402) | Your monthly limit is used up. As admin, change the plan on **Account → Accounts**. |
| Studio or the extension says invalid API key | Sign out and use **Sign in with your browser** again. |
| "The sign-in page is not on your Aitsev server" | The app's server address and `AITSEV_PUBLIC_URL` on the server must match exactly (for example both `http://localhost:8000`, not `127.0.0.1`). |
| An editor isn't found by Studio | Install the extension by hand (step 11), or turn on the editor's "install shell command" option and restart Studio. |
| Chat says "Open a project folder first" | Use File → Open Folder. The agent works on the files in that folder. |
| No squiggles from background analysis | Make sure you're signed in and **Aitsev: Background Analysis** is on. It runs a few seconds after opening a folder, then at most every two minutes after saves. Run **Aitsev: Analyze Project Now** to force it. |
| The IDE build fails with "out of memory" | Use a 16 GB machine or runner (step 13). |
| "needs the Pro plan" when choosing a model | That's a premium model. As admin, change the account's plan on **Account → Accounts**. |
| Forgot the admin password | Use "Forgot your password?". In development the reset link appears in the server window. |
