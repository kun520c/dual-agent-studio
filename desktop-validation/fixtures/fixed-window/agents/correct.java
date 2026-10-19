package studio.limit;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;

/** Offline stand-in sample. Not part of the task repository. */
public final class FixedWindowLimiter {
    private final Clock clock;
    private final Duration window;
    private final int limit;
    private final ConcurrentHashMap<String, State> states = new ConcurrentHashMap<>();

    public FixedWindowLimiter(Clock clock, Duration window, int limit) {
        this.clock = Objects.requireNonNull(clock, "clock");
        if (window == null || window.isZero() || window.isNegative()) {
            throw new IllegalArgumentException("window");
        }
        if (limit < 1) {
            throw new IllegalArgumentException("limit");
        }
        this.window = window;
        this.limit = limit;
    }

    public boolean tryAcquire(String key) {
        Objects.requireNonNull(key, "key");
        Instant now = this.clock.instant();
        State state = this.states.computeIfAbsent(key, ignored -> new State());
        synchronized (state) {
            if (state.start == null || (!now.isBefore(state.start) && !now.isBefore(state.start.plus(this.window)))) {
                state.start = now;
                state.count = 1;
                return true;
            }
            if (state.count >= this.limit) {
                return false;
            }
            state.count++;
            return true;
        }
    }

    private static final class State {
        private Instant start;
        private int count;
    }
}
