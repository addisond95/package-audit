package com.packageaudit.scanner;

/** Minimal decoded value crossing the camera/analyzer boundary. */
final class DetectedBarcode {
    final String value;
    final String format;

    DetectedBarcode(String value, String format) {
        this.value = value;
        this.format = format;
    }
}
