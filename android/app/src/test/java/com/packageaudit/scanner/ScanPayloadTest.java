package com.packageaudit.scanner;

import static org.junit.Assert.*;
import java.util.*;
import org.junit.Test;

public final class ScanPayloadTest {
    @Test public void unitsAreBoundedWithStrongReadingsFirst() {
        Map<String, Float> units = new TreeMap<>();
        for (int index = 0; index < 20; index++) units.put(String.valueOf(index), .75f);
        units.put("2803", 1f);
        units.put("invalid", Float.NaN);
        Map<String, Float> bounded = ScanPayload.boundedUnits(units);
        assertEquals(12, bounded.size());
        assertEquals("2803", bounded.keySet().iterator().next());
        assertEquals(Float.valueOf(1f), bounded.values().iterator().next());
        assertFalse(bounded.containsKey("invalid"));
    }
}
