[app]
title = RobloxTools
package.name = robloxtools
package.domain = org.robloxtools
source.dir = .
source.include_exts = py
version = 1.0
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,pillow,certifi,openssl,pyjnius,android
orientation = portrait
fullscreen = 0

# Internal storage access (MANAGE_EXTERNAL_STORAGE = "All files access", asked at first launch)
android.permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE
android.api = 34
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
