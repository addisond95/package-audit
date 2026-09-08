package com.packageaudit.scanner;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Conservative unit-number extraction from address lines recognized on-device. */
final class UnitExtractor {
    private static final Pattern LABELED = Pattern.compile(
        "(?i)(?:\\bAPT\\b|\\bAPARTMENT\\b|\\bUNIT\\b|\\bSUITE\\b|\\bSTE\\b)"
            + "\\s*\\.?\\s*(?:NO\\.?\\s*)?[:#,-]?\\s*([0-9OIL]{1,6}[A-Z]?)(?![A-Z0-9])");
    private static final Pattern HASH = Pattern.compile(
        "(?i)(?:^|\\s)#\\s*([0-9OIL]{1,6}[A-Z]?)(?![A-Z0-9])");
    private static final Pattern STANDALONE = Pattern.compile(
        "(?i)^\\s*([0-9]{1,6}[A-Z]?)\\s*$");

    private UnitExtractor() {}

    static List<DetectedUnit> extract(List<String> lines) {
        Map<String, Float> found = new LinkedHashMap<>();
        for (String rawLine : lines) {
            String line = rawLine == null ? "" : rawLine.trim();
            if (line.isEmpty()) continue;
            Matcher labeled = LABELED.matcher(line);
            while (labeled.find()) add(found, clean(labeled.group(1), true), 1.0f);
            Matcher hash = HASH.matcher(line);
            while (hash.find()) add(found, clean(hash.group(1), true), 0.8f);
            Matcher standalone = STANDALONE.matcher(line);
            if (standalone.matches()) add(found, clean(standalone.group(1), false), 0.75f);
        }
        List<DetectedUnit> values = new ArrayList<>();
        for (Map.Entry<String, Float> item : found.entrySet())
            values.add(new DetectedUnit(item.getKey(), item.getValue()));
        return values;
    }

    private static void add(Map<String, Float> values, String unit, float confidence) {
        if (unit.isEmpty()) return;
        values.put(unit, Math.max(confidence, values.getOrDefault(unit, 0.0f)));
    }

    private static String clean(String raw, boolean repairOcr) {
        String source = raw.toUpperCase(Locale.US).replaceAll("[^A-Z0-9]", "");
        if (source.isEmpty() || source.length() > 7) return "";
        StringBuilder value = new StringBuilder();
        int digits = 0;
        for (int index = 0; index < source.length(); index++) {
            char character = source.charAt(index);
            if (repairOcr && character == 'O') character = '0';
            else if (repairOcr && (character == 'I' || character == 'L')) character = '1';
            if (Character.isDigit(character)) digits++;
            value.append(character);
        }
        return digits >= 1 ? value.toString() : "";
    }
}
