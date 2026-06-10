# Enclave Android client

A sideloaded Android app that wraps the Enclave web UI and adds native
notifications, so you can read and reply to your sessions from your phone
(replacing Matrix for that purpose).

## What it does
- **Connection screen**: enter your server URL + credentials; the app logs in
  (`/api/auth/login`), stores the token encrypted, and injects it into the
  WebView so the web UI is already authenticated.
- **Full web UI** in a WebView (the existing interface, not a reimplementation).
- **Notifications** via a foreground service holding a WebSocket to
  `/api/notifications/stream`:
  - One notification **per session** for major replies (a new reply replaces the
    previous — only the latest per session is kept).
  - A single **pinned** notification summarising sessions that need a reply.
- Trusts the server's self-signed CA (bundled at `res/raw/enclave_ca.crt`) via
  `network_security_config.xml`, so both the WebView and the socket connect.

## Build
Requires Nix (provisions Android SDK + JDK 17 + Gradle automatically):

```bash
./build-apk.sh
```

Output: `app/build/outputs/apk/debug/app-debug.apk` (debug-signed, sideload-ready).

## Install (sideload)
```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```
or copy the APK to the device and open it (enable "install unknown apps").

## Notes
- The bundled CA is copied from the server's `/data/Enclave/tls/ca.crt`. If the
  server CA is regenerated, replace `app/src/main/res/raw/enclave_ca.crt` and
  rebuild.
- Server side: the orchestrator broadcasts `major_reply` events on the global
  notification channel (see `control.py` / `router.py`). Requires the
  orchestrator to be running that code (one restart).
