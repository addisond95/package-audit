package com.packageaudit.scanner;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Base64;
import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import org.json.JSONObject;

/** Wire-compatible with app/bluetooth_protocol.py. No custom encryption primitives. */
final class SecureChannel {
    static final byte[] PROTOCOL = "package-audit-ble-v1".getBytes(StandardCharsets.US_ASCII);
    private final byte[] sendKey, receiveKey;
    private long sent = 0, received = 0;

    static byte[] concat(byte[]... values) {
        int length = 0;
        for (byte[] value : values) length += value.length;
        ByteBuffer out = ByteBuffer.allocate(length);
        for (byte[] value : values) out.put(value);
        return out.array();
    }

    static byte[] hmac(byte[] key, byte[] value) throws GeneralSecurityException {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(key, "HmacSHA256"));
        return mac.doFinal(value);
    }

    static String b64(byte[] value) { return Base64.getEncoder().encodeToString(value); }
    static byte[] unb64(String value) { return Base64.getDecoder().decode(value); }

    SecureChannel(byte[] secret, byte[] client, byte[] server, boolean isServer) throws Exception {
        if (secret.length != 32 || client.length != 32 || server.length != 32)
            throw new GeneralSecurityException("Invalid pairing material");
        byte[] prk = hmac(concat(client, server), secret);
        byte[] c2s = hmac(prk, concat(PROTOCOL, new byte[]{1}));
        byte[] s2c = hmac(prk, concat(c2s, PROTOCOL, new byte[]{2}));
        sendKey = isServer ? s2c : c2s;
        receiveKey = isServer ? c2s : s2c;
        Arrays.fill(prk, (byte) 0);
    }

    static void verifyWelcome(byte[] secret, byte[] client, byte[] server, byte[] proof) throws Exception {
        if (!MessageDigest.isEqual(hmac(secret, concat(PROTOCOL, client, server)), proof))
            throw new GeneralSecurityException("Mac authentication failed");
    }

    JSONObject seal(JSONObject payload) throws Exception {
        if (sent == Long.MAX_VALUE) throw new GeneralSecurityException("Session exhausted");
        long sequence = ++sent;
        byte[] nonce = ByteBuffer.allocate(12).putInt(0).putLong(sequence).array();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(sendKey, "AES"), new GCMParameterSpec(128, nonce));
        cipher.updateAAD(PROTOCOL);
        return new JSONObject().put("seq", sequence).put("data",
            b64(cipher.doFinal(payload.toString().getBytes(StandardCharsets.UTF_8))));
    }

    JSONObject open(JSONObject envelope) throws Exception {
        Object raw = envelope.get("seq");
        if (!(raw instanceof Integer) && !(raw instanceof Long))
            throw new GeneralSecurityException("Invalid sequence");
        long sequence = ((Number) raw).longValue();
        if (sequence != received + 1) throw new GeneralSecurityException("Replayed or reordered message");
        byte[] nonce = ByteBuffer.allocate(12).putInt(0).putLong(sequence).array();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(receiveKey, "AES"), new GCMParameterSpec(128, nonce));
        cipher.updateAAD(PROTOCOL);
        JSONObject payload = new JSONObject(new String(cipher.doFinal(unb64(envelope.getString("data"))),
                                                      StandardCharsets.UTF_8));
        received = sequence;
        return payload;
    }
}
