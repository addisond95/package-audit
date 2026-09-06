package com.packageaudit.scanner;

import com.google.zxing.*;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.multi.GenericMultipleBarcodeReader;
import java.util.*;

/** Pure JVM decoder, also exercised against generated shipping barcodes in unit tests. */
final class BarcodeDecoder {
    static List<Result> read(byte[] y, int width, int height) {
        Map<DecodeHintType, Object> hints = new EnumMap<>(DecodeHintType.class);
        hints.put(DecodeHintType.TRY_HARDER, Boolean.TRUE);
        LuminanceSource source = new PlanarYUVLuminanceSource(y, width, height, 0, 0, width, height, false);
        try { return decode(source, hints); }
        catch (NotFoundException error) {
            byte[] rotated = new byte[y.length];
            for (int row = 0; row < height; row++)
                for (int col = 0; col < width; col++) rotated[(width - 1 - col) * height + row] = y[row * width + col];
            LuminanceSource sideways = new PlanarYUVLuminanceSource(rotated, height, width, 0, 0, height, width, false);
            try { return decode(sideways, hints); }
            catch (NotFoundException second) {
                try { return decode(source.invert(), hints); }
                catch (NotFoundException third) { return Collections.emptyList(); }
            }
        }
    }

    private static List<Result> decode(LuminanceSource source, Map<DecodeHintType, Object> hints) throws NotFoundException {
        return Arrays.asList(new GenericMultipleBarcodeReader(new MultiFormatReader()).decodeMultiple(
            new BinaryBitmap(new HybridBinarizer(source)), hints));
    }
}
