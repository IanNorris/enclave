#!/usr/bin/env bash
# Build the Enclave Android client APK on NixOS without a system Android SDK.
#
# Provisions the Android SDK, JDK 17 and Gradle from nixpkgs, then runs a debug
# (sideload-ready) build. Output: app/build/outputs/apk/debug/app-debug.apk
set -euo pipefail
cd "$(dirname "$0")"

echo "[+] Resolving toolchain from nixpkgs (cached after first run)…"
SDK_ROOT="$(nix-build "$PWD/sdk.nix" --no-out-link)"
ANDROID_SDK="$SDK_ROOT/libexec/android-sdk"
JDK="$(nix-build '<nixpkgs>' -A jdk17 --no-out-link)"
GRADLE_DIR="$(nix-build '<nixpkgs>' -A gradle --no-out-link)"

export ANDROID_HOME="$ANDROID_SDK"
export JAVA_HOME="$JDK"
export PATH="$JDK/bin:$PATH"
export GRADLE_USER_HOME="$PWD/.gradle-home"

echo "sdk.dir=$ANDROID_SDK" > local.properties

echo "[+] Building debug APK…"
"$GRADLE_DIR/bin/gradle" :app:assembleDebug --no-daemon "$@"

APK="$PWD/app/build/outputs/apk/debug/app-debug.apk"
echo "[+] Done: $APK"
ls -la "$APK"
