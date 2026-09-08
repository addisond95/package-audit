package com.packageaudit.scanner;

/** Privacy-safe unit candidate produced by on-device label text recognition. */
final class DetectedUnit {
    final String value;
    final float confidence;

    DetectedUnit(String value, float confidence) {
        this.value = value;
        this.confidence = confidence;
    }
}
