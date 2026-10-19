package com.packageaudit.scanner;

import android.Manifest;
import android.app.AlertDialog;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.net.Uri;
import android.os.*;
import android.util.Size;
import android.view.*;
import android.widget.*;
import androidx.activity.ComponentActivity;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.camera.core.*;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import com.google.common.util.concurrent.ListenableFuture;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import org.json.*;

public final class MainActivity extends ComponentActivity implements BleClient.Listener {
    private static final int GREEN = Color.rgb(23, 107, 77);
    private static final int MAX_BARCODE_LENGTH = 1024;
    private static final int MAX_BARCODE_PAYLOAD = 4096;
    private static final int TRAINING_CHUNK_BYTES = 6144;
    private static final int MAX_TRAINING_IMAGE_BYTES = 512 * 1024;
    private static final String[] PACKAGE_LOCATIONS = {
        "SHELF", "BIN", "BB", "CAGE", "UG", "ALPHA", "FCR", "PERISHABLES", "ODDITIES"
    };
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService cameraExecutor = Executors.newSingleThreadExecutor();
    private BleClient ble;
    private BarcodeAnalyzer analyzer;
    private LabelModel labelModel;
    private ProcessCameraProvider cameras;
    private Camera camera;
    private ImageCapture imageCapture;
    private PreviewView preview;
    private FrameLayout cameraFrame;
    private TextView connection, modelStatus, instructions, unit, details, progress;
    private Button pair, reconnect, scan, scanMode, createAlert, trainingSample, confirm,
        confirmTraining, flagError, next, undo, torch;
    private boolean pairingMode, scanning, cameraRunning, startingCamera, foreground, requestInFlight;
    private boolean torchOn, trackingOnly, manualAlertMode;
    private final Map<String, Integer> codeHits = new HashMap<>();
    private final Set<String> previousCodes = new HashSet<>();
    private final Set<String> blockedCodes = new HashSet<>();
    private final TreeMap<String, String> pendingCodes = new TreeMap<>();
    private final Map<String, Integer> unitHits = new HashMap<>();
    private final Set<String> previousUnits = new HashSet<>();
    private final TreeMap<String, Float> recentUnits = new TreeMap<>();
    private final TreeMap<String, Float> pendingUnits = new TreeMap<>();
    private int clearFrames, stableFrames, unitClearFrames;
    private long pendingSince;
    private JSONObject current, lastSaved;
    private LabelModel.Prediction labelPrediction;
    private long resultEpoch;
    private File pendingTrainingPhoto;
    private JSONObject pendingTrainingMetadata;
    private byte[] automaticTrainingImage;
    private JSONObject automaticTrainingMetadata;
    private String automaticTrainingUploadId;
    private int automaticTrainingOffset;
    private boolean trainingAfterConfirm;
    private final TreeMap<String, String> lastVisibleCodes = new TreeMap<>();
    private final ActivityResultLauncher<String> createTrainingDocument = registerForActivityResult(
        new ActivityResultContracts.CreateDocument("application/zip"), this::writeTrainingBundle);

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON | WindowManager.LayoutParams.FLAG_SECURE);
        ble = new BleClient(this, this);
        labelModel = LabelModel.load(this);
        analyzer = new BarcodeAnalyzer(
            cameraExecutor,
            (values, units, prediction) -> main.post(() -> onDetections(values, units, prediction)),
            ratio -> {
                main.post(() -> {
                    if (camera != null)
                        camera.getCameraControl().setZoomRatio(Math.max(1.0f, Math.min(3.0f, ratio)));
                });
                return true;
            }, labelModel);
        buildScreen();
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private TextView label(LinearLayout layout, String text, int size) {
        TextView view = new TextView(this);
        view.setText(text); view.setTextSize(size); view.setTextColor(Color.rgb(23, 33, 43));
        view.setPadding(0, dp(5), 0, dp(5)); layout.addView(view); return view;
    }
    private Button button(LinearLayout layout, String text, Runnable action) {
        Button value = new Button(this); value.setText(text); value.setAllCaps(false);
        value.setMinHeight(dp(52)); value.setOnClickListener(view -> action.run()); layout.addView(value); return value;
    }

    private void buildScreen() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL); layout.setPadding(dp(18), dp(12), dp(18), dp(18));
        layout.setBackgroundColor(Color.rgb(243, 248, 245));
        scroll.addView(layout);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom); return insets;
        });
        label(layout, "Package Audit", 27);
        connection = label(layout, "Offline Bluetooth scanner", 14);
        modelStatus = label(
            layout,
            labelModel == null
                ? "Label model: deterministic baseline (no approved model installed)"
                : "Label model: verified " + labelModel.releaseId(),
            12);
        progress = label(layout, "Open an audit on your Mac, then click Bluetooth Phone Scanner.", 14);
        pair = button(layout, "Pair with Mac", this::beginPairing);
        reconnect = button(layout, "Reconnect", () -> { ble.pause(); ble.resume(); });
        reconnect.setVisibility(View.GONE);
        preview = new PreviewView(this);
        preview.setImplementationMode(PreviewView.ImplementationMode.COMPATIBLE);
        preview.setScaleType(PreviewView.ScaleType.FILL_CENTER);
        cameraFrame = new FrameLayout(this);
        cameraFrame.addView(preview, new FrameLayout.LayoutParams(-1, -1));
        cameraFrame.addView(new ScanGuideView(this), new FrameLayout.LayoutParams(-1, -1));
        layout.addView(cameraFrame, new LinearLayout.LayoutParams(-1, dp(360)));
        cameraFrame.setVisibility(View.GONE);
        instructions = label(layout, "Pair once for this audit session. Wi-Fi and mobile data can stay off.", 16);
        unit = label(layout, "", 42);
        details = label(layout, "", 16);
        confirm = button(layout, "Confirm unit", this::confirmCurrent);
        confirm.setBackgroundTintList(android.content.res.ColorStateList.valueOf(GREEN));
        confirm.setTextColor(Color.WHITE);
        confirm.setVisibility(View.GONE);
        confirmTraining = button(layout, "Confirm + add label to training", this::confirmCurrentWithTraining);
        confirmTraining.setBackgroundTintList(
            android.content.res.ColorStateList.valueOf(Color.rgb(31, 78, 121)));
        confirmTraining.setTextColor(Color.WHITE);
        confirmTraining.setVisibility(View.GONE);
        flagError = button(layout, "Flag for end review", this::flagCurrentError);
        flagError.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(181, 88, 22)));
        flagError.setTextColor(Color.WHITE);
        flagError.setVisibility(View.GONE);
        next = button(layout, "Scan next package", this::scanAgainFromButton);
        next.setVisibility(View.GONE);
        undo = button(layout, "Undo last saved scan", this::undoLast);
        undo.setVisibility(View.GONE);
        scan = button(layout, "Scan packages", () -> {
            if (scanning) { scanning = false; analyzer.enabled = false; scan.setText("Resume scanning"); }
            else { manualAlertMode = false; nextPackage(); }
        });
        scan.setEnabled(false);
        scanMode = button(layout, "Mode: Tracking + unit", this::toggleScanMode);
        createAlert = button(layout, "Scan package into alert", this::startManualAlertScan);
        createAlert.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(181, 88, 22)));
        createAlert.setTextColor(Color.WHITE);
        trainingSample = button(layout, "Save standalone training example", this::chooseTrainingOutcome);
        torch = button(layout, "Flashlight", () -> {
            if (camera != null && camera.getCameraInfo().hasFlashUnit()) {
                torchOn = !torchOn; camera.getCameraControl().enableTorch(torchOn);
            }
        });
        torch.setVisibility(View.GONE);
        preview.setOnTouchListener((view, event) -> {
            if (event.getAction() == MotionEvent.ACTION_UP && camera != null) {
                MeteringPoint point = preview.getMeteringPointFactory().createPoint(event.getX(), event.getY());
                camera.getCameraControl().startFocusAndMetering(new FocusMeteringAction.Builder(point).build());
                view.performClick();
            }
            return true;
        });
        label(layout, "Routine scans save no photos. Confirm + training sends one encrypted label to the Mac.", 12);
        setContentView(scroll);
    }

    private boolean permissions() {
        return checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
            && checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
            && checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
    }

    private void beginPairing() {
        if (ble.busy() || current != null && "confirm".equals(current.optString("status"))) {
            new AlertDialog.Builder(this).setTitle("Leave this scan?")
                .setMessage("A scan or confirmation is pending. Check the Mac audit before switching pairing.")
                .setNegativeButton("Keep current connection", null)
                .setPositiveButton("Start new pairing", (dialog, which) -> preparePairing()).show();
        } else preparePairing();
    }

    private void preparePairing() {
        ble.forget(); pairingMode = true; scanning = false; manualAlertMode = false;
        clearAutomaticTrainingUpload();
        current = lastSaved = null;
        requestInFlight = false; resultEpoch++; blockedCodes.clear(); resetDetection();
        unit.setText(""); details.setText(""); confirm.setVisibility(View.GONE);
        confirmTraining.setVisibility(View.GONE);
        flagError.setVisibility(View.GONE); next.setVisibility(View.GONE);
        undo.setVisibility(View.GONE); scan.setEnabled(false);
        instructions.setText("Point at the pairing QR inside the Mac's Bluetooth Scanner window.");
        if (!permissions()) requestPermissions(new String[]{Manifest.permission.CAMERA,
            Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT}, 1);
        else startCamera();
    }

    @Override public void onRequestPermissionsResult(int request, String[] names, int[] results) {
        super.onRequestPermissionsResult(request, names, results);
        if (permissions()) startCamera();
        else instructions.setText("Camera and Nearby Devices permissions are required. Tap Pair with Mac to retry.");
    }

    private void startCamera() {
        if (!foreground || !permissions()) return;
        if (cameraRunning) {
            analyzer.enabled = pairingMode || scanning && ble.ready() && !requestInFlight;
            return;
        }
        if (startingCamera) return;
        startingCamera = true;
        cameraFrame.setVisibility(View.VISIBLE);
        ListenableFuture<ProcessCameraProvider> future = ProcessCameraProvider.getInstance(this);
        future.addListener(() -> {
            startingCamera = false;
            if (!foreground || isDestroyed()) return;
            try {
                cameras = future.get();
                Preview live = new Preview.Builder().build();
                live.setSurfaceProvider(preview.getSurfaceProvider());
                ImageAnalysis analysis = new ImageAnalysis.Builder()
                    .setTargetResolution(new Size(1920, 1080))
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST).build();
                analysis.setAnalyzer(cameraExecutor, analyzer);
                imageCapture = new ImageCapture.Builder()
                    .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY).build();
                cameras.unbindAll();
                UseCaseGroup.Builder group = new UseCaseGroup.Builder()
                    .addUseCase(live).addUseCase(analysis).addUseCase(imageCapture);
                ViewPort viewPort = preview.getViewPort();
                if (viewPort != null) group.setViewPort(viewPort);
                camera = cameras.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, group.build());
                cameraRunning = true;
                analyzer.enabled = pairingMode || scanning && ble.ready() && !requestInFlight;
                torch.setVisibility(camera.getCameraInfo().hasFlashUnit() ? View.VISIBLE : View.GONE);
                focusCenter();
            } catch (Exception error) { instructions.setText("Camera could not start. Close other camera apps, then retry."); }
        }, getMainExecutor());
    }

    private void resetDetection() {
        codeHits.clear(); previousCodes.clear(); pendingCodes.clear();
        unitHits.clear(); previousUnits.clear(); recentUnits.clear(); pendingUnits.clear();
        clearFrames = 0; stableFrames = 0; unitClearFrames = 0; pendingSince = 0;
        labelPrediction = null;
    }

    private void toggleScanMode() {
        if (requestInFlight) return;
        trackingOnly = !trackingOnly;
        scanMode.setText(trackingOnly ? "Mode: Tracking only" : "Mode: Tracking + unit");
        resetDetection();
        instructions.setText(trackingOnly
            ? "Tracking-only mode: check the audit unit against the label manually."
            : "Unit-check mode: keep the tracking barcode and destination unit visible.");
    }

    private void focusCenter() {
        preview.post(() -> {
            if (camera == null || preview.getWidth() == 0 || preview.getHeight() == 0) return;
            MeteringPoint point = preview.getMeteringPointFactory().createPoint(
                preview.getWidth() / 2.0f, preview.getHeight() / 2.0f);
            camera.getCameraControl().startFocusAndMetering(new FocusMeteringAction.Builder(point).build());
        });
    }

    private void onDetections(List<DetectedBarcode> values, List<DetectedUnit> units,
                              LabelModel.Prediction prediction) {
        if (!foreground || !analyzer.enabled) return;
        if (prediction != null) labelPrediction = prediction;
        lastVisibleCodes.clear();
        for (DetectedBarcode value : values) lastVisibleCodes.put(value.value.trim(), value.format);
        updateUnitReadings(units);
        if (values.isEmpty()) {
            codeHits.clear(); previousCodes.clear();
            if (++clearFrames >= 3) {
                blockedCodes.clear(); pendingCodes.clear(); stableFrames = 0; pendingSince = 0;
                pendingUnits.clear(); recentUnits.clear();
            }
            return;
        }
        clearFrames = 0;
        // Stabilize each code independently. Incidental barcodes may flicker on busy
        // shipping labels without delaying a tracking code that is consistently clear.
        TreeMap<String, String> codes = new TreeMap<>();
        for (DetectedBarcode value : values) codes.put(value.value.trim(), value.format);
        Map<String, Integer> nextHits = new HashMap<>();
        for (String value : codes.keySet())
            nextHits.put(value, previousCodes.contains(value) ? codeHits.getOrDefault(value, 0) + 1 : 1);
        codeHits.clear(); codeHits.putAll(nextHits);
        previousCodes.clear(); previousCodes.addAll(codes.keySet());
        TreeMap<String, String> stable = new TreeMap<>();
        for (Map.Entry<String, String> code : codes.entrySet())
            if (codeHits.getOrDefault(code.getKey(), 0) >= 2) stable.put(code.getKey(), code.getValue());
        if (stable.isEmpty() || !blockedCodes.isEmpty() && blockedCodes.containsAll(stable.keySet())) return;
        if (pairingMode) {
            for (String value : stable.keySet()) if (value.startsWith("packageaudit:")) {
                try {
                    ble.pair(value); pairingMode = false; analyzer.enabled = false;
                    instructions.setText("Connecting to your Mac…");
                } catch (Exception error) { instructions.setText(error.getMessage()); }
                return;
            }
            instructions.setText("Scan the pairing QR from the Mac, then scan package labels."); return;
        }
        if (!scanning || !ble.ready() || requestInFlight || ble.busy()) return;
        if (pendingCodes.isEmpty()) pendingSince = android.os.SystemClock.elapsedRealtime();
        pendingCodes.putAll(stable);
        pendingUnits.putAll(recentUnits);
        stableFrames++;
        // Keep a short collection window after the first stable code so a
        // second, harder carrier barcode on the same label can join the scan.
        if (stableFrames < 3 || android.os.SystemClock.elapsedRealtime() - pendingSince < 450) return;
        stable.clear(); stable.putAll(pendingCodes);
        if (stable.keySet().stream().anyMatch(value -> value.startsWith("packageaudit:"))) {
            instructions.setText("That is a pairing QR. Point at a package tracking barcode."); return;
        }
        TreeMap<String, String> bounded = boundedPackageCodes(stable);
        if (bounded.isEmpty()) {
            scanning = false;
            analyzer.enabled = false;
            instructions.setText("This label contains too much barcode data. Frame only the tracking barcode and rescan.");
            next.setText("Rescan label");
            next.setVisibility(View.VISIBLE);
            updateControls();
            return;
        }
        if (manualAlertMode) {
            scanning = false;
            analyzer.enabled = false;
            blockedCodes.clear();
            blockedCodes.addAll(stable.keySet());
            prepareManualAlert(bounded, new TreeMap<>(pendingUnits));
            updateControls();
            return;
        }
        try {
            JSONObject request = BleClient.request("scan");
            request.put("barcodes", new JSONArray(bounded.keySet()));
            request.put("formats", new JSONArray(bounded.values()));
            addModelPrediction(request);
            if (!trackingOnly) {
                Map<String, Float> unitsToSend = ScanPayload.boundedUnits(pendingUnits);
                request.put("units", new JSONArray(unitsToSend.keySet()));
                request.put("unit_confidences", new JSONArray(unitsToSend.values()));
            }
            if (ble.send(request)) {
                blockedCodes.clear(); blockedCodes.addAll(stable.keySet());
                requestInFlight = true; scanning = false; analyzer.enabled = false;
                instructions.setText("Looking up tracking on Mac…"); updateControls();
            }
        } catch (Exception error) { instructions.setText("Could not read this label. Try again."); }
    }

    private TreeMap<String, String> boundedPackageCodes(TreeMap<String, String> codes) {
        TreeMap<String, String> result = new TreeMap<>();
        int total = 0;
        for (Map.Entry<String, String> code : codes.entrySet()) {
            if (result.size() >= 8) break;
            String value = code.getKey().trim();
            if (value.isEmpty() || value.length() > MAX_BARCODE_LENGTH) continue;
            int nextTotal = total + value.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
            if (nextTotal > MAX_BARCODE_PAYLOAD) continue;
            result.put(value, code.getValue());
            total = nextTotal;
        }
        return result;
    }

    private void updateUnitReadings(List<DetectedUnit> values) {
        if (values.isEmpty()) {
            unitHits.clear(); previousUnits.clear();
            if (++unitClearFrames >= 3 && pendingCodes.isEmpty()) recentUnits.clear();
            return;
        }
        unitClearFrames = 0;
        TreeMap<String, Float> detected = new TreeMap<>();
        for (DetectedUnit value : values)
            if (!value.value.trim().isEmpty()) detected.put(value.value.trim(), value.confidence);
        recentUnits.keySet().retainAll(detected.keySet());
        Map<String, Integer> nextHits = new HashMap<>();
        for (String value : detected.keySet())
            nextHits.put(value, previousUnits.contains(value) ? unitHits.getOrDefault(value, 0) + 1 : 1);
        unitHits.clear(); unitHits.putAll(nextHits);
        previousUnits.clear(); previousUnits.addAll(detected.keySet());
        for (Map.Entry<String, Float> value : detected.entrySet())
            if (unitHits.getOrDefault(value.getKey(), 0) >= 2)
                recentUnits.put(value.getKey(), Math.max(value.getValue(), recentUnits.getOrDefault(value.getKey(), 0f)));
    }

    @Override public void state(String message, boolean connected) {
        connection.setText(message);
        connection.setTextColor(connected ? GREEN : Color.rgb(157, 62, 18));
        reconnect.setVisibility(ble.paired() && !connected ? View.VISIBLE : View.GONE);
        if (!connected) analyzer.enabled = pairingMode;
        else analyzer.enabled = scanning && !requestInFlight;
        updateControls();
    }

    private void updateControls() {
        boolean enabled = ble.ready() && !requestInFlight;
        scan.setEnabled(enabled && (current == null ||
            !("confirm".equals(current.optString("status")) || "unit_not_read".equals(current.optString("status")))));
        confirm.setEnabled(enabled); confirmTraining.setEnabled(enabled && cameraRunning);
        flagError.setEnabled(enabled); next.setEnabled(enabled); undo.setEnabled(enabled);
        scanMode.setEnabled(!requestInFlight);
        createAlert.setEnabled(enabled && current == null);
        trainingSample.setEnabled(cameraRunning && !requestInFlight);
        if (!pairingMode) scan.setText(scanning ? "Pause scanning" : "Scan packages");
    }

    @Override public void response(String operation, JSONObject response) {
        if ("status".equals(operation)) {
            if (response.optBoolean("ok")) {
                updateProgress(response.optJSONObject("result"));
                if (current == null && !requestInFlight && !scanning) instructions.setText("Connected. Tap Scan packages to begin.");
            }
            return;
        }
        if (operation.startsWith("training_")) {
            requestInFlight = false;
            handleAutomaticTrainingResponse(operation, response);
            updateControls();
            return;
        }
        requestInFlight = false;
        resultEpoch++;
        if ("manual_alert".equals(operation) || "save_manual_alert".equals(operation))
            manualAlertMode = false;
        if (!response.optBoolean("ok")) {
            if (trainingAfterConfirm) clearAutomaticTrainingUpload();
            details.setText(response.optString("error", "Mac could not complete this action."));
            instructions.setText("Check the Mac, or scan the package again.");
            next.setText("Scan again"); next.setVisibility(View.VISIBLE);
            updateControls(); return;
        }
        if ("prepare_alert".equals(operation)) {
            JSONObject prepared = response.optJSONObject("result");
            if (prepared == null) {
                details.setText("Invalid alert response from Mac.");
                next.setText("Scan again"); next.setVisibility(View.VISIBLE); updateControls(); return;
            }
            chooseManualAlertTracking(prepared);
            updateProgress(prepared.optJSONObject("progress"));
            updateControls();
            return;
        }
        current = response.optJSONObject("result");
        if (current == null) { details.setText("Invalid response from Mac."); updateControls(); return; }
        updateProgress(current.optJSONObject("progress"));
        String status = current.optString("status");
        String loggedUnit = current.optString("unit");
        unit.setText(loggedUnit.isEmpty() ? "" : "Unit " + loggedUnit);
        String tracking = current.optString("tracking");
        String scannedUnit = current.optString("scanned_unit");
        String packageLocation = current.optString("location");
        details.setText(current.optString("message")
            + (scannedUnit.isEmpty() ? "" : "\nLabel unit: " + scannedUnit)
            + (packageLocation.isEmpty() ? "" : "\nLocation: " + packageLocation)
            + (tracking.isEmpty() ? "" : "\nTracking …" + tracking.substring(Math.max(0, tracking.length()-4))));
        boolean needsConfirm = "confirm".equals(status);
        boolean unmatched = "not_found".equals(status);
        boolean unitUnreadable = "unit_not_read".equals(status);
        boolean unitMismatch = "unit_mismatch".equals(status);
        confirm.setVisibility(needsConfirm || unitUnreadable ? View.VISIBLE : View.GONE);
        confirmTraining.setVisibility(needsConfirm || unitUnreadable ? View.VISIBLE : View.GONE);
        flagError.setText(needsConfirm ? "Flag unit mismatch" : "Flag for end review");
        flagError.setVisibility(needsConfirm || unmatched || unitUnreadable ? View.VISIBLE : View.GONE);
        confirm.setText(unitUnreadable
            ? "Yes — unit " + loggedUnit + " is correct"
            : "Confirm unit " + loggedUnit);
        next.setText(needsConfirm || unmatched || unitUnreadable ? "Rescan label" : "Scan next package");
        next.setVisibility(View.VISIBLE);
        if (("matched".equals(status) || "flagged".equals(status) || "duplicate".equals(status)
                || unitMismatch)
                && current.optBoolean("can_undo") && current.optBoolean("saved")) lastSaved = current;
        if ("undo_queued".equals(status) || "rejected".equals(status)) lastSaved = null;
        undo.setVisibility(lastSaved != null ? View.VISIBLE : View.GONE);
        String unitStatus = current.optString("unit_status");
        instructions.setText(needsConfirm
            ? ("verified".equals(unitStatus)
                ? "Tracking and unit matched. Check the result, then confirm."
                : "Tracking matched. Check the unit on the label, then confirm manually.")
            : unitMismatch ? "Do not deliver by the audit unit. Investigate this package."
            : unitUnreadable ? "Unit was not clear. Confirm it manually, rescan, or flag it for review."
            : "Check the result below.");
        Vibrator vibrator = getSystemService(Vibrator.class);
        if (vibrator != null) vibrator.vibrate(VibrationEffect.createOneShot(needsConfirm ? 35 : 80, VibrationEffect.DEFAULT_AMPLITUDE));
        updateControls();
        boolean startTrainingUpload = "confirm".equals(operation) && trainingAfterConfirm
            && "matched".equals(status) && current.optBoolean("saved");
        if (startTrainingUpload) {
            trainingAfterConfirm = false;
            beginAutomaticTrainingUpload();
        }
        if (("matched".equals(status) || "flagged".equals(status)) && current.optBoolean("saved")) {
            if (!startTrainingUpload) instructions.setText("matched".equals(status)
                ? "Saved. Move to the next package." : "Flagged for Mac review. Move to the next package.");
            if (!startTrainingUpload) {
                long epoch = resultEpoch;
                main.postDelayed(() -> { if (epoch == resultEpoch && foreground && !requestInFlight) nextPackage(); }, 900);
            }
        }
        if ("rejected".equals(status)) { blockedCodes.clear(); nextPackage(); }
    }

    private void updateProgress(JSONObject value) {
        if (value != null) progress.setText(value.optInt("audited") + " of " + value.optInt("packages")
            + " checked • " + value.optInt("remaining") + " remaining");
    }

    private void confirmCurrent() {
        if (current == null) return;
        try {
            JSONObject candidate = current.getJSONArray("candidates").getJSONObject(0);
            sendAction("confirm", current, candidate.getString("item_id"));
        } catch (Exception error) { details.setText("Scan this package again."); }
    }

    private void confirmCurrentWithTraining() {
        if (current == null || imageCapture == null || requestInFlight) return;
        try {
            JSONObject target = new JSONObject(current.toString());
            String item = target.getJSONArray("candidates").getJSONObject(0).getString("item_id");
            String outcome = "unit_not_read".equals(target.optString("status"))
                ? "unit_read_error" : "correct";
            JSONObject metadata = trainingMetadata(outcome, target);
            File photo = File.createTempFile("package-audit-confirmed-", ".jpg", getCacheDir());
            requestInFlight = true;
            scanning = false;
            analyzer.enabled = false;
            instructions.setText("Capturing label, then saving package…");
            updateControls();
            ImageCapture.OutputFileOptions output = new ImageCapture.OutputFileOptions.Builder(photo).build();
            imageCapture.takePicture(output, cameraExecutor, new ImageCapture.OnImageSavedCallback() {
                @Override public void onImageSaved(ImageCapture.OutputFileResults result) {
                    try {
                        byte[] image = prepareAutomaticTrainingImage(photo);
                        main.post(() -> {
                            photo.delete();
                            automaticTrainingImage = image;
                            automaticTrainingMetadata = metadata;
                            automaticTrainingUploadId = UUID.randomUUID().toString();
                            automaticTrainingOffset = 0;
                            trainingAfterConfirm = true;
                            requestInFlight = false;
                            try { sendAction("confirm", target, item); }
                            catch (Exception error) {
                                clearAutomaticTrainingUpload();
                                details.setText("Could not confirm this package. Try again.");
                                updateControls();
                            }
                        });
                    } catch (IOException error) {
                        photo.delete();
                        main.post(() -> {
                            requestInFlight = false;
                            details.setText("Could not prepare the label image. Use normal Confirm or try again.");
                            updateControls();
                        });
                    }
                }
                @Override public void onError(ImageCaptureException error) {
                    photo.delete();
                    main.post(() -> {
                        requestInFlight = false;
                        details.setText("Could not capture the label. Use normal Confirm or try again.");
                        updateControls();
                    });
                }
            });
        } catch (Exception error) {
            requestInFlight = false;
            details.setText("Could not prepare this training example. Use normal Confirm or rescan.");
            updateControls();
        }
    }

    private void flagCurrentError() {
        if (current == null) return;
        String status = current.optString("status");
        if (!"not_found".equals(status) && !"confirm".equals(status)
                && !"unit_not_read".equals(status)) return;
        try { sendAction("not_found".equals(status) ? "not_found" : "flag_review", current, null); }
        catch (Exception error) { details.setText("Could not flag this scan. Try again."); }
    }

    private void startManualAlertScan() {
        if (!ble.ready() || requestInFlight || current != null) return;
        scanning = false;
        analyzer.enabled = false;
        manualAlertMode = true;
        blockedCodes.clear();
        nextPackage();
        instructions.setText("Scan the package label. After tracking and unit are read, choose its location.");
    }

    private void prepareManualAlert(TreeMap<String, String> codes, TreeMap<String, Float> units) {
        try {
            Map<String, Float> unitsToSend = ScanPayload.boundedUnits(units);
            JSONObject request = BleClient.request("prepare_alert")
                .put("barcodes", new JSONArray(codes.keySet()))
                .put("formats", new JSONArray(codes.values()))
                .put("units", new JSONArray(unitsToSend.keySet()))
                .put("unit_confidences", new JSONArray(unitsToSend.values()));
            addModelPrediction(request);
            if (ble.send(request)) {
                requestInFlight = true;
                instructions.setText("Checking tracking choices on Mac…");
                resultEpoch++;
            } else {
                instructions.setText("Could not send the alert scan. Check the connection and try again.");
                next.setText("Scan again"); next.setVisibility(View.VISIBLE);
            }
        } catch (Exception error) {
            details.setText("Could not prepare this alert. Scan the package again.");
            next.setText("Scan again"); next.setVisibility(View.VISIBLE);
        }
    }

    private void addModelPrediction(JSONObject request) throws JSONException {
        if (labelPrediction == null) return;
        request.put("model_carrier", labelPrediction.carrier)
            .put("model_carrier_confidence", labelPrediction.carrierConfidence)
            .put("label_condition", labelPrediction.condition)
            .put("label_condition_confidence", labelPrediction.conditionConfidence)
            .put("model_release", labelPrediction.releaseId);
    }

    private AlertDialog chooseManualAlertTracking(JSONObject prepared) {
        JSONArray options = prepared.optJSONArray("tracking_options");
        if (options == null || options.length() == 0) {
            details.setText("No usable tracking barcode was returned. Scan the tracking barcode again.");
            next.setText("Scan again"); next.setVisibility(View.VISIBLE); return null;
        }
        if (options.length() == 1) {
            return showManualAlertLocation(prepared, options.optString(0));
        }
        String[] labels = new String[options.length()];
        for (int index = 0; index < options.length(); index++) {
            String value = options.optString(index);
            labels[index] = value;
        }
        return new AlertDialog.Builder(this)
            .setTitle("Choose tracking barcode")
            .setItems(labels, (dialog, which) -> showManualAlertLocation(prepared, options.optString(which)))
            .setNegativeButton("Rescan", (dialog, which) -> scanAgainFromButton())
            .setOnCancelListener(dialog -> scanAgainFromButton())
            .show();
    }

    private AlertDialog showManualAlertLocation(JSONObject prepared, String tracking) {
        String scannedUnit = prepared.optString("scanned_unit");
        String foundUnit = scannedUnit.isEmpty() ? "Unit not read" : "Label unit: " + scannedUnit;
        return new AlertDialog.Builder(this)
            .setTitle("Choose package location — " + foundUnit)
            .setItems(PACKAGE_LOCATIONS, (dialog, which) -> sendManualAlert(prepared, tracking, PACKAGE_LOCATIONS[which]))
            .setNegativeButton("Cancel", (dialog, which) -> {
                manualAlertMode = false;
                resetDetection();
                instructions.setText("Alert cancelled. Tap Scan packages or Scan package into alert.");
                updateControls();
            })
            .setOnCancelListener(dialog -> {
                manualAlertMode = false;
                resetDetection();
                instructions.setText("Alert cancelled. Tap Scan packages or Scan package into alert.");
                updateControls();
            })
            .show();
    }

    private void sendManualAlert(JSONObject prepared, String tracking, String location) {
        try {
            JSONObject request = BleClient.request("save_manual_alert")
                .put("scan_id", prepared.getString("scan_id"))
                .put("tracking", tracking)
                .put("location", location);
            if (ble.send(request)) {
                requestInFlight = true;
                instructions.setText("Waiting for Mac to save the alert…");
                resultEpoch++;
                updateControls();
            } else {
                manualAlertMode = false;
                instructions.setText("Could not send the alert. Check the connection and try again.");
                updateControls();
            }
        } catch (Exception error) {
            manualAlertMode = false;
            details.setText("Could not create this alert. Scan the package again.");
            updateControls();
        }
    }

    private void scanAgainFromButton() {
        blockedCodes.clear();
        nextPackage();
    }

    private JSONObject trainingMetadata(String outcome, JSONObject scanResult) throws JSONException {
        JSONObject metadata = new JSONObject()
            .put("schema_version", 1)
            .put("consent", true)
            .put("outcome", outcome)
            .put("source", "android_local")
            .put("device", Build.MANUFACTURER + " " + Build.MODEL)
            .put("app_version", appVersion())
            .put("created_at", java.time.Instant.now().toString())
            .put("capture_session", "audit-" + java.time.LocalDate.now().toString())
            .put("barcodes", new JSONArray(lastVisibleCodes.keySet()))
            .put("formats", new JSONArray(lastVisibleCodes.values()))
            .put("units", new JSONArray(recentUnits.keySet()));
        if (scanResult != null) metadata.put("carrier", scanResult.optString("carrier"));
        if (labelPrediction != null) {
            metadata.put("model_carrier", labelPrediction.carrier)
                .put("model_carrier_confidence", labelPrediction.carrierConfidence)
                .put("label_condition", labelPrediction.condition)
                .put("label_condition_confidence", labelPrediction.conditionConfidence)
                .put("model_release", labelPrediction.releaseId);
        }
        return metadata;
    }

    private static byte[] prepareAutomaticTrainingImage(File photo) throws IOException {
        Bitmap bitmap = BitmapFactory.decodeFile(photo.getAbsolutePath());
        if (bitmap == null) throw new IOException("Photo could not be decoded");
        Bitmap working = bitmap;
        int largest = Math.max(bitmap.getWidth(), bitmap.getHeight());
        if (largest > 1280) {
            float scale = 1280f / largest;
            working = Bitmap.createScaledBitmap(bitmap,
                Math.max(1, Math.round(bitmap.getWidth() * scale)),
                Math.max(1, Math.round(bitmap.getHeight() * scale)), true);
            bitmap.recycle();
        }
        ExifInterface exif = new ExifInterface(photo.getAbsolutePath());
        int orientation = exif.getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL);
        Matrix transform = new Matrix();
        if (orientation == ExifInterface.ORIENTATION_ROTATE_90) transform.postRotate(90);
        else if (orientation == ExifInterface.ORIENTATION_ROTATE_180) transform.postRotate(180);
        else if (orientation == ExifInterface.ORIENTATION_ROTATE_270) transform.postRotate(270);
        if (!transform.isIdentity()) {
            Bitmap rotated = Bitmap.createBitmap(working, 0, 0, working.getWidth(), working.getHeight(), transform, true);
            if (rotated != working) working.recycle();
            working = rotated;
        }
        byte[] encoded = null;
        for (int quality : new int[]{82, 70, 58, 46}) {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            if (!working.compress(Bitmap.CompressFormat.JPEG, quality, output)) {
                working.recycle();
                throw new IOException("Photo could not be compressed");
            }
            encoded = output.toByteArray();
            if (encoded.length <= MAX_TRAINING_IMAGE_BYTES) break;
        }
        working.recycle();
        if (encoded == null || encoded.length > MAX_TRAINING_IMAGE_BYTES)
            throw new IOException("Training photo is too large");
        return encoded;
    }

    private void beginAutomaticTrainingUpload() {
        if (automaticTrainingImage == null || automaticTrainingMetadata == null
                || automaticTrainingUploadId == null) {
            failAutomaticTrainingUpload("Package saved, but no label image was available for training.");
            return;
        }
        try {
            JSONObject request = BleClient.request("training_begin")
                .put("upload_id", automaticTrainingUploadId)
                .put("size", automaticTrainingImage.length)
                .put("sha256", hex(MessageDigest.getInstance("SHA-256").digest(automaticTrainingImage)))
                .put("metadata", automaticTrainingMetadata);
            sendAutomaticTrainingRequest(request, "Package saved. Starting encrypted training upload…");
        } catch (Exception error) {
            failAutomaticTrainingUpload("Package saved, but the training upload could not start.");
        }
    }

    private void sendNextAutomaticTrainingChunk() {
        if (automaticTrainingImage == null || automaticTrainingUploadId == null) return;
        try {
            if (automaticTrainingOffset >= automaticTrainingImage.length) {
                sendAutomaticTrainingRequest(
                    BleClient.request("training_finish").put("upload_id", automaticTrainingUploadId),
                    "Verifying encrypted training example on Mac…");
                return;
            }
            int count = Math.min(TRAINING_CHUNK_BYTES,
                automaticTrainingImage.length - automaticTrainingOffset);
            byte[] chunk = Arrays.copyOfRange(automaticTrainingImage, automaticTrainingOffset,
                automaticTrainingOffset + count);
            JSONObject request = BleClient.request("training_chunk")
                .put("upload_id", automaticTrainingUploadId)
                .put("offset", automaticTrainingOffset)
                .put("data", Base64.getEncoder().encodeToString(chunk));
            int percent = Math.round(100f * automaticTrainingOffset / automaticTrainingImage.length);
            sendAutomaticTrainingRequest(request, "Package saved. Uploading label locally… " + percent + "%");
        } catch (Exception error) {
            failAutomaticTrainingUpload("Package saved, but the training upload was interrupted.");
        }
    }

    private void sendAutomaticTrainingRequest(JSONObject request, String message) {
        if (!ble.send(request)) {
            failAutomaticTrainingUpload("Package saved, but the Mac was busy. Training label was not added.");
            return;
        }
        requestInFlight = true;
        instructions.setText(message);
        updateControls();
    }

    private void handleAutomaticTrainingResponse(String operation, JSONObject response) {
        if (!response.optBoolean("ok")) {
            failAutomaticTrainingUpload("Package saved, but training upload failed: "
                + response.optString("error", "unknown error"));
            return;
        }
        JSONObject result = response.optJSONObject("result");
        if (result == null || automaticTrainingUploadId == null
                || !automaticTrainingUploadId.equals(result.optString("upload_id"))) {
            failAutomaticTrainingUpload("Package saved, but the Mac returned an invalid training response.");
            return;
        }
        if ("training_finish".equals(operation)) {
            clearAutomaticTrainingUpload();
            instructions.setText("Package saved and label added to Pending training data on Mac.");
            long epoch = ++resultEpoch;
            main.postDelayed(() -> {
                if (epoch == resultEpoch && foreground && !requestInFlight) nextPackage();
            }, 700);
            return;
        }
        int offset = result.optInt("offset", -1);
        if (automaticTrainingImage == null || offset < automaticTrainingOffset
                || offset > automaticTrainingImage.length) {
            failAutomaticTrainingUpload("Package saved, but training upload progress was invalid.");
            return;
        }
        automaticTrainingOffset = offset;
        sendNextAutomaticTrainingChunk();
    }

    private void failAutomaticTrainingUpload(String message) {
        clearAutomaticTrainingUpload();
        requestInFlight = false;
        instructions.setText(message);
        next.setText("Scan next package");
        next.setVisibility(View.VISIBLE);
        updateControls();
    }

    private void clearAutomaticTrainingUpload() {
        trainingAfterConfirm = false;
        if (automaticTrainingImage != null) Arrays.fill(automaticTrainingImage, (byte) 0);
        automaticTrainingImage = null;
        automaticTrainingMetadata = null;
        automaticTrainingUploadId = null;
        automaticTrainingOffset = 0;
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) result.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        return result.toString();
    }

    private void chooseTrainingOutcome() {
        if (imageCapture == null || pendingTrainingPhoto != null) return;
        final String[] labels = {
            "Correct scan", "Missed barcode", "Wrong tracking", "Wrong carrier",
            "Unit read error", "Unusual address layout", "Unusual name layout",
            "Damaged label", "Other"
        };
        final String[] values = {
            "correct", "missed_barcode", "wrong_tracking", "wrong_carrier",
            "unit_read_error", "address_layout", "name_layout", "damaged_label", "other"
        };
        new AlertDialog.Builder(this)
            .setTitle("Save a training example")
            .setItems(labels, (dialog, which) -> captureTrainingPhoto(values[which]))
            .setNegativeButton("Cancel", null)
            .show();
    }

    private void captureTrainingPhoto(String outcome) {
        try {
            File photo = File.createTempFile("package-audit-label-", ".jpg", getCacheDir());
            pendingTrainingPhoto = photo;
            JSONObject metadata = trainingMetadata(outcome, current);
            pendingTrainingMetadata = metadata;
            ImageCapture.OutputFileOptions output = new ImageCapture.OutputFileOptions.Builder(photo).build();
            imageCapture.takePicture(output, cameraExecutor, new ImageCapture.OnImageSavedCallback() {
                @Override public void onImageSaved(ImageCapture.OutputFileResults result) {
                    main.post(() -> {
                        instructions.setText("Choose where to save the training file for Mac import.");
                        createTrainingDocument.launch("package-audit-label-" + System.currentTimeMillis() + ".pasample");
                    });
                }
                @Override public void onError(ImageCaptureException error) {
                    main.post(() -> {
                        discardPendingTrainingSample();
                        details.setText("Could not capture the training photo. Try again.");
                    });
                }
            });
        } catch (IOException | JSONException error) {
            discardPendingTrainingSample();
            details.setText("Could not prepare the training example.");
        }
    }

    private void writeTrainingBundle(Uri destination) {
        if (destination == null || pendingTrainingPhoto == null || pendingTrainingMetadata == null) {
            discardPendingTrainingSample();
            instructions.setText("Training example cancelled. No photo was retained.");
            return;
        }
        File photo = pendingTrainingPhoto;
        JSONObject metadata = pendingTrainingMetadata;
        pendingTrainingPhoto = null;
        pendingTrainingMetadata = null;
        cameraExecutor.execute(() -> {
            try (InputStream input = new FileInputStream(photo)) {
                OutputStream raw = getContentResolver().openOutputStream(destination);
                if (raw == null) throw new IOException("Output unavailable");
                try (raw; ZipOutputStream zip = new ZipOutputStream(raw)) {
                zip.putNextEntry(new ZipEntry("image.jpg"));
                byte[] buffer = new byte[8192];
                int count;
                while ((count = input.read(buffer)) != -1) zip.write(buffer, 0, count);
                zip.closeEntry();
                zip.putNextEntry(new ZipEntry("metadata.json"));
                zip.write(metadata.toString().getBytes(StandardCharsets.UTF_8));
                zip.closeEntry();
                main.post(() -> instructions.setText("Training example exported. Import and review it on the Mac before training."));
                }
            } catch (IOException error) {
                main.post(() -> details.setText("Could not export the training example."));
            } finally {
                photo.delete();
            }
        });
    }

    private void discardPendingTrainingSample() {
        if (pendingTrainingPhoto != null) pendingTrainingPhoto.delete();
        pendingTrainingPhoto = null;
        pendingTrainingMetadata = null;
    }

    private String appVersion() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException error) {
            return "unknown";
        }
    }

    private void sendAction(String op, JSONObject target, String item) throws Exception {
        JSONObject request = BleClient.request(op).put("scan_id", target.getString("scan_id"));
        if (item != null) request.put("item_id", item);
        if (ble.send(request)) {
            requestInFlight = true; scanning = false; analyzer.enabled = false;
            instructions.setText("Waiting for Mac to save…"); resultEpoch++; updateControls();
        } else throw new IllegalStateException("Mac connection is unavailable or busy.");
    }

    private void undoLast() {
        if (lastSaved == null) return;
        try { sendAction("undo", lastSaved, null); }
        catch (Exception error) { details.setText("Could not undo. Check the Mac audit."); }
    }

    private void nextPackage() {
        if (!ble.ready() || requestInFlight) return;
        if (current != null && ("confirm".equals(current.optString("status"))
                || "not_found".equals(current.optString("status"))
                || "unit_not_read".equals(current.optString("status")))) {
            try { sendAction("reject", current, null); }
            catch (Exception error) { details.setText("Could not clear the suggestion."); }
            return;
        }
        resultEpoch++; current = null; resetDetection(); scanning = true;
        unit.setText(""); details.setText(""); confirm.setVisibility(View.GONE);
        confirmTraining.setVisibility(View.GONE);
        flagError.setVisibility(View.GONE); next.setVisibility(View.GONE);
        instructions.setText("Fill the camera with one tracking barcode. Tap the preview to focus.");
        analyzer.enabled = true;
        if (camera != null) camera.getCameraControl().setZoomRatio(1.0f);
        startCamera(); focusCenter(); updateControls();
    }

    @Override protected void onStart() {
        super.onStart(); foreground = true;
        if (ble != null && ble.paired() && permissions()) ble.resume();
        if (pairingMode || scanning || current != null) startCamera();
    }
    @Override protected void onStop() {
        foreground = false; analyzer.enabled = false; resultEpoch++;
        ble.pause();
        if (cameras != null) cameras.unbindAll();
        cameraRunning = false; torchOn = false;
        super.onStop();
    }
    @Override protected void onDestroy() {
        main.removeCallbacksAndMessages(null); discardPendingTrainingSample();
        clearAutomaticTrainingUpload(); ble.pause(); analyzer.close(); cameraExecutor.shutdown(); super.onDestroy();
    }
}
