package com.packageaudit.scanner;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.Instrumentation;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.os.Bundle;
import java.lang.reflect.Method;
import java.lang.reflect.Field;
import java.nio.FloatBuffer;
import java.util.function.BooleanSupplier;
import org.json.JSONArray;
import org.json.JSONObject;

/** Device regressions using platform instrumentation, with no production audit data. */
public final class ScannerInstrumentation extends Instrumentation {
    private String pairingQr;
    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        pairingQr = arguments == null ? null : arguments.getString("pairing");
        start();
    }

    @Override public void onStart() {
        Bundle result = new Bundle();
        Activity activity = null;
        try {
            Intent intent = new Intent(getTargetContext(), MainActivity.class);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            activity = startActivitySync(intent);
            final Activity target = activity;
            checkDialog(target, "chooseManualAlertTracking", new Class<?>[]{JSONObject.class},
                new Object[]{new JSONObject().put("tracking_options", new JSONArray()
                    .put("1Z999AA10123456784").put("94000000000000000001"))}, 2);
            checkDialog(target, "showManualAlertLocation", new Class<?>[]{JSONObject.class, String.class},
                new Object[]{new JSONObject().put("scanned_unit", "1701"), "1Z999AA10123456784"}, 9);
            Bitmap red = Bitmap.createBitmap(16, 16, Bitmap.Config.ARGB_8888);
            red.eraseColor(Color.RED);
            FloatBuffer tensor = LabelModel.rgbTensor(red, 90);
            red.recycle();
            check(Math.abs(tensor.get(0) - (1f - .485f) / .229f) < .0001f, "Red channel mismatch");
            check(Math.abs(tensor.get(224 * 224) - (0f - .456f) / .224f) < .0001f, "Green channel mismatch");
            check(Math.abs(tensor.get(2 * 224 * 224) - (0f - .406f) / .225f) < .0001f, "Blue channel mismatch");
            checkCameraResume((MainActivity) target);
            if (pairingQr != null) checkBluetooth((MainActivity) target);
            result.putString("stream", "PASS: tracking choices, location choices, RGB model input, camera resume"
                + (pairingQr == null ? "" : ", Bluetooth lookup/confirm/reconnect/retry/undo/training") + "\n");
            finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            result.putString("stream", "FAIL: " + error + "\n");
            finish(Activity.RESULT_CANCELED, result);
        } finally {
            if (activity != null) {
                final Activity target = activity;
                runOnMainSync(target::finish);
            }
        }
    }

    private void checkCameraResume(MainActivity activity) throws Exception {
        Field current = MainActivity.class.getDeclaredField("current");
        current.setAccessible(true);
        Method startCamera = MainActivity.class.getDeclaredMethod("startCamera");
        startCamera.setAccessible(true);
        JSONObject pending = new JSONObject().put("status", "confirm");
        Throwable[] failure = new Throwable[1];
        runOnMainSync(() -> {
            try { current.set(activity, pending); startCamera.invoke(activity); }
            catch (Throwable error) { failure[0] = error; }
        });
        if (failure[0] != null) throw new AssertionError(failure[0]);
        await(() -> cameraRunning(activity), "Camera did not start");
        runOnMainSync(() -> activity.moveTaskToBack(true));
        await(() -> !cameraRunning(activity), "Camera did not pause in the background");
        Intent resume = new Intent(getTargetContext(), MainActivity.class);
        resume.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
        getTargetContext().startActivity(resume);
        await(() -> cameraRunning(activity), "Camera did not resume with confirmation pending");
        runOnMainSync(() -> {
            try { current.set(activity, null); }
            catch (Exception error) { throw new AssertionError(error); }
        });
    }

    private boolean cameraRunning(MainActivity activity) {
        try { return (Boolean) field(activity, "cameraRunning"); }
        catch (Exception error) { throw new AssertionError(error); }
    }

    private Object field(Object target, String name) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return field.get(target);
    }

    private void await(BooleanSupplier condition, String message) throws Exception {
        long deadline = android.os.SystemClock.elapsedRealtime() + 25000;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            boolean[] ready = new boolean[1];
            runOnMainSync(() -> ready[0] = condition.getAsBoolean());
            if (ready[0]) return;
            Thread.sleep(100);
        }
        throw new AssertionError(message);
    }

    private JSONObject current(MainActivity activity) {
        try { return (JSONObject) field(activity, "current"); }
        catch (Exception error) { throw new AssertionError(error); }
    }

    private void send(BleClient ble, JSONObject request) throws Exception {
        Bundle stage = new Bundle();
        stage.putString("stream", "Testing Bluetooth operation: " + request.optString("op") + "\n");
        sendStatus(0, stage);
        await(() -> ble.ready() && !ble.busy(), "Bluetooth did not become ready before " + request.optString("op"));
        runOnMainSync(() -> check(ble.send(request), "Request was not sent"));
        await(() -> !ble.busy(), "Mac did not acknowledge request");
    }

    private void checkBluetooth(MainActivity activity) throws Exception {
        BleClient original = (BleClient) field(activity, "ble");
        BleClient ble = new BleClient(activity, new BleClient.Listener() {
            @Override public void state(String message, boolean connected) {
                Bundle status = new Bundle();
                status.putString("stream", "Phone transport: " + message + "\n");
                sendStatus(0, status);
                activity.state(message, connected);
            }
            @Override public void response(String operation, JSONObject response) {
                activity.response(operation, response);
            }
        });
        Field transport = MainActivity.class.getDeclaredField("ble");
        transport.setAccessible(true);
        runOnMainSync(() -> {
            original.forget();
            try { transport.set(activity, ble); }
            catch (Exception error) { throw new AssertionError(error); }
        });
        // Keep this transport test from optically scanning the phone's surroundings.
        Field foreground = MainActivity.class.getDeclaredField("foreground");
        foreground.setAccessible(true);
        runOnMainSync(() -> {
            try { foreground.setBoolean(activity, false); }
            catch (Exception error) { throw new AssertionError(error); }
        });
        Throwable[] failure = new Throwable[1];
        runOnMainSync(() -> {
            try { ble.pair(pairingQr); }
            catch (Throwable error) { failure[0] = error; }
        });
        if (failure[0] != null) throw new AssertionError(failure[0]);
        await(() -> ble.ready() && !ble.busy(), "Phone could not pair with disposable Mac receiver");
        send(ble, BleClient.request("scan").put("barcodes", new JSONArray().put("1Z999AA10123456784"))
            .put("formats", new JSONArray().put("CODE_128"))
            .put("units", new JSONArray().put("1701"))
            .put("unit_confidences", new JSONArray().put(1.0)));
        JSONObject scanned = current(activity);
        check(scanned != null && "confirm".equals(scanned.optString("status")), "Expected confirmation");
        check("verified".equals(scanned.optString("unit_status")), "Expected verified unit");
        check(scanned.getJSONObject("progress").getInt("audited") == 0, "Package saved before confirmation");
        JSONObject confirm = BleClient.request("confirm").put("scan_id", scanned.getString("scan_id"))
            .put("item_id", scanned.getJSONArray("candidates").getJSONObject(0).getString("item_id"));
        runOnMainSync(() -> {
            check(ble.send(confirm), "Confirmation was not sent");
            ble.pause(); ble.resume();
        });
        await(() -> ble.ready() && !ble.busy(), "Pending confirmation did not recover after reconnect");
        check(current(activity).optBoolean("saved"), "Confirmation was not saved");
        runOnMainSync(() -> { ble.pause(); ble.resume(); });
        send(ble, confirm);
        check(current(activity).optBoolean("saved"), "Reconnect retry was not acknowledged");
        send(ble, BleClient.request("undo").put("scan_id", scanned.getString("scan_id")));
        check(current(activity).getJSONObject("progress").getInt("audited") == 0, "Undo did not restore state");

        Bitmap label = Bitmap.createBitmap(160, 160, Bitmap.Config.ARGB_8888);
        // Produce more than one application chunk, so this exercises large GATT
        // writes as well as the encrypted upload's ordered offsets.
        java.util.Random pattern = new java.util.Random(42);
        for (int y = 0; y < 160; y++) for (int x = 0; x < 160; x++)
            label.setPixel(x, y, Color.rgb(pattern.nextInt(256), pattern.nextInt(256), pattern.nextInt(256)));
        java.io.ByteArrayOutputStream bytes = new java.io.ByteArrayOutputStream();
        label.compress(Bitmap.CompressFormat.JPEG, 80, bytes); label.recycle();
        byte[] jpeg = bytes.toByteArray();
        StringBuilder digest = new StringBuilder();
        for (byte item : java.security.MessageDigest.getInstance("SHA-256").digest(jpeg))
            digest.append(String.format(java.util.Locale.ROOT, "%02x", item & 0xff));
        send(ble, BleClient.request("training_begin").put("upload_id", "device-smoke")
            .put("size", jpeg.length).put("sha256", digest.toString())
            .put("metadata", new JSONObject().put("consent", true).put("outcome", "correct")));
        for (int offset = 0; offset < jpeg.length; offset += 6144) {
            byte[] chunk = java.util.Arrays.copyOfRange(jpeg, offset, Math.min(jpeg.length, offset + 6144));
            send(ble, BleClient.request("training_chunk").put("upload_id", "device-smoke").put("offset", offset)
                .put("data", java.util.Base64.getEncoder().encodeToString(chunk)));
        }
        send(ble, BleClient.request("training_finish").put("upload_id", "device-smoke"));
        runOnMainSync(ble::forget);
    }

    private void checkDialog(Activity activity, String methodName, Class<?>[] types, Object[] arguments,
                             int expectedItems) throws Exception {
        Method method = MainActivity.class.getDeclaredMethod(methodName, types);
        method.setAccessible(true);
        AlertDialog[] dialog = new AlertDialog[1];
        Throwable[] failure = new Throwable[1];
        runOnMainSync(() -> {
            try { dialog[0] = (AlertDialog) method.invoke(activity, arguments); }
            catch (Throwable error) { failure[0] = error; }
        });
        if (failure[0] != null) throw new AssertionError(failure[0]);
        waitForIdleSync();
        runOnMainSync(() -> {
            try {
                check(dialog[0].getListView().isShown(), methodName + " hides its choices");
                check(dialog[0].getListView().getAdapter().getCount() == expectedItems, methodName + " choice count");
            } catch (Throwable error) { failure[0] = error; }
            finally { dialog[0].dismiss(); }
        });
        if (failure[0] != null) throw new AssertionError(failure[0]);
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
