package com.packageaudit.scanner;

import org.junit.Test;
import static org.junit.Assert.*;
import org.json.JSONObject;

public class SecureChannelTest {
    private byte[] range(int first) { byte[] data=new byte[32]; for(int i=0;i<32;i++)data[i]=(byte)(first+i); return data; }
    @Test public void authenticatesPythonWelcomeAndDecryptsPythonMessages() throws Exception {
        SecureChannel.verifyWelcome(range(0),range(32),range(64),
            SecureChannel.unb64("m55PZvneuGl3zTMwSScb3SqgQEfUTIxLq+UHoRGACX4="));
        SecureChannel server = new SecureChannel(range(0),range(32),range(64),true);
        JSONObject payload = server.open(new JSONObject().put("seq",1).put("data",
            "F51ZQSCdIxkY6Xh+0+kfY8Pr6N6eW1CTlSnQLbg1LZbHndGAjRVCWUr6mqr1zVM="));
        assertEquals("status", payload.getString("op"));
        assertEquals("vector-1", payload.getString("id"));
        SecureChannel client = new SecureChannel(range(0),range(32),range(64),false);
        JSONObject reply = client.open(new JSONObject().put("seq",1).put("data",
            "oO4L/26aSjOHMpFfDwW/MF9f1UDrDTOPHZYaJ5jcx9Q0ycKC5+i2SDXIUw=="));
        assertTrue(reply.getBoolean("ok"));
    }
    @Test public void rejectsReplayedMessage() throws Exception {
        SecureChannel client = new SecureChannel(range(0),range(32),range(64),false);
        SecureChannel server = new SecureChannel(range(0),range(32),range(64),true);
        JSONObject packet = client.seal(new JSONObject().put("op","status"));
        assertEquals("status",server.open(packet).getString("op"));
        assertThrows(Exception.class, () -> server.open(packet));
    }
    @Test public void rejectsWrongPairingKeyAndTamperedCiphertext() throws Exception {
        assertThrows(Exception.class, () -> SecureChannel.verifyWelcome(range(1),range(32),range(64),range(0)));
        SecureChannel client = new SecureChannel(range(0),range(32),range(64),false);
        SecureChannel server = new SecureChannel(range(0),range(32),range(64),true);
        JSONObject packet = client.seal(new JSONObject().put("op","status"));
        byte[] ciphertext = SecureChannel.unb64(packet.getString("data")); ciphertext[0] ^= 1;
        packet.put("data",SecureChannel.b64(ciphertext));
        assertThrows(Exception.class, () -> server.open(packet));
    }
}
