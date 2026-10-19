package studio.util;

public final class Utf8Clip {
    private Utf8Clip() {
    }

    public static int utf8Length(String text) {
        if (text == null) {
            throw new IllegalArgumentException("text");
        }
        return text.length();
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
        return text.substring(0, Math.min(text.length(), maxBytes));
    }
}
