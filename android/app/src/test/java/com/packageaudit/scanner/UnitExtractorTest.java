package com.packageaudit.scanner;

import org.junit.Test;
import java.util.Arrays;
import java.util.List;
import static org.junit.Assert.*;

public class UnitExtractorTest {
    @Test public void extractsCommonAddressUnitFormatsAndRepairsLabeledOcr() {
        List<DetectedUnit> values = UnitExtractor.extract(Arrays.asList(
            "123 Main Street Apt #2803", "Apartment 1701", "Apt. No. 205", "UNIT: 0305S", "Suite 28O3"));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("2803") && value.confidence == 1.0f));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("1701")));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("205")));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("0305S")));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("2803")));
    }

    @Test public void bareHashCanVerifyButCannotIndependentlyAccuseAMislog() {
        List<DetectedUnit> values = UnitExtractor.extract(Arrays.asList("Order # 123456789", "# 2803"));
        assertFalse(values.stream().anyMatch(value -> value.value.equals("123456")));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("2803") && value.confidence == 0.8f));
    }

    @Test public void standaloneNumbersAreLowerConfidenceAndLongTrackingIsIgnored() {
        List<DetectedUnit> values = UnitExtractor.extract(Arrays.asList("305", "98101", "9400111899223856928499"));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("305") && value.confidence == 0.75f));
        assertTrue(values.stream().anyMatch(value -> value.value.equals("98101") && value.confidence == 0.75f));
        assertFalse(values.stream().anyMatch(value -> value.value.length() > 7));
    }

    @Test public void ordinaryStreetAndRecipientLinesAreNotUnits() {
        assertTrue(UnitExtractor.extract(Arrays.asList("2803 Main Street", "Jane Resident", "Seattle WA")).isEmpty());
    }
}
