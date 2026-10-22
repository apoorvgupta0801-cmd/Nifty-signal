[app]
title = Nifty Signal
package.name = niftysignal
package.domain = org.nifty
source.dir = .
source.include_exts = py
version = 0.1
requirements = python3,kivy==2.3.0,requests,urllib3,chardet,idna,certifi,charset-normalizer
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 21
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
