#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p native/build
swiftc -O -module-cache-path native/build/module-cache \
  -framework CoreBluetooth -framework Foundation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker native/Info.plist \
  native/BluetoothBridge.swift -o native/build/PackageAuditBluetooth
codesign --force --sign - --identifier com.packageaudit.bluetooth native/build/PackageAuditBluetooth
