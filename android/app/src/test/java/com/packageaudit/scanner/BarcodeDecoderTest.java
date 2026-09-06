package com.packageaudit.scanner;

import org.junit.Test;
import static org.junit.Assert.*;
import com.google.zxing.*;
import com.google.zxing.common.BitMatrix;
import java.util.*;

public class BarcodeDecoderTest {
    private byte[] pixels(BitMatrix matrix) {
        byte[] result = new byte[matrix.getWidth() * matrix.getHeight()];
        for (int y = 0; y < matrix.getHeight(); y++) for (int x = 0; x < matrix.getWidth(); x++)
            result[y * matrix.getWidth() + x] = (byte) (matrix.get(x,y) ? 0 : 255);
        return result;
    }
    @Test public void decodesShippingFormatsOffline() throws Exception {
        for (BarcodeFormat format : Arrays.asList(BarcodeFormat.CODE_128, BarcodeFormat.QR_CODE,
                BarcodeFormat.DATA_MATRIX, BarcodeFormat.PDF_417)) {
            BitMatrix matrix = new MultiFormatWriter().encode("1Z999AA10123456784", format, 960, 400);
            // Data Matrix writers omit the surrounding label's quiet zone; supply it explicitly.
            int width = matrix.getWidth() + 80, height = matrix.getHeight() + 80;
            byte[] label = new byte[width * height]; Arrays.fill(label, (byte)255);
            byte[] code = pixels(matrix);
            for (int row=0; row<matrix.getHeight(); row++)
                System.arraycopy(code,row*matrix.getWidth(),label,(row+40)*width+40,matrix.getWidth());
            List<Result> results = BarcodeDecoder.read(label, width, height);
            assertTrue(format.toString(), results.stream().anyMatch(r -> r.getText().equals("1Z999AA10123456784")));
        }
    }
    @Test public void decodesVerticalAndInvertedCode128() throws Exception {
        BitMatrix matrix = new MultiFormatWriter().encode("1Z999AA10123456784", BarcodeFormat.CODE_128, 960, 300);
        byte[] original = pixels(matrix), rotated = new byte[original.length];
        int w = matrix.getWidth(), h = matrix.getHeight();
        for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) rotated[x*h + h-1-y] = original[y*w+x];
        assertEquals("1Z999AA10123456784", BarcodeDecoder.read(rotated,h,w).get(0).getText());
        for (int i=0; i<original.length; i++) original[i] = (byte) (255 - (original[i]&255));
        assertEquals("1Z999AA10123456784", BarcodeDecoder.read(original,w,h).get(0).getText());
    }
    @Test public void emptyFrameDoesNotInventTracking() {
        byte[] empty = new byte[640*480]; Arrays.fill(empty,(byte)255);
        assertTrue(BarcodeDecoder.read(empty,640,480).isEmpty());
    }
    @Test public void preservesTwoSeparateLabelsForDesktopAmbiguityCheck() throws Exception {
        BitMatrix first = new MultiFormatWriter().encode("1Z999AA10123456784", BarcodeFormat.QR_CODE, 300, 300);
        BitMatrix second = new MultiFormatWriter().encode("1Z999AA10123450000", BarcodeFormat.QR_CODE, 300, 300);
        byte[] canvas = new byte[800*400]; Arrays.fill(canvas,(byte)255);
        for (int y=0;y<300;y++) for(int x=0;x<300;x++) {
            canvas[(y+50)*800+x+30] = (byte)(first.get(x,y)?0:255);
            canvas[(y+50)*800+x+470] = (byte)(second.get(x,y)?0:255);
        }
        assertEquals(2,BarcodeDecoder.read(canvas,800,400).size());
    }
}
