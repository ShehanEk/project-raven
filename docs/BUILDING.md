# Building Raven for Windows and macOS

Raven is packaged with [PyInstaller](https://pyinstaller.org) using
[`packaging/raven.spec`](../packaging/raven.spec). PyInstaller can't cross-compile, so
**build the Windows app on Windows and the Mac app on a Mac**. (The `build` GitHub Actions
workflow does exactly this on a Windows and a macOS runner; see
[Letting GitHub build it](#letting-github-build-it) if you'd rather not build locally.)

| | Windows | macOS |
|---|---|---|
| Output | `dist\Raven\Raven.exe` (a folder) | `dist/Raven.app` |
| Zip to share | `dist\Raven-Windows.zip` | `dist/Raven-macOS.zip` |
| Needs | Windows 10 or newer | a current macOS |
| Architecture | the CPU you build on (x64 or ARM64) | the CPU you build on (Apple Silicon or Intel) |

You need **Python 3.10 or newer** (CI uses 3.12) and Git on the machine you build on.

## Windows

Use PowerShell. Install Python from <https://www.python.org/downloads/> first and tick
*Add python.exe to PATH*.

```powershell
git clone https://github.com/ShehanEk/project-raven.git raven
cd raven

python -m venv .venv
.venv\Scripts\Activate.ps1
# If PowerShell blocks the script: Set-ExecutionPolicy -Scope Process Bypass

pip install -e ".[dev]"
pyinstaller --noconfirm packaging\raven.spec
```

The app is now in `dist\Raven\`. Run `dist\Raven\Raven.exe` to try it. To share it, zip the
whole folder (the exe needs the files next to it):

```powershell
Compress-Archive -Path dist\Raven -DestinationPath dist\Raven-Windows.zip -Force
```

Copy the zip to another PC, unzip it anywhere and open `Raven.exe`.

**First run:** the app isn't code-signed, so SmartScreen may say *Windows protected your PC*.
Choose **More info**, then **Run anyway**. Windows Firewall will ask to allow Raven on
private networks the first time you switch on Remote access; allow it.

## macOS

Use Terminal. Install Python from <https://www.python.org/downloads/> or with
`brew install python@3.12`. If `git` asks you to install the command line tools, accept.

```bash
git clone https://github.com/ShehanEk/project-raven.git raven
cd raven

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
pyinstaller --noconfirm packaging/raven.spec
```

The app is now at `dist/Raven.app`. Run it with `open dist/Raven.app`. To move it to
another Mac, zip it with `ditto` (a plain Finder zip or `zip` can break the bundle):

```bash
cd dist && ditto -c -k --keepParent Raven.app Raven-macOS.zip && cd ..
```

Unzip on the other Mac and drag `Raven.app` into Applications.

**First run:** the app isn't notarized, so Gatekeeper blocks a double-click. Right-click
`Raven.app`, choose **Open**, then **Open** again. If macOS says the app is damaged (this
happens to zips sent through a browser or chat), clear the download flag:

```bash
xattr -cr /Applications/Raven.app
```

**Permissions:** open **System Settings → Privacy & Security** and allow Raven under
**Screen Recording** (to be viewed) and **Accessibility** (to be controlled). Restart Raven
afterwards. macOS ties these grants to the app's signature, so after you rebuild the app
you may need to remove Raven from those lists and add it again.

Optional: signing the app with an ad-hoc signature before you first grant permissions
makes the grants more stable on the Mac you built it on:

```bash
codesign --force --deep --sign - dist/Raven.app
```

## Letting GitHub build it

You don't need a Windows PC or a Mac to get both builds:

- **Every push and pull request:** the `build` workflow's *package* job uploads
  `Raven-Windows` and `Raven-macOS` artifacts. Open the run in the **Actions** tab and
  download them from the bottom of the page.
- **A public release:** push a version tag and the *release* job attaches both zips to a
  GitHub Release, which the website's download buttons point at:

  ```bash
  git tag v0.1.0
  git push origin v0.1.0
  ```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `pyinstaller` is not recognised | The virtual environment isn't active. Re-run the activate command, or use `python -m PyInstaller packaging/raven.spec`. |
| `No module named PySide6` when running the built app | Install with `pip install -e ".[dev]"` inside the active venv, then rebuild. |
| A fresh build behaves like an old one | Delete the `build/` and `dist/` folders and build again. |
| The Windows app is flagged by antivirus | Unsigned PyInstaller apps trigger false positives. Sign the exe or add an exclusion; see [Not code-signed](ARCHITECTURE.md#packaging-and-ci). |
| The Mac app can't see or control the screen | Grant Screen Recording and Accessibility (above), then restart Raven. |
