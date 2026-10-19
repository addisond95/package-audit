package com.packageaudit.scanner;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Matrix;
import android.graphics.Rect;
import androidx.camera.core.ImageProxy;
import ai.onnxruntime.*;
import java.io.*;
import java.nio.FloatBuffer;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.spec.X509EncodedKeySpec;
import java.util.*;
import org.json.*;

/** Verified, optional ONNX label model. Inference is advisory and entirely offline. */
final class LabelModel implements AutoCloseable {
    static final class Prediction {
        final String carrier, condition, releaseId;
        final float carrierConfidence, conditionConfidence;
        Prediction(String carrier, float carrierConfidence, String condition,
                   float conditionConfidence, String releaseId) {
            this.carrier = carrier; this.carrierConfidence = carrierConfidence;
            this.condition = condition; this.conditionConfidence = conditionConfidence;
            this.releaseId = releaseId;
        }
    }

    private static final int SIZE = 224;
    private final OrtEnvironment environment;
    private final OrtSession session;
    private final String[] carriers, conditions;
    private final String releaseId;
    private long lastInference;

    static LabelModel load(Context context) {
        try {
            byte[] model = readAsset(context, "package_audit_label.onnx");
            byte[] manifestBytes = readAsset(context, "package_audit_model_manifest.json");
            byte[] signature = readAsset(context, "package_audit_model_manifest.sig");
            byte[] publicPem = readAsset(context, "package_audit_model_public.pem");
            verifySignature(publicPem, manifestBytes, signature);
            JSONObject manifest = new JSONObject(new String(manifestBytes, StandardCharsets.UTF_8));
            if (!manifest.optBoolean("approved", false)) return null;
            String actualHash = hex(MessageDigest.getInstance("SHA-256").digest(model));
            if (!actualHash.equals(manifest.getString("model_sha256"))) return null;
            return new LabelModel(model, manifest);
        } catch (Exception unavailable) {
            return null;
        }
    }

    private LabelModel(byte[] model, JSONObject manifest) throws Exception {
        environment = OrtEnvironment.getEnvironment();
        environment.setTelemetry(false);
        carriers = strings(manifest.getJSONArray("carrier_labels"));
        conditions = strings(manifest.getJSONArray("condition_labels"));
        releaseId = manifest.getString("release_id");
        try (OrtSession.SessionOptions options = new OrtSession.SessionOptions()) {
            options.setIntraOpNumThreads(2);
            session = environment.createSession(model, options);
        }
    }

    boolean ready() { return session != null; }
    String releaseId() { return releaseId; }

    Prediction infer(ImageProxy image) {
        long now = android.os.SystemClock.elapsedRealtime();
        if (now - lastInference < 1200) return null;
        lastInference = now;
        try {
            FloatBuffer input;
            Bitmap source = image.toBitmap();
            Rect crop = image.getCropRect();
            Bitmap cropped = Bitmap.createBitmap(source, crop.left, crop.top, crop.width(), crop.height());
            try {
                input = rgbTensor(cropped, image.getImageInfo().getRotationDegrees());
            } finally {
                if (cropped != source) cropped.recycle();
                source.recycle();
            }
            try (OnnxTensor tensor = OnnxTensor.createTensor(
                    environment, input, new long[]{1, 3, SIZE, SIZE});
                 OrtSession.Result result = session.run(Collections.singletonMap("image", tensor))) {
                float[] carrierLogits = ((float[][]) result.get("carrier_logits").get().getValue())[0];
                float[] conditionLogits = ((float[][]) result.get("condition_logits").get().getValue())[0];
                int carrier = maximum(carrierLogits), condition = maximum(conditionLogits);
                return new Prediction(
                    carriers[carrier], softmax(carrierLogits, carrier),
                    conditions[condition], softmax(conditionLogits, condition), releaseId);
            }
        } catch (Exception ignored) {
            return null;
        }
    }

    static FloatBuffer rgbTensor(Bitmap source, int rotation) {
        Matrix transform = new Matrix();
        transform.postRotate(rotation);
        Bitmap oriented = Bitmap.createBitmap(source, 0, 0, source.getWidth(), source.getHeight(), transform, true);
        Bitmap resized = Bitmap.createScaledBitmap(oriented, SIZE, SIZE, true);
        int[] pixels = new int[SIZE * SIZE];
        resized.getPixels(pixels, 0, SIZE, 0, 0, SIZE, SIZE);
        if (resized != oriented && resized != source) resized.recycle();
        if (oriented != source) oriented.recycle();
        FloatBuffer result = FloatBuffer.allocate(3 * SIZE * SIZE);
        float[] mean = {0.485f, 0.456f, 0.406f};
        float[] std = {0.229f, 0.224f, 0.225f};
        for (int channel = 0; channel < 3; channel++)
            for (int pixel : pixels) {
                float value = ((pixel >> (16 - channel * 8)) & 0xff) / 255f;
                result.put((value - mean[channel]) / std[channel]);
            }
        result.rewind();
        return result;
    }

    private static int maximum(float[] values) {
        int result = 0;
        for (int i = 1; i < values.length; i++) if (values[i] > values[result]) result = i;
        return result;
    }

    private static float softmax(float[] values, int selected) {
        float maximum = values[maximum(values)], sum = 0f;
        for (float value : values) sum += (float) Math.exp(value - maximum);
        return (float) Math.exp(values[selected] - maximum) / sum;
    }

    private static byte[] readAsset(Context context, String name) throws IOException {
        try (InputStream input = context.getAssets().open(name);
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192]; int count;
            while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
            return output.toByteArray();
        }
    }

    private static void verifySignature(byte[] pem, byte[] data, byte[] signature) throws Exception {
        String value = new String(pem, StandardCharsets.US_ASCII)
            .replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", "")
            .replaceAll("\\s", "");
        PublicKey key = KeyFactory.getInstance("Ed25519").generatePublic(
            new X509EncodedKeySpec(Base64.getDecoder().decode(value)));
        Signature verifier = Signature.getInstance("Ed25519");
        verifier.initVerify(key); verifier.update(data);
        if (!verifier.verify(signature)) throw new SignatureException("Invalid model signature");
    }

    private static String[] strings(JSONArray values) throws JSONException {
        String[] result = new String[values.length()];
        for (int index = 0; index < values.length(); index++) result[index] = values.getString(index);
        return result;
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) result.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        return result.toString();
    }

    @Override public void close() {
        try { session.close(); } catch (OrtException ignored) {}
    }
}
