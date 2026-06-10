# Android SDK for the Enclave client build (platform 34, build-tools 34.0.0).
{ pkgs ? import (builtins.getFlake "nixpkgs") {
    system = "x86_64-linux";
    config.allowUnfree = true;
    config.android_sdk.accept_license = true;
  }
}:
let
  android = pkgs.androidenv.composeAndroidPackages {
    platformVersions = [ "34" ];
    buildToolsVersions = [ "34.0.0" ];
    includeEmulator = false;
    includeNDK = false;
    includeSystemImages = false;
  };
in android.androidsdk
