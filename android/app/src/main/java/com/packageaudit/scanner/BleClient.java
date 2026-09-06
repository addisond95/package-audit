package com.packageaudit.scanner;

import android.annotation.SuppressLint;
import android.bluetooth.*;
import android.bluetooth.le.*;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.*;
import org.json.JSONObject;

/** One outstanding audit operation, retained across reconnects with the same request ID. */
@SuppressLint("MissingPermission")
final class BleClient {
    interface Listener {
        void state(String message, boolean connected);
        void response(String operation, JSONObject response);
    }
    static final UUID RX = UUID.fromString("690c9e36-68e1-4367-aad4-98b4e78d0001");
    static final UUID TX = UUID.fromString("690c9e36-68e1-4367-aad4-98b4e78d0002");
    static final UUID CCC = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    private final Context context;
    private final Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final BluetoothAdapter adapter;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic rx;
    private BluetoothLeScanner scanner;
    private UUID service;
    private byte[] secret, clientNonce;
    private SecureChannel channel;
    private JSONObject pending, queued;
    private boolean active, connected, writing, discovering;
    private int mtu = 23, attempts = 0, connectionEpoch = 0;
    private final ArrayDeque<byte[]> writes = new ArrayDeque<>();
    private final ByteArrayOutputStream input = new ByteArrayOutputStream();

    BleClient(Context context, Listener listener) {
        this.context = context;
        this.listener = listener;
        adapter = context.getSystemService(BluetoothManager.class).getAdapter();
    }

    boolean paired() { return secret != null; }
    boolean ready() { return connected; }
    boolean busy() { return pending != null || queued != null; }

    void pair(String qr) throws Exception {
        if (!qr.startsWith("packageaudit:")) throw new Exception("Scan the pairing QR in the Mac app.");
        JSONObject value = new JSONObject(qr.substring("packageaudit:".length()));
        if (value.getInt("v") != 1) throw new Exception("This pairing code needs a newer scanner app.");
        UUID newService = UUID.fromString(value.getString("service"));
        byte[] newSecret = SecureChannel.unb64(value.getString("key"));
        if (newSecret.length != 32) throw new Exception("Invalid pairing QR.");
        pause();
        service = newService;
        if (secret != null) Arrays.fill(secret, (byte) 0);
        secret = newSecret;
        pending = queued = null;
        resume();
    }

    void resume() {
        if (!paired()) return;
        if (active && (gatt != null || scanner != null)) return;
        active = true;
        attempts = 0;
        connect();
    }

    void pause() {
        active = false;
        connectionEpoch++;
        main.removeCallbacksAndMessages(null);
        closeTransport();
    }

    void forget() {
        pause();
        if (secret != null) Arrays.fill(secret, (byte) 0);
        secret = null;
        service = null;
        pending = queued = null;
        listener.state("Scan the Mac pairing QR", false);
    }

    private void closeTransport() {
        if (scanner != null) {
            try { scanner.stopScan(scanCallback); } catch (RuntimeException ignored) { }
            scanner = null;
        }
        BluetoothGatt old = gatt;
        gatt = null;
        if (old != null) {
            try { old.disconnect(); old.close(); } catch (RuntimeException ignored) { }
        }
        connected = false;
        channel = null;
        rx = null;
        writes.clear();
        writing = false;
        discovering = false;
        input.reset();
        mtu = 23;
    }

    private void connect() {
        if (!active || !paired()) return;
        closeTransport();
        int epoch = ++connectionEpoch;
        if (adapter == null || !adapter.isEnabled()) {
            active = false;
            listener.state("Turn on Bluetooth, then tap Reconnect.", false);
            return;
        }
        listener.state("Looking for your Mac…", false);
        try {
            scanner = adapter.getBluetoothLeScanner();
            if (scanner == null) throw new IllegalStateException();
            scanner.startScan(Collections.singletonList(new ScanFilter.Builder()
                .setServiceUuid(new ParcelUuid(service)).build()),
                new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scanCallback);
            main.postDelayed(() -> { if (epoch == connectionEpoch && !connected) fail("Mac not reachable."); }, 15000);
        } catch (RuntimeException error) { fail("Bluetooth permission or radio unavailable."); }
    }

    private void fail(String message) {
        if (!active) return;
        closeTransport();
        int epoch = ++connectionEpoch;
        attempts++;
        if (attempts > 5) {
            active = false;
            listener.state(message + " Tap Reconnect. Any pending confirmation is retained.", false);
            return;
        }
        listener.state(message + " Reconnecting…", false);
        main.postDelayed(() -> { if (active && epoch == connectionEpoch) connect(); }, Math.min(5000, 800L * attempts));
    }

    private final ScanCallback scanCallback = new ScanCallback() {
        @Override public void onScanResult(int type, ScanResult result) {
            main.post(() -> {
                if (!active || scanner == null || gatt != null) return;
                scanner.stopScan(this);
                scanner = null;
                listener.state("Connecting to Mac…", false);
                gatt = result.getDevice().connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE);
                if (gatt == null) fail("Could not connect.");
            });
        }
        @Override public void onScanFailed(int error) { main.post(() -> fail("Bluetooth discovery failed.")); }
    };

    private void discover(BluetoothGatt value) {
        if (value != gatt || discovering) return;
        discovering = true;
        if (!value.discoverServices()) fail("Could not discover scanner service.");
    }

    private final BluetoothGattCallback callback = new BluetoothGattCallback() {
        @Override public void onConnectionStateChange(BluetoothGatt value, int status, int state) {
            main.post(() -> {
                if (value != gatt || !active) return;
                if (status != BluetoothGatt.GATT_SUCCESS || state == BluetoothProfile.STATE_DISCONNECTED) {
                    fail("Bluetooth disconnected."); return;
                }
                if (state == BluetoothProfile.STATE_CONNECTED) {
                    if (!value.requestMtu(247)) discover(value);
                    else main.postDelayed(() -> discover(value), 2500);
                }
            });
        }
        @Override public void onMtuChanged(BluetoothGatt value, int negotiated, int status) {
            main.post(() -> {
                if (value != gatt) return;
                if (status == BluetoothGatt.GATT_SUCCESS) mtu = Math.max(23, negotiated);
                discover(value);
            });
        }
        @Override public void onServicesDiscovered(BluetoothGatt value, int status) {
            main.post(() -> {
                if (value != gatt) return;
                BluetoothGattService found = value.getService(service);
                if (status != BluetoothGatt.GATT_SUCCESS || found == null) { fail("Scanner service missing."); return; }
                rx = found.getCharacteristic(RX);
                BluetoothGattCharacteristic tx = found.getCharacteristic(TX);
                BluetoothGattDescriptor descriptor = tx == null ? null : tx.getDescriptor(CCC);
                if (rx == null || descriptor == null || !value.setCharacteristicNotification(tx, true)) {
                    fail("Scanner notifications unavailable."); return;
                }
                descriptor.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                if (!value.writeDescriptor(descriptor)) fail("Could not subscribe to scanner.");
            });
        }
        @Override public void onDescriptorWrite(BluetoothGatt value, BluetoothGattDescriptor descriptor, int status) {
            main.post(() -> {
                if (value != gatt) return;
                if (status != BluetoothGatt.GATT_SUCCESS) { fail("Scanner subscription failed."); return; }
                try {
                    clientNonce = new byte[32]; new SecureRandom().nextBytes(clientNonce);
                    writePacket(new JSONObject().put("hello", SecureChannel.b64(clientNonce)));
                } catch (Exception error) { fail("Could not authenticate."); }
            });
        }
        @Override public void onCharacteristicChanged(BluetoothGatt value, BluetoothGattCharacteristic characteristic) {
            byte[] bytes = characteristic.getValue().clone();
            main.post(() -> { if (value == gatt) accept(bytes); });
        }
        @Override public void onCharacteristicChanged(BluetoothGatt value, BluetoothGattCharacteristic characteristic, byte[] data) {
            byte[] bytes = data.clone();
            main.post(() -> { if (value == gatt) accept(bytes); });
        }
        @Override public void onCharacteristicWrite(BluetoothGatt value, BluetoothGattCharacteristic characteristic, int status) {
            main.post(() -> {
                if (value != gatt) return;
                if (status != BluetoothGatt.GATT_SUCCESS) { fail("Bluetooth write failed."); return; }
                writing = false;
                pump();
            });
        }
    };

    private void writePacket(JSONObject packet) throws Exception {
        byte[] bytes = (packet.toString() + "\n").getBytes(StandardCharsets.UTF_8);
        if (bytes.length > 16384) throw new Exception("Message too large");
        for (int offset = 0; offset < bytes.length; offset += mtu - 3)
            writes.add(Arrays.copyOfRange(bytes, offset, Math.min(bytes.length, offset + mtu - 3)));
        pump();
    }

    private void pump() {
        if (writing || writes.isEmpty() || gatt == null || rx == null) return;
        writing = true;
        rx.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
        rx.setValue(writes.remove());
        if (!gatt.writeCharacteristic(rx)) fail("Could not send to Mac.");
    }

    private void accept(byte[] bytes) {
        try {
            for (byte value : bytes) {
                if (value == 10) {
                    byte[] packet = input.toByteArray(); input.reset();
                    receive(new JSONObject(new String(packet, StandardCharsets.UTF_8)));
                } else {
                    if (input.size() >= 16384) throw new Exception("Message too large");
                    input.write(value);
                }
            }
        } catch (Exception error) { fail("Authentication or message verification failed."); }
    }

    private void receive(JSONObject envelope) throws Exception {
        if (channel == null) {
            byte[] serverNonce = SecureChannel.unb64(envelope.getString("welcome"));
            SecureChannel.verifyWelcome(secret, clientNonce, serverNonce, SecureChannel.unb64(envelope.getString("proof")));
            channel = new SecureChannel(secret, clientNonce, serverNonce, false);
            connected = true;
            listener.state("Connected securely • Bluetooth only", true);
            if (pending == null) pending = request("status");
            transmitPending();
            heartbeat(connectionEpoch);
            return;
        }
        JSONObject response = channel.open(envelope);
        if (pending == null || !pending.getString("id").equals(response.getString("id")))
            throw new Exception("Unexpected response");
        String op = pending.getString("op");
        pending = null;
        attempts = 0;
        listener.response(op, response);
        if (queued != null) { pending = queued; queued = null; transmitPending(); }
    }

    static JSONObject request(String operation) throws Exception {
        return new JSONObject().put("id", UUID.randomUUID().toString()).put("op", operation);
    }

    boolean send(JSONObject request) {
        if (!connected) return false;
        if (pending != null) {
            if ("status".equals(pending.optString("op")) && queued == null) { queued = request; return true; }
            return false;
        }
        pending = request;
        transmitPending();
        return true;
    }

    private void transmitPending() {
        try {
            writePacket(channel.seal(pending));
            String id = pending.getString("id");
            int epoch = connectionEpoch;
            main.postDelayed(() -> {
                if (active && epoch == connectionEpoch && pending != null && id.equals(pending.optString("id")))
                    fail("Mac has not acknowledged the request.");
            }, 10000);
        } catch (Exception error) { fail("Could not send encrypted request."); }
    }

    private void heartbeat(int epoch) {
        main.postDelayed(() -> {
            if (!connected || epoch != connectionEpoch) return;
            try { if (pending == null && queued == null) send(request("status")); }
            catch (Exception error) { fail("Connection check failed."); }
            heartbeat(epoch);
        }, 5000);
    }
}
