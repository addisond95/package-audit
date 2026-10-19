package com.packageaudit.scanner;

import java.util.LinkedHashMap;
import java.util.Map;

/** Keep OCR evidence within the receiver's limit, preferring explicit unit readings. */
final class ScanPayload {
    private ScanPayload() {}

    static Map<String, Float> boundedUnits(Map<String, Float> units) {
        Map<String, Float> result = new LinkedHashMap<>();
        units.entrySet().stream()
            .filter(item -> !item.getKey().isEmpty() && item.getKey().length() <= 16
                && item.getValue() != null && Float.isFinite(item.getValue())
                && item.getValue() >= 0 && item.getValue() <= 1)
            .sorted(Map.Entry.<String, Float>comparingByValue().reversed()
                .thenComparing(Map.Entry.comparingByKey()))
            .limit(12)
            .forEach(item -> result.put(item.getKey(), item.getValue()));
        return result;
    }
}
