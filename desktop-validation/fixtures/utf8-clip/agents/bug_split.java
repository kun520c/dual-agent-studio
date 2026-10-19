package studio.util;

import java.nio.charset.StandardCharsets;

public final class Utf8Clip {
    private Utf8Clip() {
    }

    public static int utf8Length(String text) {
        if (text == null) {
            throw new IllegalArgumentException("text");
        }
        int total = 0;
        int index = 0;
        while (index < text.length()) {
            int codePoint = text.codePointAt(index);
            total += utf8Bytes(codePoint);
            index += Character.charCount(codePoint);
        }
        return total;
    }

    public static String clip(String text, int maxBytes) {
        if (text == null) {
            throw new IllegalArgumentException("text");
        }
        if (maxBytes < 0) {
            throw new IllegalArgumentException("maxBytes");
        }
        byte[] raw = text.getBytes(StandardCharsets.UTF_8);
        if (maxBytes == 0 || raw.length == 0) {
            return "";
        }
        if (raw.length <= maxBytes) {
            return text;
        }
        return new String(raw, 0, maxBytes, StandardCharsets.UTF_8);
    }

    private static int utf8Bytes(int codePoint) {
        if (codePoint <= 0x7F) {
            return 1;
        }
        if (codePoint <= 0x7FF) {
            return 2;
        }
        if (codePoint <= 0xFFFF) {
            return 3;
        }
        return 4;
    }
}
