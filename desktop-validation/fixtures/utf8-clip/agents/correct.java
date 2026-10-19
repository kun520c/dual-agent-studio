package studio.util;

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
        if (maxBytes == 0 || text.isEmpty()) {
            return "";
        }
        int used = 0;
        int index = 0;
        while (index < text.length()) {
            int codePoint = text.codePointAt(index);
            int bytes = utf8Bytes(codePoint);
            if (used + bytes > maxBytes) {
                break;
            }
            used += bytes;
            index += Character.charCount(codePoint);
        }
        return text.substring(0, index);
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
