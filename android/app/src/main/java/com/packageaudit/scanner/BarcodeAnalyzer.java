package com.packageaudit.scanner;

import android.graphics.Rect;
import androidx.camera.core.ExperimentalGetImage;
import androidx.camera.core.ImageAnalysis;
import androidx.camera.core.ImageProxy;
import com.google.mlkit.vision.barcode.BarcodeScanner;
import com.google.mlkit.vision.barcode.BarcodeScannerOptions;
import com.google.mlkit.vision.barcode.BarcodeScanning;
import com.google.mlkit.vision.barcode.ZoomSuggestionOptions;
import com.google.mlkit.vision.barcode.common.Barcode;
import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.Text;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.TextRecognizer;
import com.google.mlkit.vision.text.latin.TextRecognizerOptions;
import com.google.android.gms.tasks.Task;
import com.google.android.gms.tasks.Tasks;
import com.google.zxing.Result;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.Executor;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Processes camera frames in RAM. Bundled ML Kit is primary; the existing
 * local ZXing decoder remains as an independent fallback for difficult labels.
 */
final class BarcodeAnalyzer implements ImageAnalysis.Analyzer, AutoCloseable {
    interface Listener { void detected(List<DetectedBarcode> values, List<DetectedUnit> units); }

    interface ZoomListener { boolean zoom(float ratio); }

    private final Listener listener;
    private final Executor executor;
    private final BarcodeScanner scanner;
    private final TextRecognizer textRecognizer;
    private final AtomicBoolean processing = new AtomicBoolean();
    volatile boolean enabled;

    BarcodeAnalyzer(Executor executor, Listener listener, ZoomListener zoomListener) {
        this.executor = executor;
        ZoomSuggestionOptions zoom = new ZoomSuggestionOptions.Builder(zoomListener::zoom)
            .setMaxSupportedZoomRatio(3.0f)
            .build();
        BarcodeScannerOptions options = new BarcodeScannerOptions.Builder()
            .setBarcodeFormats(
                Barcode.FORMAT_CODE_128,
                Barcode.FORMAT_CODE_39,
                Barcode.FORMAT_CODE_93,
                Barcode.FORMAT_CODABAR,
                Barcode.FORMAT_ITF,
                Barcode.FORMAT_QR_CODE,
                Barcode.FORMAT_DATA_MATRIX,
                Barcode.FORMAT_PDF417,
                Barcode.FORMAT_AZTEC
            )
            .enableAllPotentialBarcodes()
            .setZoomSuggestionOptions(zoom)
            .setExecutor(executor)
            .build();
        scanner = BarcodeScanning.getClient(options);
        textRecognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS);
        this.listener = listener;
    }

    @Override
    @ExperimentalGetImage
    public void analyze(ImageProxy image) {
        if (!enabled || image.getImage() == null || !processing.compareAndSet(false, true)) {
            image.close();
            return;
        }
        try {
            InputImage input = InputImage.fromMediaImage(
                image.getImage(), image.getImageInfo().getRotationDegrees());
            Task<List<Barcode>> barcodeTask = scanner.process(input);
            Task<Text> textTask = textRecognizer.process(input);
            Tasks.whenAllComplete(barcodeTask, textTask).addOnCompleteListener(executor, ignored -> {
                try {
                    List<DetectedBarcode> values = barcodeTask.isSuccessful()
                        ? fromMlKit(barcodeTask.getResult()) : Collections.emptyList();
                    if (values.isEmpty()) values = fromZxing(image);
                    List<DetectedUnit> units = textTask.isSuccessful()
                        ? fromText(textTask.getResult()) : Collections.emptyList();
                    listener.detected(values, units);
                } catch (RuntimeException error) {
                    listener.detected(Collections.emptyList(), Collections.emptyList());
                } finally {
                    processing.set(false);
                    image.close();
                }
            });
        } catch (RuntimeException error) {
            processing.set(false);
            image.close();
            listener.detected(Collections.emptyList(), Collections.emptyList());
        }
    }

    private static List<DetectedBarcode> fromMlKit(List<Barcode> barcodes) {
        List<DetectedBarcode> values = new ArrayList<>();
        for (Barcode barcode : barcodes) {
            String raw = barcode.getRawValue();
            if (raw != null && !raw.trim().isEmpty())
                values.add(new DetectedBarcode(raw.trim(), formatName(barcode.getFormat())));
        }
        return values;
    }

    private static List<DetectedBarcode> fromZxing(ImageProxy image) {
        Rect crop = image.getCropRect();
        ImageProxy.PlaneProxy plane = image.getPlanes()[0];
        ByteBuffer buffer = plane.getBuffer();
        int width = crop.width(), height = crop.height();
        byte[] y = new byte[width * height];
        int base = buffer.position();
        for (int row = 0; row < height; row++) {
            int start = base + (crop.top + row) * plane.getRowStride()
                + crop.left * plane.getPixelStride();
            for (int col = 0; col < width; col++)
                y[row * width + col] = buffer.get(start + col * plane.getPixelStride());
        }
        List<DetectedBarcode> values = new ArrayList<>();
        for (Result result : BarcodeDecoder.read(y, width, height))
            values.add(new DetectedBarcode(result.getText().trim(), result.getBarcodeFormat().toString()));
        return values;
    }

    private static List<DetectedUnit> fromText(Text text) {
        List<String> lines = new ArrayList<>();
        for (Text.TextBlock block : text.getTextBlocks())
            for (Text.Line line : block.getLines()) lines.add(line.getText());
        return UnitExtractor.extract(lines);
    }

    private static String formatName(int format) {
        switch (format) {
            case Barcode.FORMAT_CODE_128: return "CODE_128";
            case Barcode.FORMAT_CODE_39: return "CODE_39";
            case Barcode.FORMAT_CODE_93: return "CODE_93";
            case Barcode.FORMAT_CODABAR: return "CODABAR";
            case Barcode.FORMAT_ITF: return "ITF";
            case Barcode.FORMAT_QR_CODE: return "QR_CODE";
            case Barcode.FORMAT_DATA_MATRIX: return "DATA_MATRIX";
            case Barcode.FORMAT_PDF417: return "PDF_417";
            case Barcode.FORMAT_AZTEC: return "AZTEC";
            default: return "UNKNOWN";
        }
    }

    @Override public void close() { scanner.close(); textRecognizer.close(); }
}
