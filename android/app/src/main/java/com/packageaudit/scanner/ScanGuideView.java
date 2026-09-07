package com.packageaudit.scanner;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.view.View;

/** High-contrast aiming guide; it never receives or stores camera pixels. */
final class ScanGuideView extends View {
    private final Paint border = new Paint(Paint.ANTI_ALIAS_FLAG);

    ScanGuideView(Context context) {
        super(context);
        setClickable(false);
        border.setColor(Color.rgb(68, 214, 148));
        border.setStyle(Paint.Style.STROKE);
        border.setStrokeWidth(getResources().getDisplayMetrics().density * 3.0f);
    }

    @Override protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        float insetX = getWidth() * 0.08f;
        float insetY = getHeight() * 0.25f;
        canvas.drawRoundRect(
            insetX, insetY, getWidth() - insetX, getHeight() - insetY,
            18.0f, 18.0f, border);
    }
}
