package com.packageaudit.scanner;

import android.graphics.Rect;
import androidx.camera.core.ImageAnalysis;
import androidx.camera.core.ImageProxy;
import com.google.zxing.Result;
import java.nio.ByteBuffer;
import java.util.*;

/** Processes camera luminance in RAM. No photo creation, storage, uploads, or telemetry. */
final class BarcodeAnalyzer implements ImageAnalysis.Analyzer {
    interface Listener { void detected(List<Result> values); }
    private final Listener listener;
    private long lastFrame;
    volatile boolean enabled;

    BarcodeAnalyzer(Listener listener) { this.listener = listener; }

    @Override public void analyze(ImageProxy image) {
        try {
            long now = android.os.SystemClock.elapsedRealtime();
            if (!enabled || now - lastFrame < 180) return;
            lastFrame = now;
            Rect crop = image.getCropRect();
            ImageProxy.PlaneProxy plane = image.getPlanes()[0];
            ByteBuffer buffer = plane.getBuffer();
            int width = crop.width(), height = crop.height();
            byte[] y = new byte[width * height];
            int base = buffer.position();
            for (int row = 0; row < height; row++) {
                int start = base + (crop.top + row) * plane.getRowStride() + crop.left * plane.getPixelStride();
                for (int col = 0; col < width; col++) y[row * width + col] = buffer.get(start + col * plane.getPixelStride());
            }
            listener.detected(BarcodeDecoder.read(y, width, height));
        } catch (RuntimeException error) {
            listener.detected(Collections.emptyList());
        } finally { image.close(); }
    }

}
