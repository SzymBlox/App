# RobloxTools for Android

Files: `main.py` (UI), `core.py` (logic), `buildozer.spec`, `.github/workflows/build.yml`.

## Build the APK (no Linux needed)
1. Create a GitHub repo and upload this whole folder (keep `.github/workflows/build.yml`).
2. Open the repo -> Actions -> "Build APK" -> Run workflow (first build takes ~20-30 min).
3. Download `RobloxTools-apk` from the finished run, unzip, install the `.apk` on your phone.

Or locally on Linux / WSL: `pip install buildozer cython` then `buildozer android debug`.

## First launch
Android asks for "All files access" - allow it, otherwise custom folders can't be read or written.

## Folders (internal storage)
- `/storage/emulated/0/RobloxTools/plugins` - plugins downloaded from the store (shown in Library)
- `/storage/emulated/0/RobloxTools/exported plugins` - created by the first export

To change them, edit `BASE_DIR` / `PLUGINS_DIR` / `EXPORT_DIR` in `core.py`.

## Try it on a PC first
`pip install kivy pillow certifi` then `python main.py` (folders go to `~/RobloxToolsStorage`).
