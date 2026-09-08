package com.packageaudit.scanner;

import android.Manifest;
import android.app.AlertDialog;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.*;
import android.util.Size;
import android.view.*;
import android.widget.*;
import androidx.activity.ComponentActivity;
import androidx.camera.core.*;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import com.google.common.util.concurrent.ListenableFuture;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.*;

public final class MainActivity extends ComponentActivity implements BleClient.Listener {
    private static final int GREEN = Color.rgb(23, 107, 77);
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService cameraExecutor = Executors.newSingleThreadExecutor();
    private BleClient ble;
    private BarcodeAnalyzer analyzer;
    private ProcessCameraProvider cameras;
    private Camera camera;
    private PreviewView preview;
    private FrameLayout cameraFrame;
    private TextView connection, instructions, unit, details, progress;
    private Button pair, reconnect, scan, scanMode, confirm, flagError, next, undo, torch;
    private boolean pairingMode, scanning, cameraRunning, startingCamera, foreground, requestInFlight;
    private boolean torchOn, trackingOnly;
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
    private long resultEpoch;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON | WindowManager.LayoutParams.FLAG_SECURE);
        ble = new BleClient(this, this);
        analyzer = new BarcodeAnalyzer(
            cameraExecutor,
            (values, units) -> main.post(() -> onDetections(values, units)),
            ratio -> {
                main.post(() -> {
                    if (camera != null)
                        camera.getCameraControl().setZoomRatio(Math.max(1.0f, Math.min(3.0f, ratio)));
                });
                return true;
            });
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
        flagError = button(layout, "Flag for end review", this::flagCurrentError);
        flagError.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(181, 88, 22)));
        flagError.setTextColor(Color.WHITE);
        flagError.setVisibility(View.GONE);
        next = button(layout, "Scan next package", this::nextPackage);
        next.setVisibility(View.GONE);
        undo = button(layout, "Undo last saved scan", this::undoLast);
        undo.setVisibility(View.GONE);
        scan = button(layout, "Scan packages", () -> {
            if (scanning) { scanning = false; analyzer.enabled = false; scan.setText("Resume scanning"); }
            else nextPackage();
        });
        scan.setEnabled(false);
        scanMode = button(layout, "Mode: Tracking + unit", this::toggleScanMode);
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
        label(layout, "Photos stay on this phone and are not saved. Audit data stays on your Mac.", 12);
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
        ble.forget(); pairingMode = true; scanning = false; current = lastSaved = null;
        requestInFlight = false; resultEpoch++; blockedCodes.clear(); resetDetection();
        unit.setText(""); details.setText(""); confirm.setVisibility(View.GONE);
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
                cameras.unbindAll();
                UseCaseGroup.Builder group = new UseCaseGroup.Builder().addUseCase(live).addUseCase(analysis);
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

    private void onDetections(List<DetectedBarcode> values, List<DetectedUnit> units) {
        if (!foreground || !analyzer.enabled) return;
        updateUnitReadings(units);
        if (values.isEmpty()) {
            codeHits.clear(); previousCodes.clear();
            if (++clearFrames >= 3) {
                blockedCodes.clear(); pendingCodes.clear(); stableFrames = 0; pendingSince = 0;
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
        try {
            JSONObject request = BleClient.request("scan");
            request.put("barcodes", new JSONArray(stable.keySet()));
            request.put("formats", new JSONArray(stable.values()));
            if (!trackingOnly) {
                request.put("units", new JSONArray(pendingUnits.keySet()));
                request.put("unit_confidences", new JSONArray(pendingUnits.values()));
            }
            if (ble.send(request)) {
                blockedCodes.clear(); blockedCodes.addAll(stable.keySet());
                requestInFlight = true; scanning = false; analyzer.enabled = false;
                instructions.setText("Looking up tracking on Mac…"); updateControls();
            }
        } catch (Exception error) { instructions.setText("Could not read this label. Try again."); }
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
        scan.setEnabled(enabled && (current == null || !"confirm".equals(current.optString("status"))));
        confirm.setEnabled(enabled); flagError.setEnabled(enabled); next.setEnabled(enabled); undo.setEnabled(enabled);
        scanMode.setEnabled(!requestInFlight);
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
        requestInFlight = false;
        resultEpoch++;
        if (!response.optBoolean("ok")) {
            details.setText(response.optString("error", "Mac could not complete this action."));
            instructions.setText("Check the Mac, or scan the package again.");
            next.setText("Scan again"); next.setVisibility(View.VISIBLE);
            updateControls(); return;
        }
        current = response.optJSONObject("result");
        if (current == null) { details.setText("Invalid response from Mac."); updateControls(); return; }
        updateProgress(current.optJSONObject("progress"));
        String status = current.optString("status");
        String loggedUnit = current.optString("unit");
        unit.setText(loggedUnit.isEmpty() ? "" : "Unit " + loggedUnit);
        String tracking = current.optString("tracking");
        String scannedUnit = current.optString("scanned_unit");
        details.setText(current.optString("message")
            + (scannedUnit.isEmpty() ? "" : "\nLabel unit: " + scannedUnit)
            + (tracking.isEmpty() ? "" : "\nTracking …" + tracking.substring(Math.max(0, tracking.length()-4))));
        boolean needsConfirm = "confirm".equals(status);
        boolean unmatched = "not_found".equals(status);
        boolean unitUnreadable = "unit_not_read".equals(status);
        boolean unitMismatch = "unit_mismatch".equals(status);
        confirm.setVisibility(needsConfirm || unitUnreadable ? View.VISIBLE : View.GONE);
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
        if (("matched".equals(status) || "flagged".equals(status)) && current.optBoolean("saved")) {
            instructions.setText("matched".equals(status)
                ? "Saved. Move to the next package." : "Flagged for Mac review. Move to the next package.");
            long epoch = resultEpoch;
            main.postDelayed(() -> { if (epoch == resultEpoch && foreground && !requestInFlight) nextPackage(); }, 900);
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

    private void flagCurrentError() {
        if (current == null) return;
        String status = current.optString("status");
        if (!"not_found".equals(status) && !"confirm".equals(status)
                && !"unit_not_read".equals(status)) return;
        try { sendAction("not_found".equals(status) ? "not_found" : "flag_review", current, null); }
        catch (Exception error) { details.setText("Could not flag this scan. Try again."); }
    }

    private void sendAction(String op, JSONObject target, String item) throws Exception {
        JSONObject request = BleClient.request(op).put("scan_id", target.getString("scan_id"));
        if (item != null) request.put("item_id", item);
        if (ble.send(request)) {
            requestInFlight = true; scanning = false; analyzer.enabled = false;
            instructions.setText("Waiting for Mac to save…"); resultEpoch++; updateControls();
        }
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
        flagError.setVisibility(View.GONE); next.setVisibility(View.GONE);
        instructions.setText("Fill the camera with one tracking barcode. Tap the preview to focus.");
        analyzer.enabled = true;
        if (camera != null) camera.getCameraControl().setZoomRatio(1.0f);
        startCamera(); focusCenter(); updateControls();
    }

    @Override protected void onStart() {
        super.onStart(); foreground = true;
        if (ble != null && ble.paired() && permissions()) ble.resume();
        if (pairingMode || scanning) startCamera();
    }
    @Override protected void onStop() {
        foreground = false; analyzer.enabled = false; resultEpoch++;
        ble.pause();
        if (cameras != null) cameras.unbindAll();
        cameraRunning = false; torchOn = false;
        super.onStop();
    }
    @Override protected void onDestroy() {
        main.removeCallbacksAndMessages(null); ble.pause(); analyzer.close(); cameraExecutor.shutdown(); super.onDestroy();
    }
}
